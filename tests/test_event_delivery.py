"""Delivery policy, backoff schedule, dead-lettering and the delivery record.

The broker clients are replaced by recorders, so every record the publishers write --
its topic or routing key, its headers and its decoded envelope -- is assertable without
Kafka, RabbitMQ or a running broker, exactly as tests/test_events_kafka.py and
tests/test_events_rabbit.py do it.
"""

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

import pytest
from fastapi.exceptions import RequestValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import Base, SessionLocal, engine
from app.delivery import (
    DEFAULT_BASE_DELAY_MS,
    DEFAULT_JITTER,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_DELAY_MS,
    DELIVERY_STATUSES,
    DeliveryPolicy,
    EventDeliveryService,
    is_transient_error,
    next_delay_ms,
)
from app.kafka_producer import (
    KAFKA_UNAVAILABLE_DETAIL,
    DeliveryResult,
    EventPublisher,
    KafkaSettings,
    KafkaUnavailableError,
    get_event_publisher,
)
from app.main import app, create_app
from app.models import EventDelivery
from app.rabbit_publisher import (
    RABBIT_UNAVAILABLE_DETAIL,
    RabbitDeliveryResult,
    RabbitMessageProperties,
    RabbitPublisher,
    RabbitSettings,
    RabbitUnavailableError,
    get_rabbit_publisher,
)
from app.schemas import DeliveryStatusOut
from app.security import BOOKINGS_SERVICE_TOKEN


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
        self.header_pairs: list[tuple[object, object]] = []
        self.flush_timeouts: list[float] = []
        self._on_delivery = None

    def produce(self, topic, *, value=None, key=None, headers=None, on_delivery=None):
        if self.produce_error is not None:
            raise self.produce_error
        self.records.append(
            {"topic": topic, "value": value, "key": key, "headers": headers}
        )
        self.header_pairs.append((topic, headers))
        self._on_delivery = on_delivery

    def poll(self, timeout):
        return 0

    def flush(self, timeout):
        self.flush_timeouts.append(timeout)
        if self.remaining == 0 and self._on_delivery is not None:
            self._on_delivery(None, _DeliveryMessage(self.partition, self.offset))
        return self.remaining


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
        self.publish_pairs: list[tuple[object, object]] = []
        self.closed = False

    def exchange_declare(
        self, *, exchange: str, exchange_type: str, durable: bool
    ) -> None:
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
        self.publish_pairs.append((routing_key, properties))

    def close(self) -> None:
        self.closed = True


def _kafka_settings() -> KafkaSettings:
    """Settings pointed at a broker that is never contacted in these tests."""
    return KafkaSettings(
        bootstrap_servers="broker.test:9092",
        events_topic="bookmyshow.events",
        client_id="bookmyshow-api",
        flush_timeout_ms=5000,
    )


def _rabbit_settings(routing_key_prefix: str = "bookmyshow") -> RabbitSettings:
    """Settings pointed at a broker that is never contacted in these tests."""
    return RabbitSettings(
        url="amqp://guest:guest@broker.test:5672/",
        exchange="bookmyshow.events",
        routing_key_prefix=routing_key_prefix,
    )


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


def _envelope_of(record: dict[str, object]) -> dict[str, object]:
    value = record.get("value") or record.get("body")
    assert isinstance(value, bytes)
    return json.loads(value.decode("utf-8"))


def _headers_of(record: dict[str, object]) -> dict[str, bytes]:
    headers = record["headers"]
    assert isinstance(headers, list)
    return dict(headers)


ENV_NAMES = (
    "DELIVERY_MAX_ATTEMPTS",
    "DELIVERY_BASE_DELAY_MS",
    "DELIVERY_MAX_DELAY_MS",
    "DELIVERY_JITTER",
)


def test_policy_reads_the_four_environment_variables(monkeypatch) -> None:
    monkeypatch.setenv("DELIVERY_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("DELIVERY_BASE_DELAY_MS", "250")
    monkeypatch.setenv("DELIVERY_MAX_DELAY_MS", "4000")
    monkeypatch.setenv("DELIVERY_JITTER", "0.5")

    assert DeliveryPolicy.from_env() == DeliveryPolicy(
        max_attempts=5,
        base_delay_ms=250,
        max_delay_ms=4000,
        jitter=0.5,
    )


def test_policy_falls_back_to_the_defaults(monkeypatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    defaults = DeliveryPolicy(
        max_attempts=DEFAULT_MAX_ATTEMPTS,
        base_delay_ms=DEFAULT_BASE_DELAY_MS,
        max_delay_ms=DEFAULT_MAX_DELAY_MS,
        jitter=DEFAULT_JITTER,
    )
    assert DeliveryPolicy.from_env() == defaults
    assert (
        DEFAULT_MAX_ATTEMPTS,
        DEFAULT_BASE_DELAY_MS,
        DEFAULT_MAX_DELAY_MS,
        DEFAULT_JITTER,
    ) == (3, 100, 2000, 0.2)

    # A blank value falls back rather than raising.
    for name in ENV_NAMES:
        monkeypatch.setenv(name, "   ")
    assert DeliveryPolicy.from_env() == defaults

    monkeypatch.setenv("DELIVERY_MAX_ATTEMPTS", "not-a-number")
    assert DeliveryPolicy.from_env().max_attempts == DEFAULT_MAX_ATTEMPTS == 3

    # Non-numeric, non-positive and out-of-range values all fall back instead of raising.
    monkeypatch.setenv("DELIVERY_MAX_ATTEMPTS", "-2")
    monkeypatch.setenv("DELIVERY_BASE_DELAY_MS", "0")
    monkeypatch.setenv("DELIVERY_MAX_DELAY_MS", "later")
    monkeypatch.setenv("DELIVERY_JITTER", "2.5")
    assert DeliveryPolicy.from_env() == defaults


def test_delay_doubles_and_is_capped() -> None:
    policy = _policy(base_delay_ms=100, max_delay_ms=2000, jitter=0.0)

    assert [next_delay_ms(policy, attempt) for attempt in (1, 2, 3, 4)] == [
        100,
        200,
        400,
        800,
    ]
    assert next_delay_ms(policy, 5) == 1600
    assert next_delay_ms(policy, 8) == 2000


def test_delay_jitter_stays_inside_the_bounds() -> None:
    policy = _policy(base_delay_ms=100, max_delay_ms=2000, jitter=0.2)

    assert next_delay_ms(policy, 1, rng=lambda: 0.0) == 80
    assert next_delay_ms(policy, 1, rng=lambda: 1.0) == 120

    for attempt in range(1, 7):
        raw = min(policy.max_delay_ms, policy.base_delay_ms * 2 ** (attempt - 1))
        for sample in (0.0, 0.25, 0.5, 0.75, 1.0):
            delay = next_delay_ms(policy, attempt, rng=lambda value=sample: value)
            assert raw * (1 - policy.jitter) <= delay <= raw * (1 + policy.jitter)


def test_only_broker_errors_are_transient() -> None:
    assert is_transient_error(KafkaUnavailableError()) is True
    assert is_transient_error(RabbitUnavailableError()) is True
    assert is_transient_error(ValueError("bad event")) is False
    assert is_transient_error(RequestValidationError([])) is False


def test_delivery_statuses_are_the_four_agreed_values() -> None:
    assert DELIVERY_STATUSES == (
        "PENDING",
        "RETRYING",
        "DELIVERED",
        "DEAD_LETTERED",
    )


def test_status_out_has_exactly_the_four_wire_names() -> None:
    dumped = DeliveryStatusOut(
        status="DELIVERED",
        attempts=2,
        lastError=None,
        nextRetryAt=None,
    ).model_dump()

    assert set(dumped) == {"status", "attempts", "lastError", "nextRetryAt"}
    assert dumped == {
        "status": "DELIVERED",
        "attempts": 2,
        "lastError": None,
        "nextRetryAt": None,
    }


def test_the_delivery_row_is_persisted_and_constrained() -> None:
    Base.metadata.create_all(bind=engine)
    assert "event_deliveries" in Base.metadata.tables

    event_id = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add(
            EventDelivery(
                event_id=event_id,
                transport="KAFKA",
                event_type="booking.confirmed",
            )
        )
        db.commit()

    with SessionLocal() as db:
        stored = db.scalar(
            select(EventDelivery).where(EventDelivery.event_id == event_id)
        )
        assert stored is not None
        assert stored.transport == "KAFKA"
        assert stored.event_type == "booking.confirmed"
        assert stored.status == "PENDING"
        assert stored.attempts == 0
        assert stored.last_error is None
        assert stored.next_retry_at is None
        assert isinstance(stored.created_at, datetime)
        assert isinstance(stored.updated_at, datetime)

    # The eventId is the row's key, so a second row cannot claim the same one.
    with SessionLocal() as db:
        db.add(
            EventDelivery(
                event_id=event_id, transport="RABBIT", event_type="movie.created"
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    for status in DELIVERY_STATUSES:
        with SessionLocal() as db:
            db.add(
                EventDelivery(
                    event_id=str(uuid.uuid4()),
                    transport="RABBIT",
                    event_type="movie.created",
                    status=status,
                )
            )
            db.commit()

    for bad_status in ("FAILED", "REJECTED", "SENT"):
        with SessionLocal() as db:
            db.add(
                EventDelivery(
                    event_id=str(uuid.uuid4()),
                    transport="KAFKA",
                    event_type="movie.created",
                    status=bad_status,
                )
            )
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()

    for bad_transport in ("SQS", "kafka"):
        with SessionLocal() as db:
            db.add(
                EventDelivery(
                    event_id=str(uuid.uuid4()),
                    transport=bad_transport,
                    event_type="movie.created",
                )
            )
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()


def test_kafka_dead_letter_goes_to_the_dlq_topic() -> None:
    producer = _RecordingProducer(partition=9, offset=123)
    publisher = EventPublisher(_kafka_settings(), producer=producer)

    assert publisher.dead_letter_topic == "bookmyshow.events.dlq"

    result = publisher.publish_dead_letter(
        event_id="e-1",
        event_type="booking.confirmed",
        payload={"bookingId": 42},
        attempts=3,
        last_error=KAFKA_UNAVAILABLE_DETAIL,
    )

    assert isinstance(result, DeliveryResult)
    assert result.event_id == "e-1"
    assert result.topic == "bookmyshow.events.dlq"
    assert result.partition == 9
    assert result.offset == 123

    assert len(producer.records) == 1
    record = producer.records[0]
    assert record["topic"] == "bookmyshow.events.dlq"
    assert producer.header_pairs == [("bookmyshow.events.dlq", record["headers"])]

    envelope = _envelope_of(record)
    assert envelope["eventId"] == "e-1"
    assert envelope["type"] == "booking.confirmed"
    assert envelope["payload"] == {"bookingId": 42}

    headers = _headers_of(record)
    assert headers["eventId"] == b"e-1"
    assert headers["type"] == b"booking.confirmed"
    assert headers["attempts"] == b"3"
    assert headers["lastError"] == KAFKA_UNAVAILABLE_DETAIL.encode("utf-8")
    assert producer.flush_timeouts == [5.0]


def test_rabbit_dead_letter_uses_the_dlq_routing_key() -> None:
    channel = _RecordingChannel()
    publisher = RabbitPublisher(_rabbit_settings(), channel=channel)

    assert (
        publisher.dead_letter_routing_key("booking.confirmed")
        == "bookmyshow.booking.confirmed.dlq"
    )

    result = publisher.publish_dead_letter(
        event_id="e-2",
        event_type="booking.confirmed",
        payload={"bookingId": 42},
        attempts=3,
        last_error=RABBIT_UNAVAILABLE_DETAIL,
    )

    assert isinstance(result, RabbitDeliveryResult)
    assert result.event_id == "e-2"
    assert result.exchange == "bookmyshow.events"
    assert result.routing_key == "bookmyshow.booking.confirmed.dlq"

    assert channel.declares == [
        {"exchange": "bookmyshow.events", "exchange_type": "topic", "durable": True}
    ]
    assert len(channel.messages) == 1
    record = channel.messages[0]
    assert record["exchange"] == "bookmyshow.events"
    assert record["routing_key"] == "bookmyshow.booking.confirmed.dlq"

    properties = record["properties"]
    assert isinstance(properties, RabbitMessageProperties)
    assert properties.delivery_mode == 2
    assert properties.content_type == "application/json"
    assert properties.message_id == "e-2"
    assert properties.headers == {
        "eventId": "e-2",
        "type": "booking.confirmed",
        "correlationId": "e-2",
        "attempts": "3",
        "lastError": RABBIT_UNAVAILABLE_DETAIL,
    }
    assert channel.publish_pairs == [("bookmyshow.booking.confirmed.dlq", properties)]

    envelope = _envelope_of(record)
    assert envelope["eventId"] == "e-2"
    assert envelope["type"] == "booking.confirmed"
    assert envelope["payload"] == {"bookingId": 42}


def test_dead_letter_failure_raises_the_typed_error() -> None:
    broken_producer = _RecordingProducer(produce_error=RuntimeError("no broker"))
    with pytest.raises(KafkaUnavailableError):
        EventPublisher(_kafka_settings(), producer=broken_producer).publish_dead_letter(
            event_id="e-3",
            event_type="booking.confirmed",
            payload={"bookingId": 42},
            attempts=3,
            last_error=KAFKA_UNAVAILABLE_DETAIL,
        )
    assert broken_producer.records == []

    stuck = _RecordingProducer(remaining=1)
    with pytest.raises(KafkaUnavailableError):
        EventPublisher(_kafka_settings(), producer=stuck).publish_dead_letter(
            event_id="e-4",
            event_type="booking.confirmed",
            payload={"bookingId": 42},
            attempts=3,
            last_error=KAFKA_UNAVAILABLE_DETAIL,
        )

    broken_channel = _RecordingChannel(publish_error=RuntimeError("channel is closed"))
    broken_rabbit = RabbitPublisher(_rabbit_settings(), channel=broken_channel)
    with pytest.raises(RabbitUnavailableError):
        broken_rabbit.publish_dead_letter(
            event_id="e-5",
            event_type="booking.confirmed",
            payload={"bookingId": 42},
            attempts=3,
            last_error=RABBIT_UNAVAILABLE_DETAIL,
        )
    assert broken_channel.messages == []


def test_publish_reuses_a_caller_supplied_event_id() -> None:
    producer = _RecordingProducer()
    kafka = EventPublisher(_kafka_settings(), producer=producer)

    supplied = kafka.publish(
        event_type="booking.confirmed",
        payload={"bookingId": 1},
        event_id="e-6",
    )

    assert supplied.event_id == "e-6"
    assert _envelope_of(producer.records[0])["eventId"] == "e-6"
    assert _headers_of(producer.records[0])["eventId"] == b"e-6"

    generated = kafka.publish(event_type="booking.confirmed", payload={"bookingId": 2})

    assert generated.event_id != "e-6"
    assert uuid.UUID(generated.event_id)

    channel = _RecordingChannel()
    rabbit = RabbitPublisher(_rabbit_settings(), channel=channel)

    supplied_rabbit = rabbit.publish(
        event_type="booking.confirmed",
        payload={"bookingId": 3},
        event_id="e-7",
    )

    assert supplied_rabbit.event_id == "e-7"
    properties = channel.messages[0]["properties"]
    assert isinstance(properties, RabbitMessageProperties)
    assert properties.message_id == "e-7"
    assert properties.headers["eventId"] == "e-7"
    assert _envelope_of(channel.messages[0])["eventId"] == "e-7"


class _FlakyPublisher:
    """Stand-in for a broker publisher: fails its first N calls, then answers a result."""

    def __init__(
        self,
        *,
        failures: int = 0,
        error: BaseException | None = None,
        rabbit: bool = False,
        dead_letter_outcome: str = "succeed",
    ) -> None:
        self.failures = failures
        self.error = error if error is not None else KafkaUnavailableError()
        self.rabbit = rabbit
        self.dead_letter_outcome = dead_letter_outcome
        self.event_ids: list[str | None] = []
        self.dead_letters: list[dict[str, object]] = []
        self.publish_calls = 0

    def publish(
        self,
        *,
        event_type: str,
        payload: dict[str, object],
        version: int = 1,
        key: str | None = None,
        event_id: str | None = None,
    ) -> object:
        self.publish_calls += 1
        self.event_ids.append(event_id)
        if self.publish_calls <= self.failures:
            raise self.error
        if self.rabbit:
            return RabbitDeliveryResult(
                event_id=str(event_id),
                exchange="bookmyshow.events",
                routing_key="bookmyshow.booking.confirmed",
            )
        return DeliveryResult(
            event_id=str(event_id),
            topic="bookmyshow.events",
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
    ) -> object:
        self.dead_letters.append(
            {
                "event_id": event_id,
                "event_type": event_type,
                "payload": payload,
                "attempts": attempts,
                "last_error": last_error,
            }
        )
        if self.dead_letter_outcome == "fail":
            if self.rabbit:
                raise RabbitUnavailableError()
            raise KafkaUnavailableError()
        if self.rabbit:
            return RabbitDeliveryResult(
                event_id=event_id,
                exchange="bookmyshow.events",
                routing_key="bookmyshow.booking.confirmed.dlq",
            )
        return DeliveryResult(
            event_id=event_id,
            topic="bookmyshow.events.dlq",
            partition=1,
            offset=99,
        )


class _FakeSleep:
    """Stand-in for time.sleep: records each delay instead of waiting for it."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _row_of(event_id: str) -> EventDelivery | None:
    with SessionLocal() as db:
        return db.scalar(select(EventDelivery).where(EventDelivery.event_id == event_id))


def test_retries_transient_failures_with_backoff() -> None:
    Base.metadata.create_all(bind=engine)
    publisher = _FlakyPublisher(failures=2)
    sleep = _FakeSleep()

    with SessionLocal() as db:
        service = EventDeliveryService(
            publisher=publisher,
            policy=_policy(),
            db=db,
            transport="KAFKA",
            sleep=sleep,
        )
        outcome = service.deliver(
            event_type="booking.confirmed", payload={"bookingId": 42}
        )
        stored = service.get(outcome.event_id)
        snapshot = (
            None if stored is None else (stored.status, stored.attempts, stored.last_error)
        )

    assert publisher.publish_calls == 3
    assert publisher.dead_letters == []
    assert sleep.calls == [0.1, 0.2]
    assert publisher.event_ids[0] == outcome.event_id
    assert len(set(publisher.event_ids)) == 1
    assert outcome.status == "DELIVERED"
    assert outcome.attempts == 3
    assert isinstance(outcome.result, DeliveryResult)
    assert snapshot == ("DELIVERED", 3, None)


def test_the_retrying_row_is_committed_before_the_next_attempt() -> None:
    Base.metadata.create_all(bind=engine)
    publisher = _FlakyPublisher(failures=2)
    seen: list[tuple[str, object]] = []

    def read_while_sleeping(seconds: float) -> None:
        event_id = str(publisher.event_ids[0])
        with SessionLocal() as reader:
            row = reader.scalar(
                select(EventDelivery).where(EventDelivery.event_id == event_id)
            )
            assert row is not None
            seen.append((row.status, row.next_retry_at))

    with SessionLocal() as db:
        service = EventDeliveryService(
            publisher=publisher,
            policy=_policy(),
            db=db,
            transport="KAFKA",
            sleep=read_while_sleeping,
        )
        outcome = service.deliver(
            event_type="booking.confirmed", payload={"bookingId": 42}
        )

    assert outcome.status == "DELIVERED"
    assert [status for status, _ in seen] == ["RETRYING", "RETRYING"]
    assert all(retry_at is not None for _, retry_at in seen)


def test_dead_letters_after_exhaustion() -> None:
    Base.metadata.create_all(bind=engine)
    publisher = _FlakyPublisher(failures=10)

    with SessionLocal() as db:
        service = EventDeliveryService(
            publisher=publisher,
            policy=_policy(max_attempts=3),
            db=db,
            transport="KAFKA",
            sleep=_FakeSleep(),
        )
        outcome = service.deliver(
            event_type="booking.confirmed", payload={"bookingId": 42}
        )

    assert publisher.publish_calls == 3
    assert len(publisher.dead_letters) == 1
    dead_letter = publisher.dead_letters[0]
    assert dead_letter["event_id"] == outcome.event_id
    assert dead_letter["attempts"] == 3
    assert KAFKA_UNAVAILABLE_DETAIL in str(dead_letter["last_error"])

    assert outcome.status == "DEAD_LETTERED"
    assert outcome.attempts == 3
    assert isinstance(outcome.result, DeliveryResult)
    assert outcome.result.topic == "bookmyshow.events.dlq"

    row = _row_of(outcome.event_id)
    assert row is not None
    assert row.status == "DEAD_LETTERED"
    assert row.attempts == 3
    assert row.last_error is not None
    assert KAFKA_UNAVAILABLE_DETAIL in row.last_error
    assert row.next_retry_at is None


def test_no_retry_for_a_non_transient_error() -> None:
    Base.metadata.create_all(bind=engine)
    publisher = _FlakyPublisher(failures=10, error=ValueError("bad event"))
    sleep = _FakeSleep()

    with SessionLocal() as db:
        service = EventDeliveryService(
            publisher=publisher,
            policy=_policy(max_attempts=3),
            db=db,
            transport="KAFKA",
            sleep=sleep,
        )
        with pytest.raises(ValueError):
            service.deliver(event_type="booking.confirmed", payload={"bookingId": 42})

    assert publisher.publish_calls == 1
    assert sleep.calls == []
    assert publisher.dead_letters == []

    event_id = str(publisher.event_ids[0])
    row = _row_of(event_id)
    assert row is not None
    assert row.status == "PENDING"
    assert row.attempts == 1
    assert row.last_error == "bad event"
    assert row.next_retry_at is None


def test_dead_letter_failure_propagates_and_keeps_retrying_status() -> None:
    Base.metadata.create_all(bind=engine)
    publisher = _FlakyPublisher(failures=10, dead_letter_outcome="fail")

    with SessionLocal() as db:
        service = EventDeliveryService(
            publisher=publisher,
            policy=_policy(max_attempts=3),
            db=db,
            transport="KAFKA",
            sleep=_FakeSleep(),
        )
        with pytest.raises(KafkaUnavailableError):
            service.deliver(
                event_type="booking.confirmed", payload={"bookingId": 42}
            )

    assert publisher.publish_calls == 3
    assert len(publisher.dead_letters) == 1

    row = _row_of(str(publisher.event_ids[0]))
    assert row is not None
    assert row.status == "RETRYING"
    assert row.attempts == 3
    assert row.last_error is not None
    assert KAFKA_UNAVAILABLE_DETAIL in row.last_error


def test_rabbit_transient_failures_share_the_same_retry() -> None:
    Base.metadata.create_all(bind=engine)
    publisher = _FlakyPublisher(failures=1, error=RabbitUnavailableError(), rabbit=True)
    sleep = _FakeSleep()

    with SessionLocal() as db:
        service = EventDeliveryService(
            publisher=publisher,
            policy=_policy(),
            db=db,
            transport="RABBIT",
            sleep=sleep,
        )
        outcome = service.deliver(
            event_type="booking.confirmed", payload={"bookingId": 42}
        )

    assert publisher.publish_calls == 2
    assert sleep.calls == [0.1]
    assert outcome.status == "DELIVERED"
    assert isinstance(outcome.result, RabbitDeliveryResult)

    row = _row_of(outcome.event_id)
    assert row is not None
    assert row.transport == "RABBIT"
    assert row.status == "DELIVERED"
    assert row.attempts == 2


def test_create_app_exposes_the_delivery_policy() -> None:
    application = create_app()

    assert application.state.delivery_policy == DeliveryPolicy.from_env()


EVENTS_KAFKA_URL = "/api/events/kafka"
EVENTS_RABBIT_URL = "/api/events/rabbit"
DELIVERIES_URL = "/api/events/deliveries"
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
EVENT_KEYS = {"eventId", "topic", "partition", "offset"}
RABBIT_EVENT_KEYS = {"eventId", "exchange", "routingKey"}
DELIVERY_KEYS = {"status", "attempts", "lastError", "nextRetryAt"}
SERVICE_HEADERS: dict[str, str] = {"X-Service-Token": BOOKINGS_SERVICE_TOKEN}
EVENT_BODY = {"type": "booking.confirmed", "payload": {"bookingId": 42}}


@contextmanager
def _use_kafka_publisher(publisher: object) -> Iterator[None]:
    app.dependency_overrides[get_event_publisher] = lambda: publisher
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_event_publisher, None)


@contextmanager
def _use_rabbit_publisher(publisher: object) -> Iterator[None]:
    app.dependency_overrides[get_rabbit_publisher] = lambda: publisher
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_rabbit_publisher, None)


@contextmanager
def _fast_delivery(client) -> Iterator[None]:
    """A 1ms, jitter-free policy so a test never waits out the real backoff."""
    built = client.app.state.delivery_policy
    client.app.state.delivery_policy = _policy(base_delay_ms=1, jitter=0.0)
    try:
        yield
    finally:
        client.app.state.delivery_policy = built


def test_attempt_count_and_success(client) -> None:
    """Two transient failures then a success: exactly three attempts, one delivered row."""
    fake = _FlakyPublisher(failures=2)

    with _use_kafka_publisher(fake), _fast_delivery(client):
        response = client.post(
            EVENTS_KAFKA_URL, json=EVENT_BODY, headers=SERVICE_HEADERS
        )

    assert response.status_code == 202
    body = response.json()
    assert set(body) == EVENT_KEYS
    assert uuid.UUID(body["eventId"])
    assert body["topic"] == "bookmyshow.events"

    assert fake.publish_calls == 3
    assert set(fake.event_ids) == {body["eventId"]}
    assert fake.dead_letters == []

    row = _row_of(body["eventId"])
    assert row is not None
    assert row.status == "DELIVERED"
    assert row.attempts == 3
    assert row.last_error is None
    assert row.transport == "KAFKA"


def test_exponential_schedule() -> None:
    """The recorded gaps double from the base delay and are clamped at the ceiling."""
    Base.metadata.create_all(bind=engine)
    publisher = _FlakyPublisher(failures=4)
    sleep = _FakeSleep()
    policy = _policy(max_attempts=5, base_delay_ms=100, max_delay_ms=2000, jitter=0.0)

    with SessionLocal() as db:
        outcome = EventDeliveryService(
            publisher=publisher,
            policy=policy,
            db=db,
            transport="KAFKA",
            sleep=sleep,
        ).deliver(event_type="booking.confirmed", payload={"bookingId": 42})

    assert outcome.status == "DELIVERED"
    assert outcome.attempts == 5
    assert publisher.publish_calls == 5
    assert sleep.calls == [0.1, 0.2, 0.4, 0.8]
    assert next_delay_ms(policy, 5) == 1600
    assert next_delay_ms(policy, 6) == 2000


def test_jitter_stays_within_bounds() -> None:
    """Pinning the rng shows both bounds, and the service never sleeps outside them."""
    Base.metadata.create_all(bind=engine)
    policy = _policy(max_attempts=4, base_delay_ms=100, max_delay_ms=2000, jitter=0.2)

    assert next_delay_ms(policy, 1, rng=lambda: 0.0) == 80
    assert next_delay_ms(policy, 1, rng=lambda: 1.0) == 120

    publisher = _FlakyPublisher(failures=3)
    sleep = _FakeSleep()
    with SessionLocal() as db:
        outcome = EventDeliveryService(
            publisher=publisher,
            policy=policy,
            db=db,
            transport="KAFKA",
            sleep=sleep,
        ).deliver(event_type="booking.confirmed", payload={"bookingId": 42})

    assert outcome.status == "DELIVERED"
    assert outcome.attempts == 4
    assert len(sleep.calls) == 3
    for attempt, seconds in enumerate(sleep.calls, start=1):
        raw = min(policy.max_delay_ms, policy.base_delay_ms * 2 ** (attempt - 1))
        lower = raw * (1 - policy.jitter) / 1000
        upper = raw * (1 + policy.jitter) / 1000
        assert lower <= seconds <= upper


def test_validation_is_never_retried(client) -> None:
    """An invalid body is rejected before a publish is ever attempted."""
    fake = _FlakyPublisher()

    with _use_kafka_publisher(fake), _fast_delivery(client):
        response = client.post(
            EVENTS_KAFKA_URL,
            json={"type": "   ", "payload": {}},
            headers=SERVICE_HEADERS,
        )

    assert response.status_code == 422
    problem = response.json()
    assert set(problem) == PROBLEM_DETAIL_KEYS
    assert problem["type"] == "about:blank"
    assert problem["status"] == 422
    assert problem["instance"] == EVENTS_KAFKA_URL

    assert fake.publish_calls == 0
    assert fake.event_ids == []
    assert fake.dead_letters == []


def test_dead_letter_after_max_attempts(client) -> None:
    """A broker that never answers is parked on the DLQ and the publish still reads 202."""
    fake = _FlakyPublisher(failures=10, dead_letter_outcome="succeed")

    with _use_kafka_publisher(fake), _fast_delivery(client):
        response = client.post(
            EVENTS_KAFKA_URL, json=EVENT_BODY, headers=SERVICE_HEADERS
        )

    assert response.status_code == 202
    body = response.json()
    assert set(body) == EVENT_KEYS
    assert body["topic"] == "bookmyshow.events.dlq"

    assert fake.publish_calls == 3
    assert len(fake.dead_letters) == 1
    assert fake.dead_letters[0]["event_id"] == body["eventId"]
    assert fake.dead_letters[0]["attempts"] == 3

    row = _row_of(body["eventId"])
    assert row is not None
    assert row.status == "DEAD_LETTERED"
    assert row.attempts == 3
    assert row.last_error is not None
    assert KAFKA_UNAVAILABLE_DETAIL in row.last_error
    assert row.next_retry_at is None

    rabbit = _FlakyPublisher(
        failures=10,
        error=RabbitUnavailableError(),
        rabbit=True,
        dead_letter_outcome="succeed",
    )

    with _use_rabbit_publisher(rabbit), _fast_delivery(client):
        rabbit_response = client.post(
            EVENTS_RABBIT_URL, json=EVENT_BODY, headers=SERVICE_HEADERS
        )

    assert rabbit_response.status_code == 202
    rabbit_body = rabbit_response.json()
    assert set(rabbit_body) == RABBIT_EVENT_KEYS
    assert rabbit_body["exchange"] == "bookmyshow.events"
    assert rabbit_body["routingKey"] == "bookmyshow.booking.confirmed.dlq"

    assert rabbit.publish_calls == 3
    assert len(rabbit.dead_letters) == 1

    rabbit_row = _row_of(rabbit_body["eventId"])
    assert rabbit_row is not None
    assert rabbit_row.transport == "RABBIT"
    assert rabbit_row.status == "DEAD_LETTERED"
    assert rabbit_row.attempts == 3
    assert rabbit_row.last_error is not None
    assert RABBIT_UNAVAILABLE_DETAIL in rabbit_row.last_error


def test_delivery_status_endpoint(client) -> None:
    """The delivery resource answers the four ticket fields, 404 and 401 as ProblemDetail."""
    delivered = _FlakyPublisher()

    with _use_kafka_publisher(delivered), _fast_delivery(client):
        published = client.post(
            EVENTS_KAFKA_URL, json=EVENT_BODY, headers=SERVICE_HEADERS
        )
    assert published.status_code == 202
    event_id = published.json()["eventId"]

    known = client.get(f"{DELIVERIES_URL}/{event_id}", headers=SERVICE_HEADERS)

    assert known.status_code == 200
    delivered_body = known.json()
    assert set(delivered_body) == DELIVERY_KEYS
    assert delivered_body == {
        "status": "DELIVERED",
        "attempts": 1,
        "lastError": None,
        "nextRetryAt": None,
    }

    parked = _FlakyPublisher(failures=10, dead_letter_outcome="succeed")

    with _use_kafka_publisher(parked), _fast_delivery(client):
        dead_lettered = client.post(
            EVENTS_KAFKA_URL, json=EVENT_BODY, headers=SERVICE_HEADERS
        )
    assert dead_lettered.status_code == 202
    parked_id = dead_lettered.json()["eventId"]

    dead_read = client.get(f"{DELIVERIES_URL}/{parked_id}", headers=SERVICE_HEADERS)

    assert dead_read.status_code == 200
    dead_body = dead_read.json()
    assert set(dead_body) == DELIVERY_KEYS
    assert dead_body["status"] == "DEAD_LETTERED"
    assert dead_body["attempts"] == 3
    assert dead_body["lastError"]
    assert dead_body["nextRetryAt"] is None

    unknown = client.get(f"{DELIVERIES_URL}/no-such-event", headers=SERVICE_HEADERS)

    assert unknown.status_code == 404
    unknown_body = unknown.json()
    assert set(unknown_body) == PROBLEM_DETAIL_KEYS
    assert unknown_body["title"] == "Not Found"
    assert unknown_body["status"] == 404
    assert unknown_body["instance"] == f"{DELIVERIES_URL}/no-such-event"

    anonymous = client.get(f"{DELIVERIES_URL}/{event_id}")

    assert anonymous.status_code == 401
    anonymous_body = anonymous.json()
    assert set(anonymous_body) == PROBLEM_DETAIL_KEYS
    assert anonymous_body["title"] == "Unauthorized"
    assert anonymous_body["status"] == 401


def test_delivery_config_is_read_by_the_app(monkeypatch) -> None:
    """DELIVERY_MAX_ATTEMPTS reaches the app, so one failed attempt already dead-letters."""
    Base.metadata.create_all(bind=engine)
    monkeypatch.setenv("DELIVERY_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("DELIVERY_BASE_DELAY_MS", "1")
    monkeypatch.setenv("DELIVERY_MAX_DELAY_MS", "2")
    monkeypatch.setenv("DELIVERY_JITTER", "0")

    application = create_app()
    policy = application.state.delivery_policy

    assert policy == _policy(
        max_attempts=1, base_delay_ms=1, max_delay_ms=2, jitter=0.0
    )

    publisher = _FlakyPublisher(failures=10, dead_letter_outcome="succeed")
    sleep = _FakeSleep()
    with SessionLocal() as db:
        outcome = EventDeliveryService(
            publisher=publisher,
            policy=policy,
            db=db,
            transport="KAFKA",
            sleep=sleep,
        ).deliver(event_type="booking.confirmed", payload={"bookingId": 42})

    assert publisher.publish_calls == 1
    assert sleep.calls == []
    assert len(publisher.dead_letters) == 1
    assert outcome.status == "DEAD_LETTERED"
    assert outcome.attempts == 1

    row = _row_of(outcome.event_id)
    assert row is not None
    assert row.status == "DEAD_LETTERED"
    assert row.attempts == 1
