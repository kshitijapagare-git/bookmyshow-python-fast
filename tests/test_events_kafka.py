"""The Kafka publisher, exercised without a broker.

`confluent_kafka.Producer` is replaced by a recorder, so the topic, the partition
key, the headers and the decoded envelope are all assertable, and a broker that
cannot be reached is checked to arrive as the module's own typed error rather than
as a librdkafka exception.
"""

import json
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.kafka_producer import (
    DEFAULT_FLUSH_TIMEOUT_MS,
    KAFKA_UNAVAILABLE_DETAIL,
    DeliveryResult,
    EventPublisher,
    KafkaSettings,
    KafkaUnavailableError,
    get_event_publisher,
)
from app.main import app
from app.security import BOOKINGS_SERVICE_TOKEN


class _DeliveryMessage:
    """The two accessors the publisher reads off confluent_kafka.Message."""

    def __init__(self, partition: int, offset: int) -> None:
        self._partition = partition
        self._offset = offset

    def partition(self) -> int:
        return self._partition

    def offset(self) -> int:
        return self._offset


def _delivery_message(partition: int, offset: int) -> object:
    """A minimal delivery report, shaped like the one librdkafka hands back."""
    return _DeliveryMessage(partition, offset)


class _RecordingProducer:
    """Stand-in for confluent_kafka.Producer: records each record and answers as told."""

    def __init__(
        self,
        *,
        remaining: int = 0,
        partition: int = 3,
        offset: int = 42,
        produce_error: Exception | None = None,
    ) -> None:
        self.remaining = remaining
        self.partition = partition
        self.offset = offset
        self.produce_error = produce_error
        self.records: list[dict[str, object]] = []
        self.flush_timeouts: list[float] = []
        self._on_delivery = None

    def produce(self, topic, *, value=None, key=None, headers=None, on_delivery=None):
        if self.produce_error is not None:
            raise self.produce_error
        self.records.append(
            {"topic": topic, "value": value, "key": key, "headers": headers}
        )
        self._on_delivery = on_delivery

    def poll(self, timeout):
        return 0

    def flush(self, timeout):
        self.flush_timeouts.append(timeout)
        if self.remaining == 0 and self._on_delivery is not None:
            self._on_delivery(None, _delivery_message(self.partition, self.offset))
        return self.remaining


def _settings(flush_timeout_ms: int = DEFAULT_FLUSH_TIMEOUT_MS) -> KafkaSettings:
    return KafkaSettings(
        bootstrap_servers="broker.test:9092",
        events_topic="bookmyshow.events",
        client_id="bookmyshow-api",
        flush_timeout_ms=flush_timeout_ms,
    )


def _envelope_of(record: dict[str, object]) -> dict[str, object]:
    value = record["value"]
    assert isinstance(value, bytes)
    return json.loads(value.decode("utf-8"))


def test_settings_read_the_four_environment_variables(monkeypatch) -> None:
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "kafka.internal:9093")
    monkeypatch.setenv("KAFKA_EVENTS_TOPIC", "tickets.events")
    monkeypatch.setenv("KAFKA_CLIENT_ID", "bookings-api")
    monkeypatch.setenv("KAFKA_FLUSH_TIMEOUT_MS", "750")

    settings = KafkaSettings.from_env()

    assert settings == KafkaSettings(
        bootstrap_servers="kafka.internal:9093",
        events_topic="tickets.events",
        client_id="bookings-api",
        flush_timeout_ms=750,
    )

    # A malformed or non-positive timeout falls back instead of raising.
    monkeypatch.setenv("KAFKA_FLUSH_TIMEOUT_MS", "not-a-number")
    assert KafkaSettings.from_env().flush_timeout_ms == DEFAULT_FLUSH_TIMEOUT_MS == 5000

    monkeypatch.setenv("KAFKA_FLUSH_TIMEOUT_MS", "0")
    assert KafkaSettings.from_env().flush_timeout_ms == DEFAULT_FLUSH_TIMEOUT_MS

    monkeypatch.setenv("KAFKA_FLUSH_TIMEOUT_MS", "-1")
    assert KafkaSettings.from_env().flush_timeout_ms == DEFAULT_FLUSH_TIMEOUT_MS


def test_publish_sends_the_envelope_key_and_headers() -> None:
    producer = _RecordingProducer()
    publisher = EventPublisher(_settings(), producer=producer)

    result = publisher.publish(
        event_type="booking.confirmed",
        payload={"bookingId": 42},
        key="booking-42",
    )

    assert isinstance(result, DeliveryResult)
    assert len(producer.records) == 1
    record = producer.records[0]
    assert record["topic"] == "bookmyshow.events"
    assert record["key"] == "booking-42"
    assert record["headers"] == [
        ("eventId", result.event_id.encode("utf-8")),
        ("type", b"booking.confirmed"),
    ]

    envelope = _envelope_of(record)
    assert set(envelope) == {
        "eventId",
        "type",
        "version",
        "occurredAt",
        "payload",
        "key",
    }
    assert envelope["eventId"] == result.event_id
    assert uuid.UUID(str(envelope["eventId"]))
    assert envelope["type"] == "booking.confirmed"
    assert envelope["payload"] == {"bookingId": 42}
    assert envelope["key"] == "booking-42"
    assert datetime.fromisoformat(str(envelope["occurredAt"])).utcoffset() == timedelta(
        0
    )

    assert result.topic == "bookmyshow.events"
    assert result.partition == 3
    assert result.offset == 42
    assert producer.flush_timeouts == [5.0]


def test_publish_omits_a_missing_key_from_the_envelope() -> None:
    producer = _RecordingProducer()
    publisher = EventPublisher(_settings(), producer=producer)

    result = publisher.publish(event_type="booking.confirmed", payload={"id": 1})

    record = producer.records[0]
    assert record["key"] is None
    assert record["headers"] == [
        ("eventId", result.event_id.encode("utf-8")),
        ("type", b"booking.confirmed"),
    ]

    envelope = _envelope_of(record)
    assert set(envelope) == {"eventId", "type", "version", "occurredAt", "payload"}
    assert "key" not in envelope


def test_publish_defaults_version_to_one() -> None:
    producer = _RecordingProducer()
    publisher = EventPublisher(_settings(), producer=producer)

    publisher.publish(event_type="movie.created", payload={"movieId": 1})
    publisher.publish(event_type="movie.created", payload={"movieId": 1}, version=3)

    versions = [_envelope_of(record)["version"] for record in producer.records]
    assert versions == [1, 3]


def test_publish_raises_when_flush_does_not_drain() -> None:
    stuck = _RecordingProducer(remaining=1)

    with pytest.raises(KafkaUnavailableError):
        EventPublisher(_settings(flush_timeout_ms=50), producer=stuck).publish(
            event_type="booking.confirmed", payload={"bookingId": 42}
        )

    assert stuck.flush_timeouts == [0.05]

    broken = _RecordingProducer(produce_error=RuntimeError("no broker"))
    with pytest.raises(KafkaUnavailableError):
        EventPublisher(_settings(), producer=broken).publish(
            event_type="booking.confirmed", payload={"bookingId": 42}
        )

    assert broken.records == []


def test_publish_logs_the_delivery(caplog) -> None:
    publisher = EventPublisher(
        _settings(), producer=_RecordingProducer(partition=2, offset=17)
    )

    with caplog.at_level(logging.INFO, logger="app.kafka_producer"):
        result = publisher.publish(
            event_type="booking.confirmed", payload={"bookingId": 42}, key="booking-42"
        )

    records = [record for record in caplog.records if record.name == "app.kafka_producer"]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    message = records[0].getMessage()
    assert f"eventId={result.event_id}" in message
    assert "type=booking.confirmed" in message
    assert "topic=bookmyshow.events" in message
    assert "partition=2" in message
    assert "offset=17" in message


EVENTS_URL = "/api/events/kafka"
TOKEN_HEADER = "X-Service-Token"
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
EVENT_KEYS = {"eventId", "topic", "partition", "offset"}


class _FakePublisher:
    """Stand-in for EventPublisher: records each publish and answers as told."""

    def __init__(
        self,
        *,
        topic: str = "bookmyshow.events",
        partition: int = 4,
        offset: int = 11,
        outcome: str = "succeed",
    ) -> None:
        self.topic = topic
        self.partition = partition
        self.offset = offset
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    def publish(
        self,
        *,
        event_type: str,
        payload: dict[str, object],
        version: int = 1,
        key: str | None = None,
    ) -> DeliveryResult:
        self.calls.append(
            {
                "event_type": event_type,
                "payload": payload,
                "version": version,
                "key": key,
            }
        )
        if self.outcome == "unavailable":
            raise KafkaUnavailableError()
        return DeliveryResult(
            event_id=str(uuid.uuid4()),
            topic=self.topic,
            partition=self.partition,
            offset=self.offset,
        )


@contextmanager
def _use_publisher(publisher) -> Iterator[None]:
    app.dependency_overrides[get_event_publisher] = lambda: publisher
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_event_publisher, None)


def _service_headers(token: str = BOOKINGS_SERVICE_TOKEN) -> dict[str, str]:
    return {TOKEN_HEADER: token}


def test_publishes_the_envelope_key_and_headers(client) -> None:
    producer = _RecordingProducer(partition=5, offset=90)
    publisher = EventPublisher(_settings(), producer=producer)

    with _use_publisher(publisher):
        response = client.post(
            EVENTS_URL,
            json={
                "type": "booking.confirmed",
                "payload": {"bookingId": 42},
                "key": "booking-42",
            },
            headers=_service_headers(),
        )

    assert response.status_code == 202
    body = response.json()
    assert set(body) == EVENT_KEYS
    assert body["topic"] == "bookmyshow.events"
    assert body["partition"] == 5
    assert body["offset"] == 90

    assert len(producer.records) == 1
    record = producer.records[0]
    assert record["topic"] == "bookmyshow.events"
    assert record["key"] == "booking-42"

    envelope = _envelope_of(record)
    assert set(envelope) == {
        "eventId",
        "type",
        "version",
        "occurredAt",
        "payload",
        "key",
    }
    assert envelope["eventId"] == body["eventId"]
    assert envelope["type"] == "booking.confirmed"
    assert envelope["payload"] == {"bookingId": 42}
    assert envelope["key"] == "booking-42"
    assert record["headers"] == [
        ("eventId", str(envelope["eventId"]).encode("utf-8")),
        ("type", b"booking.confirmed"),
    ]


def test_returns_202_with_the_delivery_position(client) -> None:
    fake = _FakePublisher(topic="tickets.events", partition=2, offset=77)

    with _use_publisher(fake):
        response = client.post(
            EVENTS_URL,
            json={"type": "booking.confirmed", "payload": {"bookingId": 42}},
            headers=_service_headers(),
        )

    assert response.status_code == 202
    body = response.json()
    assert set(body) == EVENT_KEYS
    assert uuid.UUID(body["eventId"])
    assert body["topic"] == "tickets.events"
    assert body["partition"] == 2
    assert body["offset"] == 77
    assert fake.calls == [
        {
            "event_type": "booking.confirmed",
            "payload": {"bookingId": 42},
            "version": 1,
            "key": None,
        }
    ]


def test_omitted_key_and_version_use_the_documented_defaults(client) -> None:
    producer = _RecordingProducer()
    publisher = EventPublisher(_settings(), producer=producer)

    with _use_publisher(publisher):
        response = client.post(
            EVENTS_URL,
            json={"type": "movie.created", "payload": {"movieId": 1}},
            headers=_service_headers(),
        )

    assert response.status_code == 202
    record = producer.records[0]
    assert record["key"] is None
    envelope = _envelope_of(record)
    assert envelope["version"] == 1
    assert "key" not in envelope


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
    fake = _FakePublisher()

    with _use_publisher(fake):
        response = client.post(EVENTS_URL, json=body, headers=_service_headers())

    assert response.status_code == 422
    problem = response.json()
    assert set(problem) == PROBLEM_DETAIL_KEYS
    assert problem["type"] == "about:blank"
    assert problem["status"] == 422
    assert problem["instance"] == EVENTS_URL
    assert fake.calls == []


def test_broker_down_returns_503(client) -> None:
    fake = _FakePublisher(outcome="unavailable")

    with _use_publisher(fake):
        response = client.post(
            EVENTS_URL,
            json={"type": "booking.confirmed", "payload": {"bookingId": 42}},
            headers=_service_headers(),
        )

    assert response.status_code == 503
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Service Unavailable"
    assert body["status"] == 503
    assert body["detail"] == KAFKA_UNAVAILABLE_DETAIL
    assert body["instance"] == EVENTS_URL
    assert len(fake.calls) == 1


def test_publishing_requires_the_service_token(client) -> None:
    fake = _FakePublisher()
    payload = {"type": "booking.confirmed", "payload": {"bookingId": 42}}

    with _use_publisher(fake):
        missing = client.post(EVENTS_URL, json=payload)
        wrong = client.post(
            EVENTS_URL, json=payload, headers=_service_headers("not-the-token")
        )

    for response in (missing, wrong):
        assert response.status_code == 401
        body = response.json()
        assert set(body) == PROBLEM_DETAIL_KEYS
        assert body["type"] == "about:blank"
        assert body["title"] == "Unauthorized"
        assert body["status"] == 401
        assert body["detail"] == "Invalid service token"
        assert body["instance"] == EVENTS_URL

    assert fake.calls == []


def test_the_events_route_is_in_the_openapi_paths(client) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert "post" in paths[EVENTS_URL]
    assert paths[EVENTS_URL]["post"]["responses"]["202"]


def test_one_shared_publisher_serves_every_request(client) -> None:
    """The lifespan builds one publisher; both requests publish through that object."""
    built = client.app.state.event_publisher
    assert isinstance(built, EventPublisher)
    assert built.settings == app.state.kafka_settings

    producer = _RecordingProducer(partition=1, offset=5)
    shared = EventPublisher(app.state.kafka_settings, producer=producer)
    app.state.event_publisher = shared
    try:
        first = client.post(
            EVENTS_URL,
            json={"type": "booking.confirmed", "payload": {"bookingId": 1}},
            headers=_service_headers(),
        )
        second = client.post(
            EVENTS_URL,
            json={"type": "booking.confirmed", "payload": {"bookingId": 2}},
            headers=_service_headers(),
        )
        assert app.state.event_publisher is shared
    finally:
        app.state.event_publisher = built

    assert first.status_code == 202
    assert second.status_code == 202
    assert len(producer.records) == 2
    assert {_envelope_of(record)["eventId"] for record in producer.records} == {
        first.json()["eventId"],
        second.json()["eventId"],
    }


def test_app_starts_and_answers_503_when_no_publisher_can_be_built(
    client, monkeypatch
) -> None:
    monkeypatch.setattr("app.main.build_event_publisher", lambda settings: None)

    with TestClient(app) as fresh_client:
        response = fresh_client.post(
            EVENTS_URL,
            json={"type": "booking.confirmed", "payload": {"bookingId": 42}},
            headers=_service_headers(),
        )

    assert response.status_code == 503
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Service Unavailable"
    assert body["status"] == 503
    assert body["detail"] == KAFKA_UNAVAILABLE_DETAIL
    assert body["instance"] == EVENTS_URL


def test_unreachable_broker_answers_503_through_the_real_client(client) -> None:
    """librdkafka itself cannot reach the broker, so the flush times out into a 503."""
    publisher = EventPublisher(
        KafkaSettings(
            bootstrap_servers="127.0.0.1:1",
            events_topic="bookmyshow.events",
            client_id="bookmyshow-api-test",
            flush_timeout_ms=300,
        )
    )
    try:
        with _use_publisher(publisher):
            response = client.post(
                EVENTS_URL,
                json={"type": "booking.confirmed", "payload": {"bookingId": 42}},
                headers=_service_headers(),
            )
    finally:
        publisher.close()

    assert response.status_code == 503
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Service Unavailable"
    assert body["status"] == 503
    assert body["detail"] == KAFKA_UNAVAILABLE_DETAIL
    assert body["instance"] == EVENTS_URL
