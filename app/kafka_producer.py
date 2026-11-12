"""Publishing domain events to Kafka through one producer shared by the whole process.

The producer is created once in the FastAPI lifespan, so a publish is a `produce`
followed by `poll(0)` and a `flush` bounded by `KAFKA_FLUSH_TIMEOUT_MS`. Every way
the broker can let us down -- an unreachable cluster, a produce that never queued,
a delivery report carrying an error, or a flush that could not drain -- raises
`KafkaUnavailableError`, which the app answers as a 503 ProblemDetail.
"""

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Request

logger = logging.getLogger(__name__)

DEFAULT_KAFKA_BOOTSTRAP_SERVERS = "localhost:9092"
DEFAULT_KAFKA_EVENTS_TOPIC = "bookmyshow.events"
DEFAULT_KAFKA_CLIENT_ID = "bookmyshow-api"
DEFAULT_FLUSH_TIMEOUT_MS = 5000
KAFKA_UNAVAILABLE_DETAIL = "Event broker unavailable"


def _env(name: str, default: str) -> str:
    """The environment value, or the default when it is absent or blank."""
    return (os.getenv(name) or "").strip() or default


@dataclass(frozen=True)
class KafkaSettings:
    """Where the broker lives and how long a publish may wait for it."""

    bootstrap_servers: str
    events_topic: str
    client_id: str
    flush_timeout_ms: int

    @classmethod
    def from_env(cls) -> "KafkaSettings":
        raw_timeout = (os.getenv("KAFKA_FLUSH_TIMEOUT_MS") or "").strip()
        timeout = DEFAULT_FLUSH_TIMEOUT_MS
        if raw_timeout:
            try:
                parsed = int(raw_timeout)
            except ValueError:
                parsed = DEFAULT_FLUSH_TIMEOUT_MS
            if parsed > 0:
                timeout = parsed
        return cls(
            bootstrap_servers=_env(
                "KAFKA_BOOTSTRAP_SERVERS", DEFAULT_KAFKA_BOOTSTRAP_SERVERS
            ),
            events_topic=_env("KAFKA_EVENTS_TOPIC", DEFAULT_KAFKA_EVENTS_TOPIC),
            client_id=_env("KAFKA_CLIENT_ID", DEFAULT_KAFKA_CLIENT_ID),
            flush_timeout_ms=timeout,
        )


class KafkaUnavailableError(Exception):
    """The event broker could not accept the record within the flush timeout."""

    def __init__(self, detail: str = KAFKA_UNAVAILABLE_DETAIL) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class DeliveryResult:
    """Where the broker placed the record this publish produced."""

    event_id: str
    topic: str
    partition: int
    offset: int


def build_envelope(
    *,
    event_id: str,
    event_type: str,
    version: int,
    occurred_at: datetime,
    payload: dict[str, object],
    key: str | None,
) -> dict[str, object]:
    """The shared envelope: `key` is only present when the caller supplied one."""
    envelope: dict[str, object] = {
        "eventId": event_id,
        "type": event_type,
        "version": version,
        "occurredAt": occurred_at.astimezone(timezone.utc).isoformat(),
        "payload": payload,
    }
    if key is not None:
        envelope["key"] = key
    return envelope


def _create_producer(settings: KafkaSettings) -> object:
    """Build the real producer; imported lazily so a host without librdkafka still boots."""
    from confluent_kafka import Producer

    return Producer(
        {
            "bootstrap.servers": settings.bootstrap_servers,
            "client.id": settings.client_id,
            "enable.idempotence": True,
            "acks": "all",
        }
    )


class EventPublisher:
    """Produces one envelope per publish and waits for exactly that record's report."""

    def __init__(self, settings: KafkaSettings, producer: object | None = None) -> None:
        self.settings = settings
        self._producer = producer if producer is not None else _create_producer(settings)
        self._delivered: object | None = None
        self._delivery_error: object | None = None

    def _on_delivery(self, error: object, message: object) -> None:
        """The delivery report librdkafka calls back with after the flush drains."""
        if error is not None:
            self._delivery_error = error
            return
        self._delivered = message

    def publish(
        self,
        *,
        event_type: str,
        payload: dict[str, object],
        version: int = 1,
        key: str | None = None,
        event_id: str | None = None,
    ) -> DeliveryResult:
        """Produce one event, waiting at most KAFKA_FLUSH_TIMEOUT_MS for its delivery."""
        event_id = event_id if event_id is not None else str(uuid.uuid4())
        envelope = build_envelope(
            event_id=event_id,
            event_type=event_type,
            version=version,
            occurred_at=datetime.now(timezone.utc),
            payload=payload,
            key=key,
        )
        self._delivered = None
        self._delivery_error = None

        try:
            self._producer.produce(
                self.settings.events_topic,
                value=json.dumps(envelope).encode("utf-8"),
                key=key,
                headers=[
                    ("eventId", event_id.encode("utf-8")),
                    ("type", event_type.encode("utf-8")),
                ],
                on_delivery=self._on_delivery,
            )
        except Exception as exc:  # librdkafka's own error type is a lazy import
            logger.warning("event produce failed eventId=%s: %s", event_id, exc)
            raise KafkaUnavailableError() from None

        self._producer.poll(0)
        remaining = self._producer.flush(self.settings.flush_timeout_ms / 1000)

        if self._delivery_error is not None:
            logger.warning(
                "event delivery failed eventId=%s: %s", event_id, self._delivery_error
            )
            raise KafkaUnavailableError()
        if remaining > 0 or self._delivered is None:
            logger.warning(
                "event not delivered within %sms eventId=%s",
                self.settings.flush_timeout_ms,
                event_id,
            )
            raise KafkaUnavailableError()

        result = DeliveryResult(
            event_id=event_id,
            topic=self.settings.events_topic,
            partition=int(self._delivered.partition()),
            offset=int(self._delivered.offset()),
        )
        logger.info(
            "event published eventId=%s type=%s topic=%s partition=%s offset=%s",
            result.event_id,
            event_type,
            result.topic,
            result.partition,
            result.offset,
        )
        return result

    @property
    def dead_letter_topic(self) -> str:
        """Where an event that spent every attempt is parked: `{topic}.dlq`."""
        return f"{self.settings.events_topic}.dlq"

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
        """Park one event on the DLQ topic, reusing the eventId every attempt carried."""
        topic = self.dead_letter_topic
        envelope = build_envelope(
            event_id=event_id,
            event_type=event_type,
            version=version,
            occurred_at=datetime.now(timezone.utc),
            payload=payload,
            key=key,
        )
        self._delivered = None
        self._delivery_error = None

        try:
            self._producer.produce(
                topic,
                value=json.dumps(envelope).encode("utf-8"),
                key=key,
                headers=[
                    ("eventId", event_id.encode("utf-8")),
                    ("type", event_type.encode("utf-8")),
                    ("attempts", str(attempts).encode("utf-8")),
                    ("lastError", last_error.encode("utf-8")),
                ],
                on_delivery=self._on_delivery,
            )
        except Exception as exc:  # librdkafka's own error type is a lazy import
            logger.warning(
                "dead-letter produce failed eventId=%s: %s", event_id, exc
            )
            raise KafkaUnavailableError() from None

        self._producer.poll(0)
        remaining = self._producer.flush(self.settings.flush_timeout_ms / 1000)

        if self._delivery_error is not None:
            logger.warning(
                "dead-letter delivery failed eventId=%s: %s",
                event_id,
                self._delivery_error,
            )
            raise KafkaUnavailableError()
        if remaining > 0 or self._delivered is None:
            logger.warning(
                "dead-letter not delivered within %sms eventId=%s",
                self.settings.flush_timeout_ms,
                event_id,
            )
            raise KafkaUnavailableError()

        result = DeliveryResult(
            event_id=event_id,
            topic=topic,
            partition=int(self._delivered.partition()),
            offset=int(self._delivered.offset()),
        )
        logger.info(
            "event dead-lettered eventId=%s type=%s topic=%s attempts=%s "
            "partition=%s offset=%s",
            result.event_id,
            event_type,
            result.topic,
            attempts,
            result.partition,
            result.offset,
        )
        return result

    def close(self) -> None:
        """Flush with the same bounded timeout so shutdown cannot hang."""
        try:
            self._producer.flush(self.settings.flush_timeout_ms / 1000)
        except Exception as exc:  # a broken broker must not stop the app shutting down
            logger.warning("event producer flush on shutdown failed: %s", exc)


def build_event_publisher(settings: KafkaSettings) -> EventPublisher | None:
    """The process's one publisher, or None when the broker client cannot be built."""
    try:
        return EventPublisher(settings)
    except Exception as exc:  # e.g. librdkafka is missing on this host
        logger.warning("event publisher could not be built: %s", exc)
        return None


def get_event_publisher(request: Request) -> EventPublisher:
    """Build a client from the publisher `create_app`'s lifespan put on app.state."""
    publisher = getattr(request.app.state, "event_publisher", None)
    if publisher is None:
        raise KafkaUnavailableError()
    return publisher
