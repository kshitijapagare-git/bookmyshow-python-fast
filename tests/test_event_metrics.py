"""Event metrics, health, the recent-event log and payload-free structured logs.

The broker clients are replaced by recorders -- the same trick tests/test_event_delivery.py
and tests/test_events_consumer.py use -- so every counter, every health probe and every log
record is assertable without Kafka, RabbitMQ or a running broker. `/api/events/log` reads
the `event_deliveries` rows the delivery tests already exercise, so the rows here are
inserted straight into the database instead of being published through a broker.

The counter registry lives on `app.state` for the whole process lifetime, so an autouse
fixture resets it (and restores the swapped metrics settings) around every test.
"""

import json
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app.db import Base, SessionLocal, engine
from app.delivery import DELIVERY_STATUSES, DeliveryPolicy, EventDeliveryService
from app.event_metrics import (
    DEFAULT_EVENT_LOG_LIMIT,
    DEFAULT_EVENT_METRICS_FORMAT,
    DEFAULT_HEALTH_TIMEOUT_S,
    EventMetrics,
    EventMetricsSettings,
    MetricsBucket,
)
from app.kafka_consumer import (
    DEFAULT_KAFKA_AUTO_OFFSET_RESET,
    ConsumedEvent,
    HandlerRegistry,
    KafkaConsumerSettings,
    KafkaEventConsumer,
)
from app.kafka_producer import (
    DeliveryResult,
    EventPublisher,
    KafkaSettings,
    KafkaUnavailableError,
    get_event_publisher,
)
from app.main import app
from app.models import EventDelivery
from app.rabbit_publisher import RabbitPublisher, RabbitSettings
from app.security import BOOKINGS_SERVICE_TOKEN

EVENTS_KAFKA_URL = "/api/events/kafka"
METRICS_URL = "/api/events/metrics"
HEALTH_URL = "/api/events/health"
LOG_URL = "/api/events/log"
SERVICE_HEADERS: dict[str, str] = {"X-Service-Token": BOOKINGS_SERVICE_TOKEN}
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
BUCKET_KEYS = {
    "type",
    "transport",
    "published",
    "consumed",
    "failed",
    "retried",
    "deadLettered",
}
LOG_ENTRY_KEYS = {"eventId", "type", "transport", "status", "timestamp"}
EVENT_BODY = {"type": "booking.confirmed", "payload": {"bookingId": 42}}
TOPIC = "bookmyshow.events"
GROUP = "bookmyshow-api"
FIXED_NOW = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def reset_metrics() -> Iterator[None]:
    """Zero the process-lifetime counters before each test and restore the settings."""
    registry = app.state.event_metrics
    settings = app.state.event_metrics_settings
    registry.reset()
    try:
        yield
    finally:
        registry.reset()
        app.state.event_metrics_settings = settings


class _DeliveryMessage:
    """The two accessors the Kafka publisher reads off confluent_kafka.Message."""

    def __init__(self, partition: int, offset: int) -> None:
        self._partition = partition
        self._offset = offset

    def partition(self) -> int:
        return self._partition

    def offset(self) -> int:
        return self._offset


class _RecordingProducer:
    """Stand-in for confluent_kafka.Producer: records each record and answers as told.

    `list_topics` makes the publisher's health probe a real round trip that the test
    can drive up or down.
    """

    def __init__(
        self,
        *,
        healthy: bool = True,
        partition: int = 3,
        offset: int = 42,
    ) -> None:
        self.healthy = healthy
        self.partition = partition
        self.offset = offset
        self.records: list[dict[str, object]] = []
        self.health_timeouts: list[float | None] = []
        self._on_delivery = None

    def produce(self, topic, *, value=None, key=None, headers=None, on_delivery=None):
        self.records.append(
            {"topic": topic, "value": value, "key": key, "headers": headers}
        )
        self._on_delivery = on_delivery

    def poll(self, timeout):
        return 0

    def flush(self, timeout):
        if self._on_delivery is not None:
            self._on_delivery(None, _DeliveryMessage(self.partition, self.offset))
        return 0

    def list_topics(self, *, timeout: float | None = None):
        self.health_timeouts.append(timeout)
        if not self.healthy:
            raise RuntimeError("no brokers available")
        return object()


class _RecordingChannel:
    """Stand-in for a pika channel: records the declare and each published message."""

    def __init__(self, *, declare_error: Exception | None = None) -> None:
        self.declare_error = declare_error
        self.declares: list[dict[str, object]] = []
        self.messages: list[dict[str, object]] = []
        self.closed = False

    def exchange_declare(
        self, *, exchange: str, exchange_type: str, durable: bool
    ) -> None:
        if self.declare_error is not None:
            raise self.declare_error
        self.declares.append(
            {"exchange": exchange, "exchange_type": exchange_type, "durable": durable}
        )

    def basic_publish(self, exchange, routing_key, body, properties) -> None:
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


class _FlakyPublisher:
    """Stand-in for a broker publisher: fails its first N calls, then answers a result."""

    def __init__(self, *, failures: int = 0, error: BaseException | None = None) -> None:
        self.failures = failures
        self.error = error if error is not None else KafkaUnavailableError()
        self.publish_calls = 0
        self.dead_letters: list[dict[str, object]] = []

    def publish(
        self,
        *,
        event_type: str,
        payload: dict[str, object],
        version: int = 1,
        key: str | None = None,
        event_id: str | None = None,
    ) -> DeliveryResult:
        self.publish_calls += 1
        if self.publish_calls <= self.failures:
            raise self.error
        return DeliveryResult(
            event_id=str(event_id),
            topic=TOPIC,
            partition=1,
            offset=self.publish_calls,
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
    ) -> DeliveryResult:
        self.dead_letters.append(
            {"event_id": event_id, "attempts": attempts, "last_error": last_error}
        )
        return DeliveryResult(
            event_id=event_id, topic=f"{TOPIC}.dlq", partition=1, offset=99
        )


class _FakeSleep:
    """Stand-in for time.sleep: records each delay instead of waiting for it."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _RecordingConsumer:
    """Stand-in for confluent_kafka.Consumer: polls a queue and records each commit."""

    def __init__(self, messages: list[object] | None = None) -> None:
        self.messages: list[object] = list(messages or [])
        self.subscribed: list[list[str]] = []
        self.commits: list[object] = []
        self.closed = False

    def subscribe(self, topics: list[str]) -> None:
        self.subscribed.append(list(topics))

    def poll(self, timeout: float) -> object | None:
        if self.messages:
            return self.messages.pop(0)
        return None

    def commit(self, message: object | None = None) -> None:
        self.commits.append(message)

    def close(self) -> None:
        self.closed = True


class _FakeMessage:
    """Stand-in for a confluent_kafka.Message: the two accessors `process` reads."""

    def __init__(self, body: dict[str, object]) -> None:
        self._raw = json.dumps(body).encode("utf-8")

    def value(self) -> bytes | None:
        return self._raw

    def headers(self) -> list[tuple[str, bytes]] | None:
        return None


def _policy(
    *,
    max_attempts: int = 3,
    base_delay_ms: int = 100,
    max_delay_ms: int = 2000,
    jitter: float = 0.0,
) -> DeliveryPolicy:
    """A jitter-free policy unless the test pins one of the four knobs."""
    return DeliveryPolicy(
        max_attempts=max_attempts,
        base_delay_ms=base_delay_ms,
        max_delay_ms=max_delay_ms,
        jitter=jitter,
    )


def _kafka_settings() -> KafkaSettings:
    """Settings pointed at a broker that is never contacted in these tests."""
    return KafkaSettings(
        bootstrap_servers="broker.test:9092",
        events_topic=TOPIC,
        client_id="bookmyshow-api",
        flush_timeout_ms=5000,
    )


def _rabbit_settings() -> RabbitSettings:
    """Settings pointed at a broker that is never contacted in these tests."""
    return RabbitSettings(
        url="amqp://guest:guest@broker.test:5672/",
        exchange="bookmyshow.events",
        routing_key_prefix="bookmyshow",
    )


def _message(
    event_id: str = "e-1",
    event_type: str = "booking.confirmed",
    payload: dict[str, object] | None = None,
) -> _FakeMessage:
    """The envelope the producer writes, wrapped as a consumable record."""
    return _FakeMessage(
        {
            "eventId": event_id,
            "type": event_type,
            "version": 1,
            "occurredAt": FIXED_NOW.isoformat(),
            "payload": {"bookingId": 42} if payload is None else payload,
        }
    )


def _build_consumer(
    *,
    registry: HandlerRegistry,
    messages: list[object],
    metrics: EventMetrics,
    policy: DeliveryPolicy | None = None,
    publisher: object | None = None,
) -> tuple[KafkaEventConsumer, _RecordingConsumer]:
    """A consumer driven by a recorder, with no delay and a fixed clock."""
    client = _RecordingConsumer(messages)
    consumer = KafkaEventConsumer(
        settings=_kafka_settings(),
        consumer_settings=KafkaConsumerSettings(
            group=GROUP, auto_offset_reset=DEFAULT_KAFKA_AUTO_OFFSET_RESET
        ),
        registry=registry,
        publisher=publisher,
        policy=policy if policy is not None else _policy(),
        metrics=metrics,
        consumer=client,
        sleep=_FakeSleep(),
        rng=lambda: 0.5,
        now=lambda: FIXED_NOW,
    )
    return consumer, client


def _bucket_of(
    registry: EventMetrics, event_type: str, transport: str
) -> MetricsBucket:
    """The one bucket for this (type, transport) pair, or a failure naming it."""
    for bucket in registry.snapshot():
        if bucket.event_type == event_type and bucket.transport == transport:
            return bucket
    raise AssertionError(f"no bucket recorded for {event_type}/{transport}")


@contextmanager
def _use_kafka_publisher(publisher: object) -> Iterator[object]:
    app.dependency_overrides[get_event_publisher] = lambda: publisher
    try:
        yield publisher
    finally:
        app.dependency_overrides.pop(get_event_publisher, None)


@contextmanager
def _fast_delivery() -> Iterator[None]:
    """A 1ms, jitter-free policy so a route never waits out the real backoff."""
    built = app.state.delivery_policy
    app.state.delivery_policy = _policy(base_delay_ms=1, max_delay_ms=2, jitter=0.0)
    try:
        yield
    finally:
        app.state.delivery_policy = built


def _publish_kafka(client, producer: object | None = None) -> str:
    """Publish one booking.confirmed through the Kafka route and return its eventId."""
    publisher = EventPublisher(
        _kafka_settings(),
        producer=producer if producer is not None else _RecordingProducer(),
    )
    with _use_kafka_publisher(publisher), _fast_delivery():
        response = client.post(
            EVENTS_KAFKA_URL, json=EVENT_BODY, headers=SERVICE_HEADERS
        )
    assert response.status_code == 202
    return response.json()["eventId"]


def _add_delivery(
    event_type: str, transport: str, status: str, updated_at: datetime
) -> str:
    """Insert one delivery row, so `/log` has a deterministic history to read."""
    event_id = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add(
            EventDelivery(
                event_id=event_id,
                transport=transport,
                event_type=event_type,
                status=status,
                attempts=1,
                created_at=updated_at,
                updated_at=updated_at,
            )
        )
        db.commit()
    return event_id


def test_metrics_counters_increment() -> None:
    """Every transition the ticket names moves exactly its own counter."""
    Base.metadata.create_all(bind=engine)
    registry = app.state.event_metrics

    # Two transient failures then a success: two retries, one publish, nothing parked.
    recovered = _FlakyPublisher(failures=2)
    with SessionLocal() as db:
        outcome = EventDeliveryService(
            publisher=recovered,
            policy=_policy(max_attempts=3),
            db=db,
            transport="KAFKA",
            metrics=registry,
            sleep=_FakeSleep(),
        ).deliver(event_type="booking.confirmed", payload={"bookingId": 42})
    assert outcome.status == "DELIVERED"

    published_bucket = _bucket_of(registry, "booking.confirmed", "KAFKA")
    assert published_bucket.published == 1
    assert published_bucket.failed == 2
    assert published_bucket.retried == 2
    assert published_bucket.dead_lettered == 0
    assert published_bucket.consumed == 0

    # Every attempt fails, so the event is parked on the dead-letter destination.
    exhausted = _FlakyPublisher(failures=10)
    with SessionLocal() as db:
        parked = EventDeliveryService(
            publisher=exhausted,
            policy=_policy(max_attempts=3),
            db=db,
            transport="KAFKA",
            metrics=registry,
            sleep=_FakeSleep(),
        ).deliver(event_type="movie.created", payload={"movieId": 1})
    assert parked.status == "DEAD_LETTERED"

    parked_bucket = _bucket_of(registry, "movie.created", "KAFKA")
    assert parked_bucket.dead_lettered == 1
    assert parked_bucket.failed == 2
    assert parked_bucket.retried == 2
    assert parked_bucket.published == 0

    # A consumed message counts as consumed, never as a delivery state.
    consuming = HandlerRegistry()
    consumed_calls: list[ConsumedEvent] = []

    def on_seat_held(event: ConsumedEvent) -> None:
        consumed_calls.append(event)

    consuming.register("seat.held", on_seat_held)
    consumer, recorder = _build_consumer(
        registry=consuming,
        messages=[_message("e-consume", "seat.held")],
        metrics=registry,
        publisher=_FlakyPublisher(),
    )

    assert consumer.poll_once() == "HANDLED"
    assert len(consumed_calls) == 1
    assert recorder.commits != []

    consumed_bucket = _bucket_of(registry, "seat.held", "KAFKA")
    assert consumed_bucket.consumed == 1
    assert consumed_bucket.published == 0
    assert consumed_bucket.failed == 0
    assert consumed_bucket.retried == 0
    assert consumed_bucket.dead_lettered == 0

    # A handler that never returns parks the event: failed on every retried attempt,
    # dead-lettered once on the last one.
    failing = HandlerRegistry()

    def explode(event: ConsumedEvent) -> None:
        raise RuntimeError("handler exploded")

    failing.register("booking.pending", explode)
    failing_consumer, _ = _build_consumer(
        registry=failing,
        messages=[_message("e-fail", "booking.pending")],
        metrics=registry,
        policy=_policy(max_attempts=3),
        publisher=_FlakyPublisher(),
    )

    assert failing_consumer.poll_once() == "DEAD_LETTERED"
    failed_bucket = _bucket_of(registry, "booking.pending", "KAFKA")
    assert failed_bucket.failed == 2
    assert failed_bucket.retried == 2
    assert failed_bucket.dead_lettered == 1
    assert failed_bucket.consumed == 0

    assert registry.last_event_at is not None


def test_metrics_settings_fall_back(monkeypatch) -> None:
    """EVENT_LOG_LIMIT and EVENT_METRICS_FORMAT fall back instead of raising."""
    monkeypatch.delenv("EVENT_LOG_LIMIT", raising=False)
    monkeypatch.delenv("EVENT_METRICS_FORMAT", raising=False)

    defaults = EventMetricsSettings.from_env()
    assert defaults == EventMetricsSettings(
        log_limit=DEFAULT_EVENT_LOG_LIMIT, format=DEFAULT_EVENT_METRICS_FORMAT
    )
    assert (DEFAULT_EVENT_LOG_LIMIT, DEFAULT_EVENT_METRICS_FORMAT) == (50, "json")

    for raw in ("0", "-5", "abc", "   "):
        monkeypatch.setenv("EVENT_LOG_LIMIT", raw)
        assert EventMetricsSettings.from_env().log_limit == DEFAULT_EVENT_LOG_LIMIT

    monkeypatch.setenv("EVENT_LOG_LIMIT", "7")
    monkeypatch.setenv("EVENT_METRICS_FORMAT", "prometheus")
    assert EventMetricsSettings.from_env() == EventMetricsSettings(
        log_limit=7, format="prometheus"
    )

    monkeypatch.setenv("EVENT_METRICS_FORMAT", "graphite")
    assert EventMetricsSettings.from_env().format == DEFAULT_EVENT_METRICS_FORMAT


def test_metrics_shape(client) -> None:
    """The JSON branch reports one bucket per (type, transport) plus lastEventAt."""
    app.state.event_metrics_settings = EventMetricsSettings(log_limit=50, format="json")
    _publish_kafka(client)

    response = client.get(METRICS_URL, headers=SERVICE_HEADERS)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert set(body) == {"counters", "lastEventAt"}
    assert body["lastEventAt"] is not None
    assert len(body["counters"]) == 1
    bucket = body["counters"][0]
    assert set(bucket) == BUCKET_KEYS
    assert bucket == {
        "type": "booking.confirmed",
        "transport": "KAFKA",
        "published": 1,
        "consumed": 0,
        "failed": 0,
        "retried": 0,
        "deadLettered": 0,
    }


def test_metrics_prometheus_text(client) -> None:
    """EVENT_METRICS_FORMAT=prometheus answers Prometheus text, not JSON."""
    app.state.event_metrics_settings = EventMetricsSettings(
        log_limit=50, format="prometheus"
    )
    _publish_kafka(client)

    response = client.get(METRICS_URL, headers=SERVICE_HEADERS)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")

    body = response.text
    assert "# TYPE event_published_total counter" in body
    assert (
        'event_published_total{type="booking.confirmed",transport="KAFKA"} 1' in body
    )
    assert "event_last_event_at_timestamp_seconds" in body

    with pytest.raises(ValueError):
        json.loads(body)


def test_health_reports_each_component(client) -> None:
    """FastAPI, Kafka and RabbitMQ each read up or down, and the route is always 200."""
    built_kafka = app.state.event_publisher
    built_rabbit = app.state.rabbit_publisher
    producer = _RecordingProducer()
    channel = _RecordingChannel()
    try:
        app.state.event_publisher = EventPublisher(_kafka_settings(), producer=producer)
        app.state.rabbit_publisher = RabbitPublisher(_rabbit_settings(), channel=channel)

        healthy = client.get(HEALTH_URL, headers=SERVICE_HEADERS)

        assert healthy.status_code == 200
        assert set(healthy.json()) == {"fastapi", "kafka", "rabbitmq"}
        assert healthy.json() == {
            "fastapi": "up",
            "kafka": "up",
            "rabbitmq": "up",
        }
        assert producer.health_timeouts == [DEFAULT_HEALTH_TIMEOUT_S]
        assert len(channel.declares) == 1

        app.state.event_publisher = EventPublisher(
            _kafka_settings(), producer=_RecordingProducer(healthy=False)
        )
        app.state.rabbit_publisher = RabbitPublisher(
            _rabbit_settings(),
            channel=_RecordingChannel(declare_error=RuntimeError("AMQP down")),
        )

        unreachable = client.get(HEALTH_URL, headers=SERVICE_HEADERS)

        assert unreachable.status_code == 200
        assert unreachable.json() == {
            "fastapi": "up",
            "kafka": "down",
            "rabbitmq": "down",
        }

        # A host where neither publisher could be built reports both as down.
        app.state.event_publisher = None
        app.state.rabbit_publisher = None

        missing = client.get(HEALTH_URL, headers=SERVICE_HEADERS)

        assert missing.status_code == 200
        assert missing.json() == {
            "fastapi": "up",
            "kafka": "down",
            "rabbitmq": "down",
        }
    finally:
        app.state.event_publisher = built_kafka
        app.state.rabbit_publisher = built_rabbit


def test_log_pagination_and_filtering(client) -> None:
    """`/log` reads the delivery rows newest first, honours limit and filters on type."""
    app.state.event_metrics_settings = EventMetricsSettings(log_limit=50, format="json")
    base = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
    oldest = _add_delivery("booking.confirmed", "KAFKA", "PENDING", base)
    middle = _add_delivery(
        "booking.confirmed", "RABBIT", "DELIVERED", base + timedelta(seconds=1)
    )
    newest = _add_delivery(
        "movie.created", "KAFKA", "DEAD_LETTERED", base + timedelta(seconds=2)
    )

    response = client.get(LOG_URL, headers=SERVICE_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"entries", "limit"}
    assert body["limit"] == app.state.event_metrics_settings.log_limit == 50
    assert [entry["eventId"] for entry in body["entries"]] == [
        newest,
        middle,
        oldest,
    ]
    for entry in body["entries"]:
        assert set(entry) == LOG_ENTRY_KEYS
        assert entry["status"] in DELIVERY_STATUSES
        assert isinstance(entry["timestamp"], str)

    limited = client.get(f"{LOG_URL}?limit=2", headers=SERVICE_HEADERS)

    assert limited.status_code == 200
    assert limited.json()["limit"] == 2
    assert [entry["eventId"] for entry in limited.json()["entries"]] == [
        newest,
        middle,
    ]

    filtered = client.get(
        f"{LOG_URL}?type=booking.confirmed", headers=SERVICE_HEADERS
    )

    assert filtered.status_code == 200
    assert [entry["eventId"] for entry in filtered.json()["entries"]] == [
        middle,
        oldest,
    ]
    assert {entry["type"] for entry in filtered.json()["entries"]} == {
        "booking.confirmed"
    }

    unknown = client.get(f"{LOG_URL}?type=movie.removed", headers=SERVICE_HEADERS)

    assert unknown.status_code == 200
    assert unknown.json()["entries"] == []

    rejected = client.get(f"{LOG_URL}?limit=0", headers=SERVICE_HEADERS)

    assert rejected.status_code == 422
    problem = rejected.json()
    assert set(problem) == PROBLEM_DETAIL_KEYS
    assert problem["type"] == "about:blank"
    assert problem["status"] == 422
    assert problem["instance"] == LOG_URL


def test_log_redacts_payload(client, caplog) -> None:
    """A payload sentinel never reaches `/log` and never reaches a log record."""
    sentinel = "card-4111-1111-1111-1111"
    producer = _RecordingProducer()
    publisher = EventPublisher(_kafka_settings(), producer=producer)

    with caplog.at_level(logging.DEBUG):
        with _use_kafka_publisher(publisher), _fast_delivery():
            published = client.post(
                EVENTS_KAFKA_URL,
                json={
                    "type": "booking.confirmed",
                    "payload": {"cardNumber": sentinel, "bookingId": 42},
                },
                headers=SERVICE_HEADERS,
            )
        assert published.status_code == 202
        event_id = published.json()["eventId"]

        recent = client.get(f"{LOG_URL}?type=booking.confirmed", headers=SERVICE_HEADERS)

    assert recent.status_code == 200
    assert sentinel not in recent.text
    assert event_id in recent.text
    assert set(recent.json()["entries"][0]) == LOG_ENTRY_KEYS

    messages = [record.getMessage() for record in caplog.records]
    assert messages
    assert all(sentinel not in message for message in messages)
    assert any(event_id in message for message in messages)


def test_structured_logs_carry_correlation_id(client, caplog) -> None:
    """Publish and consume records both carry eventId, correlationId and transport."""
    producer = _RecordingProducer()
    publisher = EventPublisher(_kafka_settings(), producer=producer)

    with caplog.at_level(logging.INFO, logger="app.kafka_producer"):
        with _use_kafka_publisher(publisher), _fast_delivery():
            response = client.post(
                EVENTS_KAFKA_URL, json=EVENT_BODY, headers=SERVICE_HEADERS
            )
    assert response.status_code == 202
    event_id = response.json()["eventId"]

    publish_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.kafka_producer"
    ]
    assert len(publish_messages) == 1
    published = publish_messages[0]
    assert f"eventId={event_id}" in published
    assert f"correlationId={event_id}" in published
    assert "transport=KAFKA" in published

    caplog.clear()

    registry = HandlerRegistry()

    def on_booking(event: ConsumedEvent) -> None: ...

    registry.register("booking.confirmed", on_booking)
    consumer, _ = _build_consumer(
        registry=registry,
        messages=[_message("e-consume", "booking.confirmed")],
        metrics=app.state.event_metrics,
        publisher=_FlakyPublisher(),
    )

    with caplog.at_level(logging.INFO, logger="app.kafka_consumer"):
        assert consumer.poll_once() == "HANDLED"

    consume_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.kafka_consumer"
    ]
    assert len(consume_messages) == 1
    consumed = consume_messages[0]
    assert "eventId=e-consume" in consumed
    assert "correlationId=e-consume" in consumed
    assert "transport=KAFKA" in consumed


@pytest.mark.parametrize("url", [METRICS_URL, HEALTH_URL, LOG_URL])
def test_new_routes_require_the_service_token(client, url) -> None:
    """All three new routes keep the router's shared service-token guard."""
    for headers in (None, {"X-Service-Token": "not-the-token"}):
        response = client.get(url, headers=headers)

        assert response.status_code == 401
        problem = response.json()
        assert set(problem) == PROBLEM_DETAIL_KEYS
        assert problem["type"] == "about:blank"
        assert problem["title"] == "Unauthorized"
        assert problem["status"] == 401
        assert problem["instance"] == url
