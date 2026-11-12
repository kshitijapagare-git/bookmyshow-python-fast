"""The Kafka consumer: dispatch, commit-after-handling, retry, .dlq, duplicates, status.

A recorder stands in for `confluent_kafka.Consumer` and a second one for the process's
publisher, so every commit, every dead-letter write and every retry delay is
assertable without a broker, exactly as tests/test_events_kafka.py does it for the
producer. Retries never wait: `sleep`, `rng` and `now` are injected into the consumer.
"""

import json
import logging
import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.delivery import DeliveryPolicy, next_delay_ms
from app.kafka_consumer import (
    DEFAULT_KAFKA_AUTO_OFFSET_RESET,
    DEFAULT_KAFKA_CONSUMER_GROUP,
    ConsumedEvent,
    HandlerRegistry,
    KafkaConsumerSettings,
    KafkaConsumerState,
    KafkaEventConsumer,
)
from app.kafka_producer import (
    KAFKA_UNAVAILABLE_DETAIL,
    DeliveryResult,
    KafkaSettings,
    KafkaUnavailableError,
)
from app.main import app
from app.security import BOOKINGS_SERVICE_TOKEN

STATUS_URL = "/api/events/kafka/status"
TOPIC = "bookmyshow.events"
GROUP = "bookmyshow-api"
SERVICE_HEADERS: dict[str, str] = {"X-Service-Token": BOOKINGS_SERVICE_TOKEN}
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
STATUS_KEYS = {"running", "topic", "group", "processedCount", "lastEventAt", "lastError"}
FIXED_NOW = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
CONSUMER_ENV_NAMES = ("KAFKA_CONSUMER_GROUP", "KAFKA_AUTO_OFFSET_RESET")


class _RecordingConsumer:
    """Stand-in for confluent_kafka.Consumer: records each call and polls a queue."""

    def __init__(
        self,
        messages: list[object] | None = None,
        log: list[str] | None = None,
        polled: threading.Event | None = None,
    ) -> None:
        self.messages: list[object] = list(messages or [])
        self.subscribed: list[list[str]] = []
        self.commits: list[object] = []
        self.polls = 0
        self.closed = False
        self.log: list[str] = log if log is not None else []
        self.polled = polled

    def subscribe(self, topics: list[str]) -> None:
        self.subscribed.append(list(topics))

    def poll(self, timeout: float) -> object | None:
        self.polls += 1
        if self.polled is not None:
            self.polled.set()
        if self.messages:
            return self.messages.pop(0)
        return None

    def commit(self, message: object | None = None) -> None:
        self.log.append("commit")
        self.commits.append(message)

    def close(self) -> None:
        self.closed = True


class _FakeMessage:
    """Stand-in for a confluent_kafka.Message: the two accessors `process` reads."""

    def __init__(self, body: dict[str, object] | bytes | None) -> None:
        self.body = body
        self._raw = (
            body
            if isinstance(body, bytes) or body is None
            else json.dumps(body).encode("utf-8")
        )

    def value(self) -> bytes | None:
        return self._raw

    def headers(self) -> list[tuple[str, bytes]] | None:
        """The `eventId`/`type` headers the producer writes, when the body has them."""
        if not isinstance(self.body, dict):
            return None
        pairs: list[tuple[str, bytes]] = [
            ("eventId", str(self.body["eventId"]).encode("utf-8"))
        ]
        if isinstance(self.body.get("type"), str):
            pairs.append(("type", str(self.body["type"]).encode("utf-8")))
        return pairs


class _FakePublisher:
    """Stand-in for the process's Kafka publisher: records each dead-letter write."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.dead_letters: list[dict[str, object]] = []

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
        if self.fail:
            raise KafkaUnavailableError()
        return DeliveryResult(
            event_id=event_id,
            topic=f"{TOPIC}.dlq",
            partition=0,
            offset=7,
        )


class _FakeSleep:
    """Stand-in for time.sleep: records each delay instead of waiting for it."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _settings() -> KafkaSettings:
    """Settings pointed at a broker that is never contacted in these tests."""
    return KafkaSettings(
        bootstrap_servers="broker.test:9092",
        events_topic=TOPIC,
        client_id="bookmyshow-api",
        flush_timeout_ms=5000,
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


def _envelope(
    event_id: str = "e-1",
    event_type: str = "booking.confirmed",
    payload: dict[str, object] | None = None,
    *,
    version: int = 1,
    key: str | None = None,
    occurred_at: str = "2026-10-20T10:00:00+00:00",
) -> dict[str, object]:
    """The envelope the producer writes, with `key` only present when it was given."""
    body: dict[str, object] = {
        "eventId": event_id,
        "type": event_type,
        "version": version,
        "occurredAt": occurred_at,
        "payload": {"bookingId": 42} if payload is None else payload,
    }
    if key is not None:
        body["key"] = key
    return body


def _message(
    event_id: str = "e-1",
    event_type: str = "booking.confirmed",
    payload: dict[str, object] | None = None,
    **kwargs: object,
) -> _FakeMessage:
    return _FakeMessage(
        _envelope(event_id, event_type, payload, **kwargs)  # type: ignore[arg-type]
    )


def _build_consumer(
    *,
    registry: HandlerRegistry | None = None,
    publisher: object | None = None,
    messages: list[object] | None = None,
    policy: DeliveryPolicy | None = None,
    sleep: _FakeSleep | None = None,
    log: list[str] | None = None,
    polled: threading.Event | None = None,
) -> tuple[KafkaEventConsumer, _RecordingConsumer]:
    """A consumer driven by a recorder, with no delay and a fixed clock."""
    client = _RecordingConsumer(messages, log=log, polled=polled)
    consumer = KafkaEventConsumer(
        settings=_settings(),
        consumer_settings=KafkaConsumerSettings(
            group=GROUP, auto_offset_reset=DEFAULT_KAFKA_AUTO_OFFSET_RESET
        ),
        registry=registry if registry is not None else HandlerRegistry(),
        publisher=publisher,
        policy=policy if policy is not None else _policy(),
        consumer=client,
        sleep=sleep if sleep is not None else _FakeSleep(),
        rng=lambda: 0.5,
        now=lambda: FIXED_NOW,
    )
    return consumer, client


def test_settings_read_the_two_consumer_environment_variables(monkeypatch) -> None:
    for name in CONSUMER_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    defaults = KafkaConsumerSettings.from_env()

    assert defaults == KafkaConsumerSettings(
        group=DEFAULT_KAFKA_CONSUMER_GROUP,
        auto_offset_reset=DEFAULT_KAFKA_AUTO_OFFSET_RESET,
    )
    assert (DEFAULT_KAFKA_CONSUMER_GROUP, DEFAULT_KAFKA_AUTO_OFFSET_RESET) == (
        "bookmyshow-api",
        "earliest",
    )

    monkeypatch.setenv("KAFKA_CONSUMER_GROUP", "bookings-consumer")
    monkeypatch.setenv("KAFKA_AUTO_OFFSET_RESET", "latest")

    assert KafkaConsumerSettings.from_env() == KafkaConsumerSettings(
        group="bookings-consumer", auto_offset_reset="latest"
    )

    # A blank group and an unknown reset fall back instead of raising.
    monkeypatch.setenv("KAFKA_CONSUMER_GROUP", "   ")
    monkeypatch.setenv("KAFKA_AUTO_OFFSET_RESET", "sometimes")
    assert KafkaConsumerSettings.from_env() == defaults

    monkeypatch.setenv("KAFKA_AUTO_OFFSET_RESET", "")
    assert KafkaConsumerSettings.from_env() == defaults


def test_handler_is_invoked_and_the_offset_is_committed_after_it_returns() -> None:
    log: list[str] = []
    calls: list[ConsumedEvent] = []
    publisher = _FakePublisher()
    message = _message("e-1", "booking.confirmed", {"bookingId": 42}, key="booking-42")
    registry = HandlerRegistry()

    def handler(event: ConsumedEvent) -> None:
        log.append("handler")
        calls.append(event)

    registry.register("booking.confirmed", handler)
    consumer, client = _build_consumer(
        registry=registry, publisher=publisher, messages=[message], log=log
    )

    outcome = consumer.poll_once()

    assert outcome == "HANDLED"
    assert len(calls) == 1
    event = calls[0]
    assert event.event_id == "e-1"
    assert event.type == "booking.confirmed"
    assert event.version == 1
    assert event.payload == {"bookingId": 42}
    assert event.key == "booking-42"
    assert event.occurred_at == datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)

    # The commit happens only once the handler has returned, not before it ran.
    assert log == ["handler", "commit"]
    assert client.commits == [message]
    assert publisher.dead_letters == []
    assert consumer.state.processed_count == 1
    assert consumer.state.last_event_at == FIXED_NOW
    assert consumer.state.last_error is None
    assert "e-1" in consumer.processed


def test_failed_handler_is_retried_then_dead_lettered_and_committed() -> None:
    policy = _policy(max_attempts=3)
    sleep = _FakeSleep()
    publisher = _FakePublisher()
    message = _message("e-9", "booking.confirmed", {"bookingId": 42}, key="booking-42")
    attempts: list[int] = []
    registry = HandlerRegistry()

    def handler(event: ConsumedEvent) -> None:
        attempts.append(1)
        raise RuntimeError("handler exploded")

    registry.register("booking.confirmed", handler)
    consumer, client = _build_consumer(
        registry=registry,
        publisher=publisher,
        messages=[message],
        policy=policy,
        sleep=sleep,
    )

    outcome = consumer.poll_once()

    assert outcome == "DEAD_LETTERED"
    assert len(attempts) == policy.max_attempts == 3

    # The retries are separated by the MB-122 backoff, not run back-to-back.
    assert sleep.calls == [
        next_delay_ms(policy, attempt, rng=lambda: 0.5) / 1000
        for attempt in (1, 2)
    ]
    assert sleep.calls == [0.1, 0.2]

    assert publisher.dead_letters == [
        {
            "event_id": "e-9",
            "event_type": "booking.confirmed",
            "payload": {"bookingId": 42},
            "version": 1,
            "key": "booking-42",
            "attempts": 3,
            "last_error": "handler exploded",
        }
    ]

    # A successful dead-letter publish counts as handled, so the offset is committed.
    assert client.commits == [message]
    assert consumer.state.processed_count == 1
    assert consumer.state.last_event_at == FIXED_NOW
    assert consumer.state.last_error is None


def test_no_commit_when_the_dead_letter_publish_fails() -> None:
    publisher = _FakePublisher(fail=True)
    message = _message("e-10", "booking.confirmed", {"bookingId": 42})
    registry = HandlerRegistry()
    attempts: list[int] = []

    def handler(event: ConsumedEvent) -> None:
        attempts.append(1)
        raise RuntimeError("handler exploded")

    registry.register("booking.confirmed", handler)
    consumer, client = _build_consumer(
        registry=registry,
        publisher=publisher,
        messages=[message],
        policy=_policy(max_attempts=2),
    )

    # The record is neither handled nor parked, so nothing is committed and the loop
    # keeps polling: process() returns the failure instead of raising out of it.
    assert consumer.poll_once() == "DEAD_LETTER_FAILED"

    assert len(attempts) == 2
    assert len(publisher.dead_letters) == 1
    assert client.commits == []
    assert consumer.state.last_error == KAFKA_UNAVAILABLE_DETAIL
    assert consumer.state.processed_count == 0
    assert "e-10" not in consumer.processed

    # The loop is still alive: the next empty poll simply answers None.
    assert consumer.poll_once() is None


def test_duplicate_event_id_is_skipped_and_committed() -> None:
    first = _message("e-dup", "booking.confirmed", {"bookingId": 1})
    duplicate = _message("e-dup", "booking.confirmed", {"bookingId": 1})
    other = _message("e-other", "booking.confirmed", {"bookingId": 2})
    calls: list[str] = []
    registry = HandlerRegistry()

    def handler(event: ConsumedEvent) -> None:
        calls.append(event.event_id)

    registry.register("booking.confirmed", handler)
    consumer, client = _build_consumer(
        registry=registry, publisher=_FakePublisher(), messages=[first, duplicate, other]
    )

    assert consumer.poll_once() == "HANDLED"
    assert consumer.poll_once() == "DUPLICATE"
    assert consumer.poll_once() == "HANDLED"

    assert calls == ["e-dup", "e-other"]
    # The duplicate is acknowledged so it is not redelivered, but it is not handled twice.
    assert client.commits == [first, duplicate, other]
    assert consumer.state.processed_count == 2


def test_unknown_type_and_malformed_body_are_committed_and_logged(caplog) -> None:
    unknown = _message("e-unknown", "movie.created", {"movieId": 1})
    malformed = _FakeMessage(b"{not json")
    calls: list[ConsumedEvent] = []
    registry = HandlerRegistry()

    def handler(event: ConsumedEvent) -> None:
        calls.append(event)

    registry.register("booking.confirmed", handler)
    consumer, client = _build_consumer(
        registry=registry,
        publisher=_FakePublisher(),
        messages=[unknown, malformed],
    )

    with caplog.at_level(logging.WARNING, logger="app.kafka_consumer"):
        assert consumer.poll_once() == "UNKNOWN_TYPE"
        assert consumer.poll_once() == "MALFORMED"

    assert calls == []
    assert client.commits == [unknown, malformed]
    assert consumer.state.processed_count == 0
    assert consumer.state.last_event_at is None

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.kafka_consumer" and record.levelno == logging.WARNING
    ]
    assert any(
        "movie.created" in message and "e-unknown" in message for message in warnings
    )
    assert any("malformed" in message for message in warnings)


def test_start_then_stop_shuts_the_consumer_down_cleanly() -> None:
    polled = threading.Event()
    consumer, client = _build_consumer(publisher=_FakePublisher(), polled=polled)

    assert consumer.running is False
    assert consumer.status().running is False

    consumer.start()

    assert consumer.running is True
    assert consumer.status().running is True
    # Starting subscribes exactly the configured events topic.
    assert client.subscribed == [[TOPIC]]
    assert consumer.state.running is True
    # The polling thread really is the one talking to the client.
    assert polled.wait(2.0) is True

    consumer.stop()

    assert consumer.running is False
    assert consumer.status().running is False
    assert client.closed is True
    assert client.polls >= 1
    # The final commit on the way out carries no message: it commits the assignment.
    assert client.commits == [None]

    # Stopping twice is a no-op rather than an error.
    consumer.stop()
    assert consumer.running is False
    assert client.closed is True


def test_status_route_shape_and_token(client, monkeypatch) -> None:
    state = KafkaConsumerState(
        running=True,
        processed_count=4,
        last_event_at=FIXED_NOW,
        last_error="broker hiccup",
    )
    app.state.kafka_consumer_state = state

    response = client.get(STATUS_URL, headers=SERVICE_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == STATUS_KEYS
    assert body["running"] is True
    assert body["topic"] == app.state.kafka_settings.events_topic
    assert body["group"] == app.state.kafka_consumer_settings.group
    assert body["processedCount"] == 4
    assert body["lastEventAt"].startswith("2026-10-20T10:00:00")
    assert body["lastError"] == "broker hiccup"

    # The route is documented, and the publish route beside it still answers 202.
    paths = client.get("/openapi.json").json()["paths"]
    assert "get" in paths[STATUS_URL]
    assert paths[STATUS_URL]["get"]["responses"]["200"]

    # The shared service token guards it, exactly like the publish routes.
    for headers in (None, {"X-Service-Token": "not-the-token"}):
        refused = client.get(STATUS_URL, headers=headers)
        assert refused.status_code == 401
        problem = refused.json()
        assert set(problem) == PROBLEM_DETAIL_KEYS
        assert problem["type"] == "about:blank"
        assert problem["title"] == "Unauthorized"
        assert problem["status"] == 401
        assert problem["instance"] == STATUS_URL

    # A host where no consumer could be built still answers 200 with running=false.
    app.state.kafka_consumer_state = KafkaConsumerState()
    monkeypatch.setattr("app.main.build_kafka_event_consumer", lambda **kwargs: None)
    built = app.state.event_consumer
    try:
        with TestClient(app) as fresh_client:
            unbuilt = fresh_client.get(STATUS_URL, headers=SERVICE_HEADERS)
    finally:
        app.state.event_consumer = built

    assert unbuilt.status_code == 200
    unbuilt_body = unbuilt.json()
    assert set(unbuilt_body) == STATUS_KEYS
    assert unbuilt_body == {
        "running": False,
        "topic": app.state.kafka_settings.events_topic,
        "group": app.state.kafka_consumer_settings.group,
        "processedCount": 0,
        "lastEventAt": None,
        "lastError": None,
    }


@pytest.mark.parametrize("body", [b"", b"[]", b'{"eventId": "e-1", "type": "x.y"}'])
def test_a_record_without_a_routable_envelope_is_committed(body) -> None:
    message = _FakeMessage(body)
    consumer, client = _build_consumer(
        publisher=_FakePublisher(), messages=[message]
    )

    assert consumer.poll_once() == "MALFORMED"
    assert client.commits == [message]
    assert consumer.state.processed_count == 0
