"""The RabbitMQ publisher, exercised without a broker.

A recorder stands in for the pika channel, so the exchange declaration, the routing
key, the persistence and the AMQP properties are all assertable, and a broker that
cannot be reached is checked to arrive as the module's own typed error rather than
as a pika exception. Nothing here needs a RabbitMQ install or a running broker.
"""

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest

from app.main import app
from app.rabbit_publisher import (
    DEFAULT_RABBITMQ_EXCHANGE,
    DEFAULT_RABBITMQ_ROUTING_KEY_PREFIX,
    DEFAULT_RABBITMQ_URL,
    RABBIT_UNAVAILABLE_DETAIL,
    RabbitDeliveryResult,
    RabbitMessageProperties,
    RabbitPublisher,
    RabbitSettings,
    RabbitUnavailableError,
    get_rabbit_publisher,
)
from app.security import BOOKINGS_SERVICE_TOKEN

RABBIT_URL = "/api/events/rabbit"
TOKEN_HEADER = "X-Service-Token"
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
PUBLISH_KEYS = {"eventId", "exchange", "routingKey"}
DELIVERED_ENVELOPE_KEYS = {"eventId", "type", "version", "occurredAt", "payload", "key"}
ENVELOPE_WITHOUT_KEY_KEYS = {"eventId", "type", "version", "occurredAt", "payload"}


class _RecordingChannel:
    """Stand-in for a pika channel: records the declare and each published message."""

    def __init__(
        self,
        *,
        declare_error: Exception | None = None,
        publish_error: Exception | None = None,
    ) -> None:
        self.declare_error = declare_error
        self.publish_error = publish_error
        self.declares: list[dict[str, object]] = []
        self.messages: list[dict[str, object]] = []
        self.calls: list[str] = []
        self.closed = False

    def exchange_declare(
        self, *, exchange: str, exchange_type: str, durable: bool
    ) -> None:
        self.calls.append("declare")
        if self.declare_error is not None:
            raise self.declare_error
        self.declares.append(
            {"exchange": exchange, "exchange_type": exchange_type, "durable": durable}
        )

    def basic_publish(
        self,
        exchange: str,
        routing_key: str,
        body: bytes,
        properties: RabbitMessageProperties,
    ) -> None:
        self.calls.append("publish")
        if self.publish_error is not None:
            raise self.publish_error
        self.messages.append(
            {
                "exchange": exchange,
                "routing_key": routing_key,
                "body": body,
                "properties": properties,
            }
        )

    def close(self) -> None:
        self.closed = True


def _settings(routing_key_prefix: str = "bookmyshow") -> RabbitSettings:
    """Settings pointed at a broker that is never contacted in these tests."""
    return RabbitSettings(
        url="amqp://guest:guest@broker.test:5672/",
        exchange="bookmyshow.events",
        routing_key_prefix=routing_key_prefix,
    )


def _envelope_of(record: dict[str, object]) -> dict[str, object]:
    body = record["body"]
    assert isinstance(body, bytes)
    return json.loads(body.decode("utf-8"))


def _properties_of(record: dict[str, object]) -> RabbitMessageProperties:
    properties = record["properties"]
    assert isinstance(properties, RabbitMessageProperties)
    return properties


class _FakeRabbitPublisher:
    """Stand-in for RabbitPublisher: records each publish and answers as told."""

    def __init__(
        self,
        *,
        exchange: str = "bookmyshow.events",
        routing_key: str = "bookmyshow.booking.confirmed",
        outcome: str = "succeed",
        dead_letter_outcome: str = "fail",
    ) -> None:
        self.exchange = exchange
        self.routing_key = routing_key
        self.outcome = outcome
        self.dead_letter_outcome = dead_letter_outcome
        self.calls: list[dict[str, object]] = []
        self.event_ids: list[str | None] = []
        self.dead_letters: list[dict[str, object]] = []

    def publish(
        self,
        *,
        event_type: str,
        payload: dict[str, object],
        version: int = 1,
        key: str | None = None,
        event_id: str | None = None,
    ) -> RabbitDeliveryResult:
        self.calls.append(
            {
                "event_type": event_type,
                "payload": payload,
                "version": version,
                "key": key,
            }
        )
        self.event_ids.append(event_id)
        if self.outcome == "unavailable":
            raise RabbitUnavailableError()
        return RabbitDeliveryResult(
            event_id=event_id if event_id is not None else str(uuid.uuid4()),
            exchange=self.exchange,
            routing_key=self.routing_key,
        )

    def publish_dead_letter(
        self,
        *,
        event_id: str,
        event_type: str,
        payload: dict[str, object],
        version: int = 1,
        key: str | None = None,
        attempts: int,
        last_error: str,
    ) -> RabbitDeliveryResult:
        """Record the dead-letter write; only a `succeed` double actually parks it."""
        self.dead_letters.append(
            {
                "event_id": event_id,
                "event_type": event_type,
                "payload": payload,
                "version": version,
                "key": key,
                "attempts": attempts,
                "last_error": last_error,
            }
        )
        if self.dead_letter_outcome == "fail":
            raise RabbitUnavailableError()
        return RabbitDeliveryResult(
            event_id=event_id,
            exchange=self.exchange,
            routing_key=f"{self.routing_key}.dlq",
        )


@contextmanager
def _use_publisher(publisher: object) -> Iterator[None]:
    app.dependency_overrides[get_rabbit_publisher] = lambda: publisher
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_rabbit_publisher, None)


def _service_headers(token: str = BOOKINGS_SERVICE_TOKEN) -> dict[str, str]:
    return {TOKEN_HEADER: token}


def test_settings_read_the_three_environment_variables(monkeypatch) -> None:
    monkeypatch.setenv("RABBITMQ_URL", "amqp://events:secret@rabbit.internal:5672/%2f")
    monkeypatch.setenv("RABBITMQ_EXCHANGE", "tickets.events")
    monkeypatch.setenv("RABBITMQ_ROUTING_KEY_PREFIX", "bookmyshow")

    settings = RabbitSettings.from_env()

    assert settings == RabbitSettings(
        url="amqp://events:secret@rabbit.internal:5672/%2f",
        exchange="tickets.events",
        routing_key_prefix="bookmyshow",
    )


def test_settings_fall_back_when_the_variables_are_unset_or_blank(
    monkeypatch,
) -> None:
    for name in (
        "RABBITMQ_URL",
        "RABBITMQ_EXCHANGE",
        "RABBITMQ_ROUTING_KEY_PREFIX",
    ):
        monkeypatch.delenv(name, raising=False)

    unset = RabbitSettings.from_env()

    assert unset.url == DEFAULT_RABBITMQ_URL == "amqp://guest:guest@localhost:5672/"
    assert unset.exchange == DEFAULT_RABBITMQ_EXCHANGE == "bookmyshow.events"
    assert unset.routing_key_prefix == DEFAULT_RABBITMQ_ROUTING_KEY_PREFIX == ""

    monkeypatch.setenv("RABBITMQ_URL", "   ")
    monkeypatch.setenv("RABBITMQ_EXCHANGE", "")
    monkeypatch.setenv("RABBITMQ_ROUTING_KEY_PREFIX", "  ")

    blank = RabbitSettings.from_env()

    assert blank.url == DEFAULT_RABBITMQ_URL
    assert blank.exchange == DEFAULT_RABBITMQ_EXCHANGE
    assert blank.routing_key_prefix == DEFAULT_RABBITMQ_ROUTING_KEY_PREFIX
    assert blank.routing_key_for("booking.confirmed") == "booking.confirmed"


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("bookmyshow", "bookmyshow.booking.confirmed"),
        ("bookmyshow.", "bookmyshow.booking.confirmed"),
        ("  bookmyshow  ", "bookmyshow.booking.confirmed"),
        ("", "booking.confirmed"),
        ("   ", "booking.confirmed"),
    ],
)
def test_routing_key_is_the_prefix_and_the_type(prefix, expected) -> None:
    assert _settings(prefix).routing_key_for("booking.confirmed") == expected


def test_second_publish_does_not_declare_the_exchange_again() -> None:
    channel = _RecordingChannel()
    publisher = RabbitPublisher(_settings(), channel=channel)

    publisher.publish(event_type="booking.confirmed", payload={"bookingId": 1})
    publisher.publish(event_type="booking.confirmed", payload={"bookingId": 2})

    assert channel.declares == [
        {
            "exchange": "bookmyshow.events",
            "exchange_type": "topic",
            "durable": True,
        }
    ]
    # The exchange is declared before the message that needs it.
    assert channel.calls == ["declare", "publish", "publish"]
    assert [record["exchange"] for record in channel.messages] == [
        "bookmyshow.events",
        "bookmyshow.events",
    ]
    assert [record["routing_key"] for record in channel.messages] == [
        "bookmyshow.booking.confirmed",
        "bookmyshow.booking.confirmed",
    ]


def test_publish_sends_a_persistent_message_with_the_envelope_and_headers() -> None:
    channel = _RecordingChannel()
    publisher = RabbitPublisher(_settings(), channel=channel)

    result = publisher.publish(
        event_type="booking.confirmed",
        payload={"bookingId": 42},
        key="booking-42",
    )

    assert isinstance(result, RabbitDeliveryResult)
    assert result.exchange == "bookmyshow.events"
    assert result.routing_key == "bookmyshow.booking.confirmed"

    assert len(channel.messages) == 1
    record = channel.messages[0]
    assert record["exchange"] == "bookmyshow.events"
    assert record["routing_key"] == "bookmyshow.booking.confirmed"

    properties = _properties_of(record)
    assert properties.delivery_mode == 2
    assert properties.content_type == "application/json"

    envelope = _envelope_of(record)
    assert set(envelope) == DELIVERED_ENVELOPE_KEYS
    assert properties.message_id == envelope["eventId"] == result.event_id
    assert uuid.UUID(str(envelope["eventId"]))
    assert envelope["type"] == "booking.confirmed"
    assert envelope["version"] == 1
    assert envelope["payload"] == {"bookingId": 42}
    assert envelope["key"] == "booking-42"
    assert datetime.fromisoformat(str(envelope["occurredAt"])).utcoffset() == timedelta(
        0
    )

    assert properties.headers == {
        "eventId": result.event_id,
        "type": "booking.confirmed",
        "correlationId": "booking-42",
    }


def test_a_missing_key_falls_back_to_the_event_id_for_the_correlation_id() -> None:
    channel = _RecordingChannel()
    publisher = RabbitPublisher(_settings(), channel=channel)

    result = publisher.publish(event_type="movie.created", payload={"movieId": 1})

    record = channel.messages[0]
    envelope = _envelope_of(record)
    assert set(envelope) == ENVELOPE_WITHOUT_KEY_KEYS
    assert "key" not in envelope

    properties = _properties_of(record)
    assert properties.message_id == result.event_id
    assert properties.headers == {
        "eventId": result.event_id,
        "type": "movie.created",
        "correlationId": result.event_id,
    }


def test_publish_defaults_version_to_one() -> None:
    channel = _RecordingChannel()
    publisher = RabbitPublisher(_settings(), channel=channel)

    publisher.publish(event_type="movie.created", payload={"movieId": 1})
    publisher.publish(event_type="movie.created", payload={"movieId": 1}, version=3)

    versions = [_envelope_of(record)["version"] for record in channel.messages]
    assert versions == [1, 3]


def test_publish_raises_the_typed_error_when_the_broker_refuses(caplog) -> None:
    broken = _RecordingChannel(publish_error=RuntimeError("channel is closed"))

    with pytest.raises(RabbitUnavailableError):
        RabbitPublisher(_settings(), channel=broken).publish(
            event_type="booking.confirmed", payload={"bookingId": 42}
        )

    assert broken.messages == []
    warnings = [
        record
        for record in caplog.records
        if record.name == "app.rabbit_publisher" and record.levelname == "WARNING"
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "eventId=" in message
    assert "type=booking.confirmed" in message
    assert "exchange=bookmyshow.events" in message
    assert "routingKey=bookmyshow.booking.confirmed" in message


def test_publishing_answers_202_with_the_exchange_and_routing_key(client) -> None:
    channel = _RecordingChannel()
    publisher = RabbitPublisher(_settings(), channel=channel)

    with _use_publisher(publisher):
        response = client.post(
            RABBIT_URL,
            json={
                "type": "booking.confirmed",
                "payload": {"bookingId": 42},
                "key": "booking-42",
            },
            headers=_service_headers(),
        )

    assert response.status_code == 202
    body = response.json()
    assert set(body) == PUBLISH_KEYS
    assert body["exchange"] == "bookmyshow.events"
    assert body["routingKey"] == "bookmyshow.booking.confirmed"

    assert len(channel.messages) == 1
    record = channel.messages[0]
    envelope = _envelope_of(record)
    assert body["eventId"] == envelope["eventId"]
    assert set(envelope) == DELIVERED_ENVELOPE_KEYS
    assert envelope["type"] == "booking.confirmed"
    assert envelope["payload"] == {"bookingId": 42}
    assert envelope["key"] == "booking-42"
    assert _properties_of(record).message_id == body["eventId"]


def test_omitted_key_and_version_use_the_documented_defaults(client) -> None:
    fake = _FakeRabbitPublisher()

    with _use_publisher(fake):
        response = client.post(
            RABBIT_URL,
            json={"type": "movie.created", "payload": {"movieId": 1}},
            headers=_service_headers(),
        )

    assert response.status_code == 202
    assert set(response.json()) == PUBLISH_KEYS
    assert fake.calls == [
        {
            "event_type": "movie.created",
            "payload": {"movieId": 1},
            "version": 1,
            "key": None,
        }
    ]


def test_a_broker_failure_on_publish_returns_503(client) -> None:
    channel = _RecordingChannel(publish_error=RuntimeError("no broker"))
    publisher = RabbitPublisher(_settings(), channel=channel)

    with _use_publisher(publisher):
        response = client.post(
            RABBIT_URL,
            json={"type": "booking.confirmed", "payload": {"bookingId": 42}},
            headers=_service_headers(),
        )

    assert response.status_code == 503
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Service Unavailable"
    assert body["status"] == 503
    assert body["detail"] == RABBIT_UNAVAILABLE_DETAIL
    assert body["instance"] == RABBIT_URL
    assert channel.messages == []


def test_a_failure_declaring_the_exchange_returns_503(client) -> None:
    channel = _RecordingChannel(declare_error=RuntimeError("ACCESS_REFUSED"))
    publisher = RabbitPublisher(_settings(), channel=channel)

    with _use_publisher(publisher):
        response = client.post(
            RABBIT_URL,
            json={"type": "booking.confirmed", "payload": {"bookingId": 42}},
            headers=_service_headers(),
        )

    assert response.status_code == 503
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Service Unavailable"
    assert body["status"] == 503
    assert body["detail"] == RABBIT_UNAVAILABLE_DETAIL
    assert body["instance"] == RABBIT_URL
    assert channel.declares == []
    assert channel.messages == []


def test_without_a_publisher_the_route_answers_503(client) -> None:
    built = client.app.state.rabbit_publisher
    client.app.state.rabbit_publisher = None
    try:
        response = client.post(
            RABBIT_URL,
            json={"type": "booking.confirmed", "payload": {"bookingId": 42}},
            headers=_service_headers(),
        )
    finally:
        client.app.state.rabbit_publisher = built

    assert response.status_code == 503
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Service Unavailable"
    assert body["status"] == 503
    assert body["detail"] == RABBIT_UNAVAILABLE_DETAIL
    assert body["instance"] == RABBIT_URL


@pytest.mark.parametrize(
    "body",
    [
        {"type": "booking.confirmed"},
        {"type": "", "payload": {}},
        {"type": "   ", "payload": {}},
        {"type": "booking.confirmed", "payload": {}, "version": 0},
        {"type": "booking.confirmed", "payload": ["not", "an", "object"]},
        {"type": "booking.confirmed", "payload": {}, "key": "   "},
    ],
)
def test_invalid_body_returns_422_and_publishes_nothing(client, body) -> None:
    fake = _FakeRabbitPublisher()

    with _use_publisher(fake):
        response = client.post(RABBIT_URL, json=body, headers=_service_headers())

    assert response.status_code == 422
    problem = response.json()
    assert set(problem) == PROBLEM_DETAIL_KEYS
    assert problem["type"] == "about:blank"
    assert problem["status"] == 422
    assert problem["instance"] == RABBIT_URL
    assert fake.calls == []


def test_publishing_requires_the_service_token(client) -> None:
    channel = _RecordingChannel()
    publisher = RabbitPublisher(_settings(), channel=channel)
    payload = {"type": "booking.confirmed", "payload": {"bookingId": 42}}

    with _use_publisher(publisher):
        missing = client.post(RABBIT_URL, json=payload)
        wrong = client.post(
            RABBIT_URL, json=payload, headers=_service_headers("not-the-token")
        )

    for response in (missing, wrong):
        assert response.status_code == 401
        body = response.json()
        assert set(body) == PROBLEM_DETAIL_KEYS
        assert body["type"] == "about:blank"
        assert body["title"] == "Unauthorized"
        assert body["status"] == 401
        assert body["detail"] == "Invalid service token"
        assert body["instance"] == RABBIT_URL

    assert channel.messages == []
