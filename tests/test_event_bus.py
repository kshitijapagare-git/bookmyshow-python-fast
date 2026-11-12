"""The event bus: envelope validation, routing, handler isolation and its HTTP surface.

Both brokers are replaced by recording publishers -- the same trick
tests/test_events_kafka.py and tests/test_events_rabbit.py use -- so every eventId,
type, payload and version the bus forwards is assertable without a broker. The HTTP
tests inject a bus through `app.dependency_overrides[get_event_bus]`, exactly as
tests/test_groups.py injects a fake payments client, and the override is always
cleared again in the fixture's teardown.
"""

import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from typing import get_args

import pytest
from pydantic import ValidationError

from app.delivery import Transport
from app.event_bus import (
    DEFAULT_EVENT_BUS_TRANSPORT,
    EVENT_BUS_TRANSPORTS,
    WILDCARD_EVENT_TYPE,
    EventBus,
    EventBusSettings,
    EventBusTransport,
    EventEnvelope,
    HandlerError,
    build_event_bus,
    get_event_bus,
)
from app.kafka_producer import (
    KAFKA_UNAVAILABLE_DETAIL,
    DeliveryResult,
    KafkaUnavailableError,
)
from app.main import app, create_app
from app.rabbit_publisher import (
    RABBIT_UNAVAILABLE_DETAIL,
    RabbitDeliveryResult,
    RabbitUnavailableError,
)
from app.security import BOOKINGS_SERVICE_TOKEN

EVENTS_URL = "/api/events"
SUBSCRIPTIONS_URL = "/api/events/subscriptions"
DELIVERIES_URL = "/api/events/deliveries"
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
SERVICE_HEADERS: dict[str, str] = {"X-Service-Token": BOOKINGS_SERVICE_TOKEN}
EVENT_BODY = {"type": "booking.confirmed", "payload": {"bookingId": 42}}
FIXED_NOW = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


class _RecordingKafkaPublisher:
    """Stand-in for EventPublisher: records each publish and answers as told."""

    def __init__(self, *, outcome: str = "succeed") -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    def publish(
        self,
        *,
        event_type: str,
        payload: dict[str, object],
        version: int = 1,
        key: str | None = None,
        event_id: str | None = None,
    ) -> DeliveryResult:
        self.calls.append(
            {
                "event_type": event_type,
                "payload": payload,
                "version": version,
                "key": key,
                "event_id": event_id,
            }
        )
        if self.outcome == "unavailable":
            raise KafkaUnavailableError()
        return DeliveryResult(
            event_id=str(event_id),
            topic="bookmyshow.events",
            partition=1,
            offset=len(self.calls),
        )


class _RecordingRabbitPublisher:
    """Stand-in for RabbitPublisher: records each publish and answers as told."""

    def __init__(self, *, outcome: str = "succeed") -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

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
                "event_id": event_id,
            }
        )
        if self.outcome == "unavailable":
            raise RabbitUnavailableError()
        return RabbitDeliveryResult(
            event_id=str(event_id),
            exchange="bookmyshow.events",
            routing_key="bookmyshow.booking.confirmed",
        )


@pytest.fixture()
def recording_handler() -> Callable[..., tuple[object, list[EventEnvelope]]]:
    """Build a handler plus its call log; `error` makes it raise for the isolation test."""

    def _make(
        name: str = "recording_handler", error: Exception | None = None
    ) -> tuple[object, list[EventEnvelope]]:
        calls: list[EventEnvelope] = []

        def handler(event: EventEnvelope) -> None:
            calls.append(event)
            if error is not None:
                raise error

        handler.__name__ = name
        return handler, calls

    return _make


@pytest.fixture()
def bus_override() -> Iterator[Callable[[object], object]]:
    """Point the events router at a given bus, and always clear the override after."""

    def _override(bus: object) -> object:
        app.dependency_overrides[get_event_bus] = lambda: bus
        return bus

    try:
        yield _override
    finally:
        app.dependency_overrides.pop(get_event_bus, None)


def test_envelope_fills_the_generated_defaults() -> None:
    envelope = EventEnvelope(type="booking.confirmed", payload={"bookingId": 42})

    assert uuid.UUID(envelope.id)
    assert envelope.id.strip() != ""
    assert envelope.type == "booking.confirmed"
    assert envelope.version == 1
    assert envelope.occurredAt.utcoffset() == timedelta(0)
    assert envelope.payload == {"bookingId": 42}
    assert envelope.metadata == {}

    # Two envelopes never share a generated id.
    assert EventEnvelope(type="movie.created", payload={}).id != envelope.id


def test_envelope_keeps_what_the_caller_supplies() -> None:
    envelope = EventEnvelope(
        id="e-1",
        type="  booking.confirmed  ",
        version=3,
        occurredAt=FIXED_NOW,
        payload={"bookingId": 42},
        metadata={"source": "test"},
    )

    assert envelope.id == "e-1"
    assert envelope.type == "booking.confirmed"
    assert envelope.version == 3
    assert envelope.occurredAt == FIXED_NOW
    assert envelope.metadata == {"source": "test"}


@pytest.mark.parametrize(
    "fields",
    [
        {"type": "booking.confirmed", "payload": {}, "id": "   "},
        {"type": "", "payload": {}},
        {"type": "   ", "payload": {}},
        {"type": "booking.confirmed", "payload": {}, "version": 0},
        {"type": "booking.confirmed", "payload": {}, "version": -1},
        {"type": "booking.confirmed"},
        {"type": "booking.confirmed", "payload": ["not", "an", "object"]},
        {"type": "booking.confirmed", "payload": {}, "metadata": ["nope"]},
        {"type": "booking.confirmed", "payload": {}, "occurredAt": "not-a-timestamp"},
    ],
)
def test_envelope_rejects_invalid_fields(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(**fields)  # type: ignore[arg-type]


def test_the_bus_vocabulary_is_the_three_agreed_transports() -> None:
    assert EVENT_BUS_TRANSPORTS == ("MEMORY", "KAFKA", "RABBIT")
    assert get_args(EventBusTransport) == ("MEMORY", "KAFKA", "RABBIT")
    assert DEFAULT_EVENT_BUS_TRANSPORT == "MEMORY"
    assert WILDCARD_EVENT_TYPE == "*"

    # The delivery record's own vocabulary stays KAFKA/RABBIT, untouched by the bus.
    assert get_args(Transport) == ("KAFKA", "RABBIT")


def test_settings_fall_back_to_memory(monkeypatch) -> None:
    monkeypatch.delenv("EVENT_BUS_TRANSPORT", raising=False)
    assert EventBusSettings.from_env() == EventBusSettings(transport="MEMORY")

    monkeypatch.setenv("EVENT_BUS_TRANSPORT", "   ")
    assert EventBusSettings.from_env().transport == "MEMORY"

    monkeypatch.setenv("EVENT_BUS_TRANSPORT", "gcp")
    assert EventBusSettings.from_env().transport == "MEMORY"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("memory", "MEMORY"),
        ("kafka", "KAFKA"),
        ("KAFKA", "KAFKA"),
        ("rabbit", "RABBIT"),
        ("RABBIT", "RABBIT"),
    ],
)
def test_settings_read_the_environment(monkeypatch, raw, expected) -> None:
    monkeypatch.setenv("EVENT_BUS_TRANSPORT", raw)

    assert EventBusSettings.from_env() == EventBusSettings(transport=expected)


def test_build_event_bus_uses_the_settings_transport() -> None:
    bus = build_event_bus(settings=EventBusSettings(transport="KAFKA"))

    assert bus.transport == "KAFKA"
    assert bus.subscriptions() == ()


def test_exact_type_routing_runs_every_subscriber(recording_handler) -> None:
    bus = EventBus(transport="MEMORY")
    first, first_calls = recording_handler("first_handler")
    second, second_calls = recording_handler("second_handler")
    unrelated, unrelated_calls = recording_handler("unrelated_handler")
    bus.subscribe("booking.confirmed", first)
    bus.subscribe("booking.confirmed", second)
    bus.subscribe("movie.created", unrelated)

    result = bus.publish(
        EventEnvelope(type="booking.confirmed", payload={"bookingId": 42})
    )

    assert len(first_calls) == 1
    assert len(second_calls) == 1
    assert unrelated_calls == []
    assert result.event_id == first_calls[0].id
    assert result.handler_errors == ()


def test_wildcard_runs_for_every_type_after_the_exact_handlers() -> None:
    bus = EventBus(transport="MEMORY")
    order: list[str] = []

    def on_booking(event: EventEnvelope) -> None:
        order.append("exact")

    def on_any(event: EventEnvelope) -> None:
        order.append("wildcard")

    bus.subscribe("booking.confirmed", on_booking)
    bus.subscribe(WILDCARD_EVENT_TYPE, on_any)

    bus.publish(EventEnvelope(type="booking.confirmed", payload={"bookingId": 42}))
    bus.publish(EventEnvelope(type="movie.created", payload={"movieId": 1}))

    assert order == ["exact", "wildcard", "wildcard"]


def test_a_type_with_no_subscription_publishes_normally() -> None:
    bus = EventBus(transport="MEMORY")
    envelope = EventEnvelope(type="nothing.listens", payload={"id": 1})

    result = bus.publish(envelope)

    assert result.event_id == envelope.id
    assert result.handler_errors == ()


def test_a_failing_handler_is_reported_and_the_rest_still_run(
    recording_handler,
) -> None:
    bus = EventBus(transport="MEMORY")
    exploding, exploding_calls = recording_handler(
        "exploding_handler", RuntimeError("handler exploded")
    )
    survivor, survivor_calls = recording_handler("surviving_handler")
    bus.subscribe("booking.confirmed", exploding)
    bus.subscribe("booking.confirmed", survivor)

    result = bus.publish(
        EventEnvelope(type="booking.confirmed", payload={"bookingId": 42})
    )

    assert len(exploding_calls) == 1
    assert len(survivor_calls) == 1
    assert result.handler_errors == (
        HandlerError(
            event_type="booking.confirmed",
            handler="exploding_handler",
            error="handler exploded",
        ),
    )


def test_a_blank_error_message_is_reported_as_the_exception_type(
    recording_handler,
) -> None:
    bus = EventBus(transport="MEMORY")
    exploding, _ = recording_handler("quiet_handler", RuntimeError(""))
    bus.subscribe("booking.confirmed", exploding)

    result = bus.publish(EventEnvelope(type="booking.confirmed", payload={}))

    assert [error.error for error in result.handler_errors] == ["RuntimeError"]


@pytest.mark.parametrize("event_type", ["", "   "])
def test_subscribe_rejects_a_blank_event_type(event_type) -> None:
    bus = EventBus(transport="MEMORY")

    with pytest.raises(ValueError):
        bus.subscribe(event_type, lambda event: None)

    assert bus.subscriptions() == ()


def test_memory_transport_calls_no_publisher(recording_handler) -> None:
    kafka = _RecordingKafkaPublisher()
    rabbit = _RecordingRabbitPublisher()
    bus = EventBus(transport="MEMORY", kafka_publisher=kafka, rabbit_publisher=rabbit)
    handler, calls = recording_handler("memory_handler")
    bus.subscribe("booking.confirmed", handler)

    result = bus.publish(
        EventEnvelope(type="booking.confirmed", payload={"bookingId": 42})
    )

    assert kafka.calls == []
    assert rabbit.calls == []
    assert len(calls) == 1
    assert result.handler_errors == ()


def test_kafka_transport_forwards_the_envelope_then_runs_handlers(
    recording_handler,
) -> None:
    publisher = _RecordingKafkaPublisher()
    bus = EventBus(transport="KAFKA", kafka_publisher=publisher)
    handler, calls = recording_handler("kafka_handler")
    bus.subscribe("booking.confirmed", handler)
    envelope = EventEnvelope(
        type="booking.confirmed", version=2, payload={"bookingId": 42}
    )

    result = bus.publish(envelope)

    assert publisher.calls == [
        {
            "event_type": "booking.confirmed",
            "payload": {"bookingId": 42},
            "version": 2,
            "key": None,
            "event_id": envelope.id,
        }
    ]
    assert [recorded.id for recorded in calls] == [envelope.id]
    assert result.event_id == envelope.id


def test_rabbit_transport_forwards_the_envelope_then_runs_handlers(
    recording_handler,
) -> None:
    publisher = _RecordingRabbitPublisher()
    bus = EventBus(transport="RABBIT", rabbit_publisher=publisher)
    handler, calls = recording_handler("rabbit_handler")
    bus.subscribe("booking.confirmed", handler)
    envelope = EventEnvelope(type="booking.confirmed", payload={"bookingId": 42})

    result = bus.publish(envelope)

    assert publisher.calls == [
        {
            "event_type": "booking.confirmed",
            "payload": {"bookingId": 42},
            "version": 1,
            "key": None,
            "event_id": envelope.id,
        }
    ]
    assert [recorded.id for recorded in calls] == [envelope.id]
    assert result.event_id == envelope.id


def test_a_missing_kafka_publisher_raises_and_runs_no_handler(
    recording_handler,
) -> None:
    bus = EventBus(transport="KAFKA")
    handler, calls = recording_handler("never_handler")
    bus.subscribe("booking.confirmed", handler)

    with pytest.raises(KafkaUnavailableError):
        bus.publish(EventEnvelope(type="booking.confirmed", payload={"bookingId": 42}))

    assert calls == []


def test_a_missing_rabbit_publisher_raises_and_runs_no_handler(
    recording_handler,
) -> None:
    bus = EventBus(transport="RABBIT")
    handler, calls = recording_handler("never_handler")
    bus.subscribe("booking.confirmed", handler)

    with pytest.raises(RabbitUnavailableError):
        bus.publish(EventEnvelope(type="booking.confirmed", payload={"bookingId": 42}))

    assert calls == []


def test_subscriptions_keep_registration_order_and_names() -> None:
    bus = EventBus(transport="MEMORY")

    def on_booking(event: EventEnvelope) -> None: ...

    def on_any(event: EventEnvelope) -> None: ...

    def on_movie(event: EventEnvelope) -> None: ...

    bus.subscribe("booking.confirmed", on_booking)
    bus.subscribe(WILDCARD_EVENT_TYPE, on_any)
    bus.subscribe("movie.created", on_movie)

    subscriptions = bus.subscriptions()

    assert [(entry.event_type, entry.name) for entry in subscriptions] == [
        ("booking.confirmed", "on_booking"),
        ("*", "on_any"),
        ("movie.created", "on_movie"),
    ]
    assert [entry.handler for entry in subscriptions] == [on_booking, on_any, on_movie]


def test_bind_publishers_keeps_existing_subscriptions(recording_handler) -> None:
    bus = EventBus(transport="KAFKA")
    handler, calls = recording_handler("bound_handler")
    bus.subscribe("booking.confirmed", handler)
    publisher = _RecordingKafkaPublisher()

    bus.bind_publishers(kafka_publisher=publisher, rabbit_publisher=None)

    assert [entry.name for entry in bus.subscriptions()] == ["bound_handler"]

    result = bus.publish(
        EventEnvelope(type="booking.confirmed", payload={"bookingId": 42})
    )

    assert len(publisher.calls) == 1
    assert len(calls) == 1
    assert result.handler_errors == ()


def test_publish_route_returns_202_with_the_envelope_id(
    client, bus_override, recording_handler
) -> None:
    bus = EventBus(transport="MEMORY")
    handler, calls = recording_handler("recording_handler")
    bus.subscribe("booking.confirmed", handler)
    bus_override(bus)

    response = client.post(EVENTS_URL, json=EVENT_BODY, headers=SERVICE_HEADERS)

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"id", "handlerErrors"}
    assert body["handlerErrors"] == []
    assert uuid.UUID(body["id"])

    assert len(calls) == 1
    envelope = calls[0]
    assert body["id"] == envelope.id
    assert envelope.type == "booking.confirmed"
    assert envelope.version == 1
    assert envelope.payload == {"bookingId": 42}
    assert envelope.metadata == {}

    # The bus publish is not a delivery record, so nothing was written for this id.
    missing = client.get(f"{DELIVERIES_URL}/{body['id']}", headers=SERVICE_HEADERS)
    assert missing.status_code == 404
    assert missing.json()["title"] == "Not Found"


def test_publish_route_of_an_unsubscribed_type_answers_202(
    client, bus_override, recording_handler
) -> None:
    bus = EventBus(transport="MEMORY")
    handler, calls = recording_handler("booking_only_handler")
    bus.subscribe("booking.confirmed", handler)
    bus_override(bus)

    response = client.post(
        EVENTS_URL,
        json={"type": "movie.created", "payload": {"movieId": 1}},
        headers=SERVICE_HEADERS,
    )

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"id", "handlerErrors"}
    assert body["handlerErrors"] == []
    assert calls == []


def test_publish_route_reports_handler_failures_and_still_answers_202(
    client, bus_override, recording_handler
) -> None:
    bus = EventBus(transport="MEMORY")
    exploding, _ = recording_handler(
        "exploding_handler", RuntimeError("handler exploded")
    )
    survivor, survivor_calls = recording_handler("surviving_handler")
    bus.subscribe("booking.confirmed", exploding)
    bus.subscribe("booking.confirmed", survivor)
    bus_override(bus)

    response = client.post(EVENTS_URL, json=EVENT_BODY, headers=SERVICE_HEADERS)

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"id", "handlerErrors"}
    assert len(survivor_calls) == 1
    assert body["handlerErrors"] == [
        {
            "eventType": "booking.confirmed",
            "handler": "exploding_handler",
            "error": "handler exploded",
        }
    ]
    assert set(body["handlerErrors"][0]) == {"eventType", "handler", "error"}


@pytest.mark.parametrize(
    "body",
    [
        {"type": "booking.confirmed"},
        {"type": "", "payload": {}},
        {"type": "   ", "payload": {}},
        {"type": "booking.confirmed", "payload": {}, "version": 0},
        {"type": "booking.confirmed", "payload": ["not", "an", "object"]},
        {"type": "booking.confirmed", "payload": {}, "metadata": ["nope"]},
    ],
)
def test_publish_route_rejects_an_invalid_body(
    client, bus_override, recording_handler, body
) -> None:
    bus = EventBus(transport="MEMORY")
    handler, calls = recording_handler("never_handler")
    bus.subscribe(WILDCARD_EVENT_TYPE, handler)
    bus_override(bus)

    response = client.post(EVENTS_URL, json=body, headers=SERVICE_HEADERS)

    assert response.status_code == 422
    problem = response.json()
    assert set(problem) == PROBLEM_DETAIL_KEYS
    assert problem["type"] == "about:blank"
    assert problem["status"] == 422
    assert problem["instance"] == EVENTS_URL
    assert calls == []


def test_publish_route_answers_503_when_the_transport_has_no_publisher(
    client, bus_override
) -> None:
    bus_override(EventBus(transport="KAFKA"))

    kafka = client.post(EVENTS_URL, json=EVENT_BODY, headers=SERVICE_HEADERS)

    assert kafka.status_code == 503
    kafka_body = kafka.json()
    assert set(kafka_body) == PROBLEM_DETAIL_KEYS
    assert kafka_body["type"] == "about:blank"
    assert kafka_body["title"] == "Service Unavailable"
    assert kafka_body["status"] == 503
    assert kafka_body["detail"] == KAFKA_UNAVAILABLE_DETAIL
    assert kafka_body["instance"] == EVENTS_URL

    app.dependency_overrides[get_event_bus] = lambda: EventBus(transport="RABBIT")

    rabbit = client.post(EVENTS_URL, json=EVENT_BODY, headers=SERVICE_HEADERS)

    assert rabbit.status_code == 503
    rabbit_body = rabbit.json()
    assert set(rabbit_body) == PROBLEM_DETAIL_KEYS
    assert rabbit_body["title"] == "Service Unavailable"
    assert rabbit_body["status"] == 503
    assert rabbit_body["detail"] == RABBIT_UNAVAILABLE_DETAIL
    assert rabbit_body["instance"] == EVENTS_URL


def test_subscriptions_route_lists_every_registration(client, bus_override) -> None:
    bus = EventBus(transport="MEMORY")

    def on_booking(event: EventEnvelope) -> None: ...

    def on_any(event: EventEnvelope) -> None: ...

    bus.subscribe("booking.confirmed", on_booking)
    bus.subscribe(WILDCARD_EVENT_TYPE, on_any)
    bus_override(bus)

    response = client.get(SUBSCRIPTIONS_URL, headers=SERVICE_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"transport", "subscriptions"}
    assert body["transport"] == "MEMORY"
    assert body["subscriptions"] == [
        {"eventType": "booking.confirmed", "handler": "on_booking"},
        {"eventType": "*", "handler": "on_any"},
    ]

    # The reported transport is the bus's own, so a broker-selected bus says so.
    app.dependency_overrides[get_event_bus] = lambda: EventBus(transport="KAFKA")

    selected = client.get(SUBSCRIPTIONS_URL, headers=SERVICE_HEADERS)

    assert selected.status_code == 200
    assert selected.json() == {"transport": "KAFKA", "subscriptions": []}


def test_bus_routes_require_the_service_token(client) -> None:
    for headers in (None, {"X-Service-Token": "not-the-token"}):
        publish = client.post(EVENTS_URL, json=EVENT_BODY, headers=headers)
        listing = client.get(SUBSCRIPTIONS_URL, headers=headers)

        for response, url in (
            (publish, EVENTS_URL),
            (listing, SUBSCRIPTIONS_URL),
        ):
            assert response.status_code == 401
            problem = response.json()
            assert set(problem) == PROBLEM_DETAIL_KEYS
            assert problem["type"] == "about:blank"
            assert problem["title"] == "Unauthorized"
            assert problem["status"] == 401
            assert problem["instance"] == url


def test_create_app_selects_the_configured_transport(monkeypatch) -> None:
    monkeypatch.delenv("EVENT_BUS_TRANSPORT", raising=False)

    assert create_app().state.event_bus.transport == "MEMORY"

    monkeypatch.setenv("EVENT_BUS_TRANSPORT", "kafka")
    configured = create_app()

    assert configured.state.event_bus_settings == EventBusSettings(transport="KAFKA")
    assert configured.state.event_bus.transport == "KAFKA"
