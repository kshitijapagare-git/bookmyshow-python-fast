"""Consuming domain events from Kafka in one consumer group, with safe dispatch.

The consumer side mirrors `app.kafka_producer`: nothing here imports librdkafka at
module import time, so a host without the client still boots, and the broker address
and topics stay owned by `KafkaSettings` rather than being duplicated. Only the two
consumer-specific knobs -- `KAFKA_CONSUMER_GROUP` and `KAFKA_AUTO_OFFSET_RESET` --
are read here, with the same fall-back-not-raise convention as `KafkaSettings` and
`RabbitSettings`.

`parse_envelope` turns one consumed record into the `ConsumedEvent` the dispatcher
routes on, and refuses anything it could not route as `MalformedEventError` rather
than handing a half-parsed event to a handler. `HandlerRegistry` is keyed by the
envelope's `type` and ships empty: this module is the dispatch infrastructure, and a
later change registers the concrete handlers. `ProcessedEventStore` is the in-memory,
bounded set of eventIds this process already handled, so a redelivered duplicate is
skipped within one process lifetime only -- it is never persisted.
"""

import json
import logging
import os
import random
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol

from fastapi import Request

from .delivery import DeliveryPolicy, _error_text, next_delay_ms
from .kafka_producer import KafkaSettings

logger = logging.getLogger(__name__)

DEFAULT_KAFKA_CONSUMER_GROUP = "bookmyshow-api"
DEFAULT_KAFKA_AUTO_OFFSET_RESET = "earliest"
AUTO_OFFSET_RESET_VALUES: tuple[str, ...] = ("earliest", "latest", "error")
DEFAULT_PROCESSED_WINDOW = 10_000


def _env(name: str, default: str) -> str:
    """The environment value, or the default when it is absent or blank."""
    return (os.getenv(name) or "").strip() or default


@dataclass(frozen=True)
class KafkaConsumerSettings:
    """Which group this process joins and where a group with no offset starts."""

    group: str
    auto_offset_reset: str

    @classmethod
    def from_env(cls) -> "KafkaConsumerSettings":
        reset = _env("KAFKA_AUTO_OFFSET_RESET", DEFAULT_KAFKA_AUTO_OFFSET_RESET)
        return cls(
            group=_env("KAFKA_CONSUMER_GROUP", DEFAULT_KAFKA_CONSUMER_GROUP),
            auto_offset_reset=(
                reset
                if reset in AUTO_OFFSET_RESET_VALUES
                else DEFAULT_KAFKA_AUTO_OFFSET_RESET
            ),
        )


class MalformedEventError(Exception):
    """A consumed record the dispatcher cannot route to any handler."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class ConsumedEvent:
    """One decoded envelope: everything the handler registry needs to dispatch it."""

    event_id: str
    type: str
    version: int
    occurred_at: datetime | None
    payload: dict[str, object]
    key: str | None


def _required_text(body: dict[str, object], name: str) -> str:
    """A non-blank string field of the envelope, or MalformedEventError."""
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise MalformedEventError(f"envelope has no {name}")
    return value


def _occurred_at(body: dict[str, object]) -> datetime | None:
    """The envelope's `occurredAt` as a datetime, or None when it was not sent."""
    raw = body.get("occurredAt")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise MalformedEventError("envelope occurredAt is not an ISO timestamp")
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        raise MalformedEventError(
            "envelope occurredAt is not an ISO timestamp"
        ) from None


def _version(body: dict[str, object]) -> int:
    """The envelope's schema version, defaulting to 1 like the producer's."""
    raw = body.get("version", 1)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise MalformedEventError("envelope version is not a positive integer")
    return raw


def _key(body: dict[str, object]) -> str | None:
    """The business key, which the producer omits entirely when there is none."""
    raw = body.get("key")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise MalformedEventError("envelope key is not a string")
    return raw


def parse_envelope(raw: bytes | None) -> ConsumedEvent:
    """Decode one consumed record, raising MalformedEventError for anything unroutable."""
    if not raw:
        raise MalformedEventError("event body is empty")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise MalformedEventError("event body is not JSON") from None
    if not isinstance(body, dict):
        raise MalformedEventError("event body is not a JSON object")

    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise MalformedEventError("envelope payload is not a JSON object")

    return ConsumedEvent(
        event_id=_required_text(body, "eventId"),
        type=_required_text(body, "type"),
        version=_version(body),
        occurred_at=_occurred_at(body),
        payload=payload,
        key=_key(body),
    )


Handler = Callable[[ConsumedEvent], None]


class HandlerRegistry:
    """event.type -> handler; a handler signals failure by raising.

    The registry ships empty: it is the dispatch infrastructure a later change
    registers the concrete handlers on, and an unknown type is acknowledged.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, event_type: str, handler: Handler) -> None:
        self._handlers[event_type] = handler

    def get(self, event_type: str) -> Handler | None:
        return self._handlers.get(event_type)

    def registered_types(self) -> tuple[str, ...]:
        return tuple(self._handlers)


class ProcessedEventStore:
    """A bounded in-memory set of the eventIds this process already handled.

    The store is a FIFO: once `max_size` ids are held the oldest one is dropped, so
    memory stays bounded and a duplicate is only skipped within one process lifetime.
    Nothing here is persisted, which is the clarifying decision for this change.
    """

    def __init__(self, max_size: int = DEFAULT_PROCESSED_WINDOW) -> None:
        self._max_size = max_size if max_size > 0 else DEFAULT_PROCESSED_WINDOW
        self._seen: OrderedDict[str, None] = OrderedDict()

    def add(self, event_id: str) -> bool:
        """True only for a newly seen id; False when it is already in the window."""
        if event_id in self._seen:
            return False
        self._seen[event_id] = None
        while len(self._seen) > self._max_size:
            self._seen.popitem(last=False)
        return True

    def __contains__(self, event_id: object) -> bool:
        return event_id in self._seen

    def __len__(self) -> int:
        return len(self._seen)


@dataclass
class KafkaConsumerState:
    """The in-memory counters and last-seen values the status route reads.

    Everything here is per process and per lifetime: a restarted consumer starts from
    the initial values again, because nothing is persisted.
    """

    running: bool = False
    processed_count: int = 0
    last_event_at: datetime | None = None
    last_error: str | None = None


DEFAULT_POLL_TIMEOUT_S: float = 0.2
CONSUMER_STOP_TIMEOUT_S: float = 5.0

ProcessOutcome = Literal[
    "HANDLED",
    "DEAD_LETTERED",
    "DUPLICATE",
    "UNKNOWN_TYPE",
    "MALFORMED",
    "DEAD_LETTER_FAILED",
]


class ConsumerMessage(Protocol):
    """The two accessors `process` needs off a confluent_kafka.Message."""

    def value(self) -> bytes | None: ...

    def headers(self) -> object: ...


@dataclass(frozen=True)
class KafkaConsumerStatus:
    """What one status read reports about this process's consumer."""

    running: bool
    topic: str
    group: str
    processed_count: int
    last_event_at: datetime | None
    last_error: str | None


class _ConfluentConsumerAdapter:
    """A real confluent_kafka.Consumer, imported lazily so a host without it boots."""

    def __init__(
        self, settings: KafkaSettings, consumer_settings: KafkaConsumerSettings
    ) -> None:
        from confluent_kafka import Consumer

        self._consumer = Consumer(
            {
                "bootstrap.servers": settings.bootstrap_servers,
                "group.id": consumer_settings.group,
                "auto.offset.reset": consumer_settings.auto_offset_reset,
                # Offsets are committed by hand, only once a record counts as handled.
                "enable.auto.commit": False,
            }
        )

    def subscribe(self, topics: list[str]) -> None:
        self._consumer.subscribe(topics)

    def poll(self, timeout: float) -> ConsumerMessage | None:
        return self._consumer.poll(timeout)

    def commit(self, message: object | None = None) -> None:
        """Commit one record's offset, or the whole assignment when none is given."""
        if message is None:
            self._consumer.commit()
            return
        self._consumer.commit(message=message)

    def close(self) -> None:
        self._consumer.close()


class KafkaEventConsumer:
    """Consumes one topic in one group and dispatches each event to its handler.

    A record counts as handled only once its handler returned or the event was parked
    on the dead-letter destination, and only then is its own offset committed, so a
    crashed process redelivers rather than drops. A handler that fails is retried
    in-process with the same `DeliveryPolicy` backoff the producer retries with, and a
    duplicate eventId is skipped and acknowledged. The broker client is injectable,
    exactly like `EventPublisher(producer=...)`, so no test needs a live broker.
    """

    def __init__(
        self,
        *,
        settings: KafkaSettings,
        consumer_settings: KafkaConsumerSettings,
        registry: HandlerRegistry,
        publisher: object | None,
        policy: DeliveryPolicy,
        state: KafkaConsumerState | None = None,
        processed: ProcessedEventStore | None = None,
        consumer: object | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.settings = settings
        self.consumer_settings = consumer_settings
        self.registry = registry
        self.publisher = publisher
        self.policy = policy
        self.state = state if state is not None else KafkaConsumerState()
        self.processed = processed if processed is not None else ProcessedEventStore()
        self.dead_letter_call: Callable[..., object] | None = (
            publisher.publish_dead_letter if publisher is not None else None
        )
        self._consumer = consumer
        self._sleep = sleep
        self._rng = rng
        self._now = now
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def running(self) -> bool:
        """Whether the polling thread is inside `run` right now."""
        return bool(self.state.running)

    def _client(self) -> object:
        """The injected client, or the real one built on first use."""
        if self._consumer is None:
            self._consumer = _ConfluentConsumerAdapter(
                self.settings, self.consumer_settings
            )
        return self._consumer

    def start(self) -> None:
        """Subscribe the events topic and spawn the one daemon thread that polls it."""
        self._stop.clear()
        client = self._client()
        client.subscribe([self.settings.events_topic])
        self.state.running = True
        self._thread = threading.Thread(
            target=self.run, name="kafka-event-consumer", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = CONSUMER_STOP_TIMEOUT_S) -> None:
        """Ask the loop to finish, join it, commit once and close; safe to call twice."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None
        self.state.running = False
        if self._closed:
            return
        self._closed = True
        client = self._consumer
        if client is None:
            return
        try:
            client.commit()
        except Exception as exc:  # a broker already gone must not block shutdown
            logger.warning(
                "event consumer final commit failed group=%s: %s",
                self.consumer_settings.group,
                exc,
            )
        try:
            client.close()
        except Exception as exc:  # a broken broker must not stop the app shutting down
            logger.warning("event consumer close failed: %s", exc)

    def run(self) -> None:
        """Poll until `stop` sets the flag; a broker error is recorded, never fatal."""
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # a broken broker must not kill the thread
                self.state.last_error = _error_text(exc)
                logger.warning(
                    "event consumer poll failed topic=%s group=%s: %s",
                    self.settings.events_topic,
                    self.consumer_settings.group,
                    exc,
                )

    def poll_once(self) -> ProcessOutcome | None:
        """Poll once and handle whatever came back; None when the poll was empty."""
        message = self._client().poll(DEFAULT_POLL_TIMEOUT_S)
        if message is None:
            return None
        return self.process(message)

    def process(self, message: ConsumerMessage) -> ProcessOutcome:
        """Parse one record, skip what is already done, dispatch it and commit it."""
        try:
            event = parse_envelope(message.value())
        except MalformedEventError as error:
            # A poison record is acknowledged, so it can never loop forever.
            logger.warning(
                "event consumer skipped a malformed record topic=%s group=%s: %s",
                self.settings.events_topic,
                self.consumer_settings.group,
                error.detail,
            )
            self._commit(message)
            return "MALFORMED"

        if event.event_id in self.processed:
            logger.info(
                "event consumer skipped a duplicate eventId=%s type=%s topic=%s "
                "group=%s",
                event.event_id,
                event.type,
                self.settings.events_topic,
                self.consumer_settings.group,
            )
            self._commit(message)
            return "DUPLICATE"

        handler = self.registry.get(event.type)
        if handler is None:
            logger.warning(
                "event consumer has no handler eventId=%s type=%s topic=%s group=%s",
                event.event_id,
                event.type,
                self.settings.events_topic,
                self.consumer_settings.group,
            )
            self._commit(message)
            return "UNKNOWN_TYPE"

        attempt = 0
        while True:
            attempt += 1
            try:
                handler(event)
            except Exception as error:
                last_error = _error_text(error)
                if attempt >= self.policy.max_attempts:
                    return self._dead_letter(
                        message, event, attempt=attempt, last_error=last_error
                    )
                delay_ms = next_delay_ms(self.policy, attempt, rng=self._rng)
                logger.warning(
                    "event consumer retrying eventId=%s type=%s attempt=%s "
                    "delayMs=%s error=%s",
                    event.event_id,
                    event.type,
                    attempt,
                    delay_ms,
                    last_error,
                )
                self._sleep(delay_ms / 1000)
                continue

            self.processed.add(event.event_id)
            self._record_success()
            self._commit(message)
            logger.info(
                "event consumed eventId=%s type=%s topic=%s group=%s attempt=%s",
                event.event_id,
                event.type,
                self.settings.events_topic,
                self.consumer_settings.group,
                attempt,
            )
            return "HANDLED"

    def _dead_letter(
        self,
        message: ConsumerMessage,
        event: ConsumedEvent,
        *,
        attempt: int,
        last_error: str,
    ) -> ProcessOutcome:
        """Park the event on the publisher's DLQ; only a successful park commits."""
        if self.dead_letter_call is None:
            self.state.last_error = last_error
            logger.warning(
                "event consumer could not dead-letter eventId=%s type=%s: no publisher",
                event.event_id,
                event.type,
            )
            return "DEAD_LETTER_FAILED"

        try:
            self.dead_letter_call(
                event_id=event.event_id,
                event_type=event.type,
                payload=event.payload,
                version=event.version,
                key=event.key,
                attempts=attempt,
                last_error=last_error,
            )
        except Exception as error:
            # Neither handled nor parked, so the offset stays uncommitted and the
            # record is redelivered rather than silently lost.
            self.state.last_error = _error_text(error)
            logger.warning(
                "event consumer dead-letter failed eventId=%s type=%s topic=%s "
                "group=%s: %s",
                event.event_id,
                event.type,
                self.settings.events_topic,
                self.consumer_settings.group,
                self.state.last_error,
            )
            return "DEAD_LETTER_FAILED"

        self.processed.add(event.event_id)
        self._record_success()
        self._commit(message)
        logger.info(
            "event dead-lettered eventId=%s type=%s topic=%s group=%s attempts=%s",
            event.event_id,
            event.type,
            self.settings.events_topic,
            self.consumer_settings.group,
            attempt,
        )
        return "DEAD_LETTERED"

    def _record_success(self) -> None:
        """Count one handled event and stamp when it happened; memory only."""
        self.state.processed_count += 1
        self.state.last_event_at = self._now()

    def _commit(self, message: ConsumerMessage) -> None:
        """Commit this record's own offset: never a batch, never before it is handled."""
        self._client().commit(message=message)

    def status(self) -> KafkaConsumerStatus:
        """This process's consumer state, as the status route reports it."""
        return KafkaConsumerStatus(
            running=bool(self.state.running),
            topic=self.settings.events_topic,
            group=self.consumer_settings.group,
            processed_count=self.state.processed_count,
            last_event_at=self.state.last_event_at,
            last_error=self.state.last_error,
        )


def build_kafka_event_consumer(
    *,
    settings: KafkaSettings,
    consumer_settings: KafkaConsumerSettings,
    registry: HandlerRegistry,
    publisher: object | None,
    policy: DeliveryPolicy,
    state: KafkaConsumerState,
) -> KafkaEventConsumer | None:
    """The process's consumer, or None when the broker client cannot be built.

    It is built but never started here, so the lifespan owns both ends of its life.
    """
    try:
        client = _ConfluentConsumerAdapter(settings, consumer_settings)
    except Exception as exc:  # e.g. librdkafka is missing on this host
        logger.warning("kafka event consumer could not be built: %s", exc)
        return None
    return KafkaEventConsumer(
        settings=settings,
        consumer_settings=consumer_settings,
        registry=registry,
        publisher=publisher,
        policy=policy,
        state=state,
        consumer=client,
    )


def get_consumer_status(request: Request) -> KafkaConsumerStatus:
    """Read the consumer state `create_app` put on app.state, even when none was built."""
    state = getattr(request.app.state, "kafka_consumer_state", None)
    settings = getattr(request.app.state, "kafka_settings", None)
    consumer_settings = getattr(request.app.state, "kafka_consumer_settings", None)
    return KafkaConsumerStatus(
        running=bool(getattr(state, "running", False)),
        topic=settings.events_topic if settings is not None else "",
        group=(
            consumer_settings.group
            if consumer_settings is not None
            else DEFAULT_KAFKA_CONSUMER_GROUP
        ),
        processed_count=int(getattr(state, "processed_count", 0) or 0),
        last_event_at=getattr(state, "last_event_at", None),
        last_error=getattr(state, "last_error", None),
    )
