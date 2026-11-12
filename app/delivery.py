"""Retry policy, backoff schedule and transient-error classification for event delivery.

The shared wrapper in this module retries only the failures a second attempt can fix --
an unreachable broker or a publish the broker client refused -- and never a validation
or business error. The gap between attempts grows exponentially from
`DELIVERY_BASE_DELAY_MS` up to `DELIVERY_MAX_DELAY_MS` with symmetric jitter, and the
four environment variables follow the same fall-back-not-raise convention as
`KafkaSettings` and `RabbitSettings`.

`EventDeliveryService` is the one place both brokers are retried from: it generates the
eventId once, records the attempt in `event_deliveries`, and parks an event that spent
every attempt on the broker's dead-letter destination. The record is bookkeeping, never
part of a business transaction, so a broker failure rolls nothing back.
"""

import os
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .kafka_producer import KafkaUnavailableError
from .models import EventDelivery
from .rabbit_publisher import RabbitUnavailableError

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY_MS = 100
DEFAULT_MAX_DELAY_MS = 2000
DEFAULT_JITTER = 0.2

DELIVERY_STATUSES: tuple[str, ...] = (
    "PENDING",
    "RETRYING",
    "DELIVERED",
    "DEAD_LETTERED",
)

TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    KafkaUnavailableError,
    RabbitUnavailableError,
)


def _env(name: str, default: str) -> str:
    """The environment value, or the default when it is absent or blank."""
    return (os.getenv(name) or "").strip() or default


def _positive_int(name: str, default: int) -> int:
    """A positive integer from the environment; anything else falls back."""
    raw = _env(name, "")
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _jitter_fraction(name: str, default: float) -> float:
    """A jitter fraction between 0 and 1; anything else falls back."""
    raw = _env(name, "")
    if not raw:
        return default
    try:
        parsed = float(raw)
    except ValueError:
        return default
    return parsed if 0.0 <= parsed <= 1.0 else default


@dataclass(frozen=True)
class DeliveryPolicy:
    """How many attempts an event gets and how long the gaps between them grow."""

    max_attempts: int
    base_delay_ms: int
    max_delay_ms: int
    jitter: float

    @classmethod
    def from_env(cls) -> "DeliveryPolicy":
        return cls(
            max_attempts=_positive_int("DELIVERY_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS),
            base_delay_ms=_positive_int(
                "DELIVERY_BASE_DELAY_MS", DEFAULT_BASE_DELAY_MS
            ),
            max_delay_ms=_positive_int("DELIVERY_MAX_DELAY_MS", DEFAULT_MAX_DELAY_MS),
            jitter=_jitter_fraction("DELIVERY_JITTER", DEFAULT_JITTER),
        )


def is_transient_error(exc: BaseException) -> bool:
    """Whether a second attempt could succeed; only the broker clients' errors can."""
    return isinstance(exc, TRANSIENT_ERRORS)


def next_delay_ms(
    policy: DeliveryPolicy, attempt: int, *, rng: Callable[[], float] = random.random
) -> int:
    """The gap after `attempt` (1-based): exponential and capped, then jittered."""
    raw = min(policy.max_delay_ms, policy.base_delay_ms * 2 ** (max(attempt, 1) - 1))
    return int(round(raw * (1 + policy.jitter * (2 * rng() - 1))))


@dataclass(frozen=True)
class DeliveryOutcome:
    """What one `deliver` call ended as: the eventId, its status and the broker's answer."""

    event_id: str
    status: str
    attempts: int
    result: object


Transport = Literal["KAFKA", "RABBIT"]


def _error_text(error: BaseException) -> str:
    """The message stored in `last_error`; a blank error falls back to its type name."""
    message = str(error).strip() or error.__class__.__name__
    return message[:1000]


class EventDeliveryService:
    """Publishes one event through a broker, retrying transient failures only.

    Every attempt carries the same eventId, so a retried event is de-duplicable by the
    id the caller reads in the 202 body, and both the retry and the dead-letter copy
    carry it too. The row is committed after each transition, which is what lets a
    concurrent `GET /api/events/deliveries/{eventId}` observe RETRYING.
    """

    def __init__(
        self,
        *,
        publisher: object,
        policy: DeliveryPolicy,
        db: Session,
        transport: Transport,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.publisher = publisher
        self.policy = policy
        self.db = db
        self.transport = transport
        self.publish_call: Callable[..., object] = publisher.publish
        self.dead_letter_call: Callable[..., object] = publisher.publish_dead_letter
        self._sleep = sleep
        self._rng = rng
        self._now = now

    def deliver(
        self,
        *,
        event_type: str,
        payload: dict[str, object],
        version: int = 1,
        key: str | None = None,
    ) -> DeliveryOutcome:
        """Publish one event, retrying transient failures and dead-lettering on exhaustion."""
        event_id = str(uuid.uuid4())
        row = EventDelivery(
            event_id=event_id,
            transport=self.transport,
            event_type=event_type,
            status="PENDING",
            attempts=0,
            last_error=None,
            next_retry_at=None,
        )
        self.db.add(row)
        self.db.commit()

        attempt = 0
        while True:
            attempt += 1
            try:
                result = self.publish_call(
                    event_type=event_type,
                    payload=payload,
                    version=version,
                    key=key,
                    event_id=event_id,
                )
            except Exception as error:
                if not is_transient_error(error):
                    # A rejected event is not a lost one: record it and never retry.
                    self._write_state(
                        row, attempt=attempt, last_error=_error_text(error)
                    )
                    raise
                if attempt >= self.policy.max_attempts:
                    return self._dead_letter(
                        row=row,
                        event_type=event_type,
                        payload=payload,
                        version=version,
                        key=key,
                        attempt=attempt,
                        error=error,
                    )
                delay_ms = next_delay_ms(self.policy, attempt, rng=self._rng)
                self._write_state(
                    row,
                    attempt=attempt,
                    status="RETRYING",
                    last_error=_error_text(error),
                    next_retry_at=self._now() + timedelta(milliseconds=delay_ms),
                )
                self._sleep(delay_ms / 1000)
                continue

            self._write_state(row, attempt=attempt, status="DELIVERED")
            return DeliveryOutcome(
                event_id=event_id,
                status="DELIVERED",
                attempts=attempt,
                result=result,
            )

    def get(self, event_id: str) -> EventDelivery | None:
        """The delivery row keyed by `event_id`, or None when no attempt was ever made."""
        return self.db.scalar(
            select(EventDelivery).where(EventDelivery.event_id == event_id)
        )

    def _dead_letter(
        self,
        *,
        row: EventDelivery,
        event_type: str,
        payload: dict[str, object],
        version: int,
        key: str | None,
        attempt: int,
        error: BaseException,
    ) -> DeliveryOutcome:
        """Park the event on the dead-letter destination; a failure re-raises the broker error."""
        last_error = _error_text(error)
        try:
            result = self.dead_letter_call(
                event_id=row.event_id,
                event_type=event_type,
                payload=payload,
                version=version,
                key=key,
                attempts=attempt,
                last_error=last_error,
            )
        except Exception:
            # Neither published nor dead-lettered, so the caller must see the broker error
            # rather than a 202 it did not earn.
            self._write_state(
                row, attempt=attempt, status="RETRYING", last_error=last_error
            )
            raise error from None

        self._write_state(
            row, attempt=attempt, status="DEAD_LETTERED", last_error=last_error
        )
        return DeliveryOutcome(
            event_id=row.event_id,
            status="DEAD_LETTERED",
            attempts=attempt,
            result=result,
        )

    def _write_state(
        self,
        row: EventDelivery,
        *,
        attempt: int,
        status: str | None = None,
        last_error: str | None = None,
        next_retry_at: datetime | None = None,
    ) -> None:
        """Record the attempt just made and commit it, so a reader sees it right away."""
        if status is not None:
            row.status = status
        row.attempts = attempt
        row.last_error = last_error
        row.next_retry_at = next_retry_at
        row.updated_at = self._now()
        self.db.commit()


def get_delivery_policy(request: Request) -> DeliveryPolicy:
    """The policy `create_app` put on app.state, so the environment is read once."""
    return request.app.state.delivery_policy
