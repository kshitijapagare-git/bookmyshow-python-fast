"""The broker-agnostic event bus: the envelope, the transport vocabulary and settings.

`EventEnvelope` is the Pydantic model a caller publishes and a handler receives, and
`EventBusTransport` is the bus's own three-value vocabulary -- MEMORY, KAFKA and
RABBIT -- deliberately kept separate from `app.delivery.Transport`, which stays
KAFKA/RABBIT because it is the value the `event_deliveries` column stores. The two
spellings match on purpose, so an event routed to KAFKA means the same thing in both
places.

`EventBusSettings` reads `EVENT_BUS_TRANSPORT` with the same
fall-back-not-raise convention as `KafkaSettings` and `RabbitSettings`: an absent,
blank or unrecognised value selects `MEMORY` rather than raising. The Kafka and
Rabbit broker settings themselves stay owned by those two classes and are not
re-read here.

`EventBus` is the one place a domain service publishes through: it hands the envelope
to the selected transport and only then dispatches it to the in-process subscribers --
exact-type registrations first, then `*` -- isolating each handler so a failing one is
reported instead of surfacing to the publisher. `subscribe` registers in-process
handlers on every transport; it never consumes from a broker.

Nothing in this module imports `app.schemas`, so `app.schemas` may import
`EventBusTransport` from here without a cycle. That is why the constrained
non-blank string is declared locally as `NonBlankText` instead of being imported.
"""

import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal, cast

from fastapi import Request
from pydantic import BaseModel, Field, StringConstraints

from .delivery import _error_text
from .kafka_producer import KafkaUnavailableError
from .rabbit_publisher import RabbitUnavailableError

logger = logging.getLogger(__name__)

EventBusTransport = Literal["MEMORY", "KAFKA", "RABBIT"]

EVENT_BUS_TRANSPORTS: tuple[str, ...] = ("MEMORY", "KAFKA", "RABBIT")
DEFAULT_EVENT_BUS_TRANSPORT = "MEMORY"
EVENT_BUS_TRANSPORT_ENV = "EVENT_BUS_TRANSPORT"
WILDCARD_EVENT_TYPE = "*"

NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _env(name: str, default: str) -> str:
    """The environment value, or the default when it is absent or blank."""
    return (os.getenv(name) or "").strip() or default


@dataclass(frozen=True)
class EventBusSettings:
    """Which transport the bus publishes through, as `EVENT_BUS_TRANSPORT` selects."""

    transport: EventBusTransport

    @classmethod
    def from_env(cls) -> "EventBusSettings":
        """The configured transport; anything unrecognised falls back to memory."""
        configured = _env(EVENT_BUS_TRANSPORT_ENV, "").upper()
        return cls(
            transport=cast(
                EventBusTransport,
                configured
                if configured in EVENT_BUS_TRANSPORTS
                else DEFAULT_EVENT_BUS_TRANSPORT,
            )
        )


class EventEnvelope(BaseModel):
    """One event on the bus: the ticket's six fields and nothing else.

    `id` and `occurredAt` are generated when the caller does not supply them, so a
    caller may pin either, but the same validation still applies to what it sends.
    The envelope carries no `key`: the broker wire envelope built by
    `kafka_producer.build_envelope` keeps that field to itself.
    """

    id: NonBlankText = Field(default_factory=lambda: str(uuid.uuid4()))
    type: NonBlankText
    version: int = Field(default=1, ge=1)
    occurredAt: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, object]
    metadata: dict[str, object] = Field(default_factory=dict)


Handler = Callable[[EventEnvelope], None]


def _handler_name(handler: Handler) -> str:
    """A function's `__name__`, otherwise its class name, so a report is readable."""
    explicit = getattr(handler, "__name__", None)
    if isinstance(explicit, str) and explicit.strip():
        return explicit
    return type(handler).__name__


@dataclass(frozen=True)
class HandlerError:
    """One handler that raised while a publish was being dispatched."""

    event_type: str
    handler: str
    error: str


@dataclass(frozen=True)
class Subscription:
    """One registration: the event type it listens for and the handler itself."""

    event_type: str
    handler: Handler

    @property
    def name(self) -> str:
        return _handler_name(self.handler)


@dataclass(frozen=True)
class PublishResult:
    """What one publish ended as: the envelope's id and the handlers that failed."""

    event_id: str
    handler_errors: tuple[HandlerError, ...]


class EventBus:
    """Publishes an envelope through one transport, then dispatches it locally.

    Transport first, subscribers second: an event the broker refused raises before any
    handler runs, so a 5xx never pretends the event was handled. Subscribers live in
    process only, which is what makes `publish`/`subscribe` mean the same thing on
    every transport. Each handler is isolated, so a failing one is reported and the
    rest still run.
    """

    def __init__(
        self,
        *,
        transport: EventBusTransport,
        kafka_publisher: object | None = None,
        rabbit_publisher: object | None = None,
    ) -> None:
        self._transport = transport
        self._kafka_publisher = kafka_publisher
        self._rabbit_publisher = rabbit_publisher
        self._subscriptions: dict[str, list[Handler]] = {}

    @property
    def transport(self) -> EventBusTransport:
        """The transport this bus was built with; the listing route reports it."""
        return self._transport

    def bind_publishers(
        self,
        *,
        kafka_publisher: object | None,
        rabbit_publisher: object | None,
    ) -> None:
        """Attach the process's publishers without touching the registrations."""
        self._kafka_publisher = kafka_publisher
        self._rabbit_publisher = rabbit_publisher

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """Register one handler for an exact type, or for the `*` wildcard."""
        normalised = event_type.strip() if isinstance(event_type, str) else ""
        if not normalised:
            raise ValueError("event type must not be blank")
        self._subscriptions.setdefault(normalised, []).append(handler)

    def subscriptions(self) -> tuple[Subscription, ...]:
        """Every registration, in the order it was made, the wildcard included."""
        return tuple(
            Subscription(event_type=event_type, handler=handler)
            for event_type, handlers in self._subscriptions.items()
            for handler in handlers
        )

    def publish(self, event: EventEnvelope) -> PublishResult:
        """Send the envelope through the transport, then run its subscribers."""
        self._send(event)
        errors = self._dispatch(event)
        return PublishResult(event_id=event.id, handler_errors=errors)

    def _send(self, event: EventEnvelope) -> None:
        """Hand the envelope to the selected transport; memory touches no broker."""
        if self._transport == "MEMORY":
            return
        if self._transport == "KAFKA":
            self._publish(self._kafka_publisher, event, transport="KAFKA")
            return
        self._publish(self._rabbit_publisher, event, transport="RABBIT")

    def _publish(
        self, publisher: object | None, event: EventEnvelope, *, transport: str
    ) -> None:
        """Forward the envelope's id, type, version and payload to one broker."""
        publish_call = getattr(publisher, "publish", None)
        if not callable(publish_call):
            # No usable publisher, so the caller must see the broker error rather than
            # a 202 for an event nothing accepted.
            if transport == "RABBIT":
                raise RabbitUnavailableError()
            raise KafkaUnavailableError()

        try:
            publish_call(
                event_type=event.type,
                payload=event.payload,
                version=event.version,
                key=None,
                event_id=event.id,
            )
        except Exception as error:
            logger.warning(
                "event bus publish failed eventId=%s type=%s transport=%s: %s",
                event.id,
                event.type,
                transport,
                _error_text(error),
            )
            raise

        logger.info(
            "event bus published eventId=%s type=%s transport=%s",
            event.id,
            event.type,
            transport,
        )

    def _dispatch(self, event: EventEnvelope) -> tuple[HandlerError, ...]:
        """Run exact-type subscribers, then wildcard ones, each isolated from the rest."""
        handlers = [
            *self._subscriptions.get(event.type, ()),
            *self._subscriptions.get(WILDCARD_EVENT_TYPE, ()),
        ]
        errors: list[HandlerError] = []
        for handler in handlers:
            name = _handler_name(handler)
            try:
                handler(event)
            except Exception as error:  # one bad handler must not stop the others
                text = _error_text(error)
                errors.append(
                    HandlerError(event_type=event.type, handler=name, error=text)
                )
                logger.warning(
                    "event bus handler failed eventId=%s type=%s handler=%s "
                    "transport=%s error=%s",
                    event.id,
                    event.type,
                    name,
                    self._transport,
                    text,
                )
        return tuple(errors)


def build_event_bus(
    *,
    settings: EventBusSettings,
    kafka_publisher: object | None = None,
    rabbit_publisher: object | None = None,
) -> EventBus:
    """The bus for these settings; a missing broker client is bound later, never fatal."""
    return EventBus(
        transport=settings.transport,
        kafka_publisher=kafka_publisher,
        rabbit_publisher=rabbit_publisher,
    )


def get_event_bus(request: Request) -> EventBus:
    """The bus `create_app` put on app.state, built and cached when none was wired."""
    bus = getattr(request.app.state, "event_bus", None)
    if isinstance(bus, EventBus):
        return bus
    built = build_event_bus(settings=EventBusSettings.from_env())
    request.app.state.event_bus = built
    return built
