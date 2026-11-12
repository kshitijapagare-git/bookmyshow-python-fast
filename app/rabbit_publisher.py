"""Publishing domain events to RabbitMQ through one publisher shared by the process.

Publishing is a `basic_publish` on a durable topic exchange declared once per
connection. The connection is opened lazily on the first publish, so a broker that is
down at boot does not poison the process: the publish raises `RabbitUnavailableError`
(answered as a 503 ProblemDetail) and the next publish opens a fresh connection.

The message body is the same JSON envelope the Kafka publisher builds, imported from
`app.kafka_producer` so the two brokers cannot drift. `pika` is imported only inside
the real-channel adapter, so this module imports on a host without the client.
"""

import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Request

from .event_metrics import DEFAULT_HEALTH_TIMEOUT_S, log_event
from .kafka_producer import build_envelope

logger = logging.getLogger(__name__)

DEFAULT_RABBITMQ_URL = "amqp://guest:guest@localhost:5672/"
DEFAULT_RABBITMQ_EXCHANGE = "bookmyshow.events"
DEFAULT_RABBITMQ_ROUTING_KEY_PREFIX = ""
RABBIT_UNAVAILABLE_DETAIL = "Event broker unavailable"
EXCHANGE_TYPE = "topic"
PERSISTENT_DELIVERY_MODE = 2
CONTENT_TYPE = "application/json"


def _env(name: str, default: str) -> str:
    """The environment value, or the default when it is absent or blank."""
    return (os.getenv(name) or "").strip() or default


@dataclass(frozen=True)
class RabbitSettings:
    """Where the broker and the exchange live, and how a routing key is built."""

    url: str
    exchange: str
    routing_key_prefix: str

    @classmethod
    def from_env(cls) -> "RabbitSettings":
        return cls(
            url=_env("RABBITMQ_URL", DEFAULT_RABBITMQ_URL),
            exchange=_env("RABBITMQ_EXCHANGE", DEFAULT_RABBITMQ_EXCHANGE),
            routing_key_prefix=_env(
                "RABBITMQ_ROUTING_KEY_PREFIX", DEFAULT_RABBITMQ_ROUTING_KEY_PREFIX
            ),
        )

    def routing_key_for(self, event_type: str) -> str:
        """`{prefix}.{type}`, or just the type when no prefix is configured."""
        prefix = self.routing_key_prefix.strip().rstrip(".").strip()
        if not prefix:
            return event_type
        return f"{prefix}.{event_type}"


class RabbitUnavailableError(Exception):
    """The event broker could not accept the message."""

    def __init__(self, detail: str = RABBIT_UNAVAILABLE_DETAIL) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class RabbitMessageProperties:
    """The AMQP properties every published event carries."""

    delivery_mode: int
    message_id: str
    headers: dict[str, str]
    content_type: str


@dataclass(frozen=True)
class RabbitDeliveryResult:
    """Where the broker was asked to place the message this publish produced."""

    event_id: str
    exchange: str
    routing_key: str


class _PikaChannelAdapter:
    """A real pika channel, opened on first use so a missing client cannot break boot."""

    def __init__(self, settings: RabbitSettings) -> None:
        import pika

        self._pika = pika
        self._connection = pika.BlockingConnection(pika.URLParameters(settings.url))
        self._channel = self._connection.channel()

    def exchange_declare(
        self, *, exchange: str, exchange_type: str, durable: bool
    ) -> None:
        self._channel.exchange_declare(
            exchange=exchange, exchange_type=exchange_type, durable=durable
        )

    def basic_publish(
        self,
        exchange: str,
        routing_key: str,
        body: bytes,
        properties: RabbitMessageProperties,
    ) -> None:
        self._channel.basic_publish(
            exchange=exchange,
            routing_key=routing_key,
            body=body,
            properties=self._pika.BasicProperties(
                delivery_mode=properties.delivery_mode,
                message_id=properties.message_id,
                headers=properties.headers,
                content_type=properties.content_type,
            ),
        )

    def close(self) -> None:
        self._connection.close()


class RabbitPublisher:
    """Declares the durable topic exchange once, then publishes one message per call."""

    def __init__(
        self, settings: RabbitSettings, channel: object | None = None
    ) -> None:
        self.settings = settings
        self._channel = channel
        self._injected = channel is not None
        self._exchange_declared = False
        self._lock = threading.Lock()

    def _open_channel(self) -> object:
        """The channel to publish on, opening the real connection when there is none."""
        if self._channel is None:
            self._channel = _PikaChannelAdapter(self.settings)
        return self._channel

    def _ensure_exchange(self, channel: object) -> None:
        """Declare the durable topic exchange idempotently, once per connection."""
        if self._exchange_declared:
            return
        channel.exchange_declare(
            exchange=self.settings.exchange,
            exchange_type=EXCHANGE_TYPE,
            durable=True,
        )
        self._exchange_declared = True

    def _reset(self) -> None:
        """Forget a connection that failed so the next publish opens a fresh one."""
        self._exchange_declared = False
        if self._injected:
            return
        channel = self._channel
        self._channel = None
        if channel is None:
            return
        try:
            channel.close()
        except Exception as exc:  # a broken connection must not mask the real failure
            logger.warning("event channel close after a failure failed: %s", exc)

    def publish(
        self,
        *,
        event_type: str,
        payload: dict[str, object],
        version: int = 1,
        key: str | None = None,
        event_id: str | None = None,
    ) -> RabbitDeliveryResult:
        """Publish one persistent event, raising RabbitUnavailableError on any failure."""
        event_id = event_id if event_id is not None else str(uuid.uuid4())
        envelope = build_envelope(
            event_id=event_id,
            event_type=event_type,
            version=version,
            occurred_at=datetime.now(timezone.utc),
            payload=payload,
            key=key,
        )
        routing_key = self.settings.routing_key_for(event_type)
        properties = RabbitMessageProperties(
            delivery_mode=PERSISTENT_DELIVERY_MODE,
            message_id=event_id,
            headers={
                "eventId": event_id,
                "type": event_type,
                "correlationId": key if key is not None else event_id,
            },
            content_type=CONTENT_TYPE,
        )
        body = json.dumps(envelope).encode("utf-8")

        with self._lock:
            try:
                channel = self._open_channel()
                self._ensure_exchange(channel)
                channel.basic_publish(
                    exchange=self.settings.exchange,
                    routing_key=routing_key,
                    body=body,
                    properties=properties,
                )
            except Exception as exc:  # pika's own error types are a lazy import
                log_event(
                    logger,
                    logging.WARNING,
                    "event publish failed",
                    event_id=event_id,
                    event_type=event_type,
                    transport="RABBIT",
                    detail=(
                        f"exchange={self.settings.exchange} "
                        f"routingKey={routing_key} error={exc}"
                    ),
                )
                self._reset()
                raise RabbitUnavailableError() from None

        log_event(
            logger,
            logging.INFO,
            "event published",
            event_id=event_id,
            event_type=event_type,
            transport="RABBIT",
            detail=f"exchange={self.settings.exchange} routingKey={routing_key}",
        )
        return RabbitDeliveryResult(
            event_id=event_id,
            exchange=self.settings.exchange,
            routing_key=routing_key,
        )

    def dead_letter_routing_key(self, event_type: str) -> str:
        """`{routing_key}.dlq`: the producer-side contract for the dead-letter route."""
        return f"{self.settings.routing_key_for(event_type)}.dlq"

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
        """Park one event on the `.dlq` route, reusing the eventId every attempt carried."""
        envelope = build_envelope(
            event_id=event_id,
            event_type=event_type,
            version=version,
            occurred_at=datetime.now(timezone.utc),
            payload=payload,
            key=key,
        )
        routing_key = self.dead_letter_routing_key(event_type)
        properties = RabbitMessageProperties(
            delivery_mode=PERSISTENT_DELIVERY_MODE,
            message_id=event_id,
            headers={
                "eventId": event_id,
                "type": event_type,
                "correlationId": key if key is not None else event_id,
                "attempts": str(attempts),
                "lastError": last_error,
            },
            content_type=CONTENT_TYPE,
        )
        body = json.dumps(envelope).encode("utf-8")

        with self._lock:
            try:
                channel = self._open_channel()
                self._ensure_exchange(channel)
                channel.basic_publish(
                    exchange=self.settings.exchange,
                    routing_key=routing_key,
                    body=body,
                    properties=properties,
                )
            except Exception as exc:  # pika's own error types are a lazy import
                log_event(
                    logger,
                    logging.WARNING,
                    "dead-letter publish failed",
                    event_id=event_id,
                    event_type=event_type,
                    transport="RABBIT",
                    attempt=attempts,
                    detail=(
                        f"exchange={self.settings.exchange} "
                        f"routingKey={routing_key} error={exc}"
                    ),
                )
                self._reset()
                raise RabbitUnavailableError() from None

        log_event(
            logger,
            logging.INFO,
            "event dead-lettered",
            event_id=event_id,
            event_type=event_type,
            transport="RABBIT",
            attempt=attempts,
            detail=f"exchange={self.settings.exchange} routingKey={routing_key}",
        )
        return RabbitDeliveryResult(
            event_id=event_id,
            exchange=self.settings.exchange,
            routing_key=routing_key,
        )

    def check_health(self, *, timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S) -> bool:
        """Whether the broker answers an idempotent exchange declare; never raises.

        Re-declaring the durable topic exchange is idempotent and is a real broker
        round-trip, so a broker that is gone reads as down without failing the
        health route; pika's own socket timeout bounds the wait, which is why
        `timeout_s` only keeps the shared probe signature.
        """
        with self._lock:
            try:
                channel = self._open_channel()
                channel.exchange_declare(
                    exchange=self.settings.exchange,
                    exchange_type=EXCHANGE_TYPE,
                    durable=True,
                )
            except Exception as exc:  # never let a broker failure reach the route
                logger.warning("rabbit event broker health check failed: %s", exc)
                self._reset()
                return False
            self._exchange_declared = True
        return True

    def close(self) -> None:
        """Close the channel so shutdown cannot hang on a broken connection."""
        with self._lock:
            channel = self._channel
            self._channel = None
            self._exchange_declared = False
        if channel is None:
            return
        try:
            channel.close()
        except Exception as exc:  # a broken broker must not stop the app shutting down
            logger.warning("event channel close on shutdown failed: %s", exc)


def build_rabbit_publisher(settings: RabbitSettings) -> RabbitPublisher | None:
    """The process's one publisher, or None when it cannot be constructed."""
    try:
        return RabbitPublisher(settings)
    except Exception as exc:
        logger.warning("rabbit event publisher could not be built: %s", exc)
        return None


def get_rabbit_publisher(request: Request) -> RabbitPublisher:
    """Build a client from the publisher `create_app`'s lifespan put on app.state."""
    publisher = getattr(request.app.state, "rabbit_publisher", None)
    if publisher is None:
        raise RabbitUnavailableError()
    return publisher
