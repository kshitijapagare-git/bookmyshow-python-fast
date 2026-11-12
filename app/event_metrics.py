"""In-memory event counters, their settings and the payload-free log helper.

Nothing in this module imports another module from `app`, so `delivery.py`,
`event_bus.py`, `kafka_producer.py`, `kafka_consumer.py` and `rabbit_publisher.py`
can all import it without a cycle -- the same constraint `event_bus.py` documents
for `app.schemas`. That is why the environment helpers are declared locally rather
than reused from `app.delivery`, and why this module owns
`DEFAULT_HEALTH_TIMEOUT_S`, the timeout the two broker probes default to.

`EventMetrics` is one process-wide registry: a dict keyed by
`(event_type, transport)` of five counters behind a `threading.Lock`, because the
Kafka consumer thread and the request threads increment the same object. Nothing is
persisted, so a restarted process starts from zero -- the ticket asks for in-memory
counters only. `EventMetricsSettings` reads `EVENT_LOG_LIMIT` and
`EVENT_METRICS_FORMAT` with the same fall-back-not-raise convention as
`KafkaSettings`, `RabbitSettings` and `EventBusSettings`: an absent, blank or
unparseable value selects the default instead of raising.

`log_event` carries the three identifiers every event record needs -- `eventId`,
`correlationId` and `transport` -- and deliberately takes only scalar keyword
parameters, so a payload or body cannot physically reach a log line.
"""

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from fastapi import Request

DEFAULT_EVENT_LOG_LIMIT = 50
DEFAULT_EVENT_METRICS_FORMAT = "json"
EVENT_METRICS_FORMATS: tuple[str, ...] = ("json", "prometheus")
EVENT_METRICS_FORMAT_ENV = "EVENT_METRICS_FORMAT"
EVENT_LOG_LIMIT_ENV = "EVENT_LOG_LIMIT"
DEFAULT_HEALTH_TIMEOUT_S = 2.0


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


@dataclass(frozen=True)
class EventMetricsSettings:
    """How many `/log` entries a default read returns, and how `/metrics` answers."""

    log_limit: int
    format: str

    @classmethod
    def from_env(cls) -> "EventMetricsSettings":
        """The configured settings; anything unrecognised falls back, never raises."""
        configured = _env(EVENT_METRICS_FORMAT_ENV, "").lower()
        return cls(
            log_limit=_positive_int(EVENT_LOG_LIMIT_ENV, DEFAULT_EVENT_LOG_LIMIT),
            format=(
                configured
                if configured in EVENT_METRICS_FORMATS
                else DEFAULT_EVENT_METRICS_FORMAT
            ),
        )


@dataclass(frozen=True)
class MetricsBucket:
    """The five counters one (event type, transport) pair has accumulated."""

    event_type: str
    transport: str
    published: int = 0
    consumed: int = 0
    failed: int = 0
    retried: int = 0
    dead_lettered: int = 0


_COUNTER_FIELDS: tuple[str, ...] = (
    "published",
    "consumed",
    "failed",
    "retried",
    "dead_lettered",
)

_PROMETHEUS_NAMES: tuple[tuple[str, str], ...] = (
    ("published", "event_published_total"),
    ("consumed", "event_consumed_total"),
    ("failed", "event_failed_total"),
    ("retried", "event_retried_total"),
    ("dead_lettered", "event_dead_lettered_total"),
)


class EventMetrics:
    """The process's event counters, keyed by event type and transport.

    Every transition the ticket names has its own `record_*` method and touches only
    its own counter. The registry is shared by the request threads and the Kafka
    consumer thread, so every read and every increment happens under one lock.
    """

    def __init__(
        self,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], MetricsBucket] = {}
        self._last_event_at: datetime | None = None

    def record_published(self, *, event_type: str, transport: str) -> None:
        self._record(event_type=event_type, transport=transport, field="published")

    def record_consumed(self, *, event_type: str, transport: str) -> None:
        self._record(event_type=event_type, transport=transport, field="consumed")

    def record_failed(self, *, event_type: str, transport: str) -> None:
        self._record(event_type=event_type, transport=transport, field="failed")

    def record_retried(self, *, event_type: str, transport: str) -> None:
        self._record(event_type=event_type, transport=transport, field="retried")

    def record_dead_lettered(self, *, event_type: str, transport: str) -> None:
        self._record(event_type=event_type, transport=transport, field="dead_lettered")

    def snapshot(self) -> tuple[MetricsBucket, ...]:
        """Every bucket that has been recorded, ordered by (event type, transport)."""
        with self._lock:
            return tuple(self._buckets[key] for key in sorted(self._buckets))

    @property
    def last_event_at(self) -> datetime | None:
        """When any counter last moved, or None before the first record."""
        with self._lock:
            return self._last_event_at

    def reset(self) -> None:
        """Drop every counter and the timestamp; the tests reset between cases."""
        with self._lock:
            self._buckets.clear()
            self._last_event_at = None

    def _record(self, *, event_type: str, transport: str, field: str) -> None:
        """Increment exactly one counter of one bucket and stamp the clock."""
        if field not in _COUNTER_FIELDS:
            raise ValueError(f"unknown counter: {field}")
        key = (event_type, transport)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = MetricsBucket(event_type=event_type, transport=transport)
            self._buckets[key] = replace(
                bucket, **{field: getattr(bucket, field) + 1}
            )
            self._last_event_at = self._now()


def _label(value: str) -> str:
    """A label value with the characters Prometheus' text format reserves escaped."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return escaped.replace("\n", "\\n")


def prometheus_text(
    buckets: tuple[MetricsBucket, ...], last_event_at: datetime | None
) -> str:
    """The same snapshot as Prometheus' text exposition format."""
    lines: list[str] = []
    for field, name in _PROMETHEUS_NAMES:
        lines.append(f"# TYPE {name} counter")
        for bucket in buckets:
            lines.append(
                f'{name}{{type="{_label(bucket.event_type)}",'
                f'transport="{_label(bucket.transport)}"}} '
                f"{getattr(bucket, field)}"
            )

    lines.append("# TYPE event_last_event_at_timestamp_seconds gauge")
    seconds = 0 if last_event_at is None else int(last_event_at.timestamp())
    lines.append(f"event_last_event_at_timestamp_seconds {seconds}")
    return "\n".join(lines) + "\n"


def log_event(
    logger: logging.Logger,
    level: int,
    message: str,
    *,
    event_id: str,
    event_type: str,
    transport: str,
    correlation_id: str | None = None,
    status: str | None = None,
    attempt: int | None = None,
    topic: str | None = None,
    detail: str | None = None,
) -> None:
    """Log one event record with its identifiers; a payload can never be passed in.

    `correlationId` defaults to the `eventId`, which is the clarifying decision for
    this change: the delivery record carries no correlation id of its own.
    """
    parts = [
        message,
        f"eventId={event_id}",
        f"correlationId={event_id if correlation_id is None else correlation_id}",
        f"type={event_type}",
        f"transport={transport}",
    ]
    if status is not None:
        parts.append(f"status={status}")
    if attempt is not None:
        parts.append(f"attempt={attempt}")
    if topic is not None:
        parts.append(f"topic={topic}")
    if detail is not None:
        parts.append(f"detail={detail}")
    logger.log(level, " ".join(parts))


def get_event_metrics(request: Request) -> EventMetrics:
    """The registry `create_app` put on app.state, built and cached when none was wired."""
    metrics = getattr(request.app.state, "event_metrics", None)
    if isinstance(metrics, EventMetrics):
        return metrics
    built = EventMetrics()
    request.app.state.event_metrics = built
    return built


def get_event_metrics_settings(request: Request) -> EventMetricsSettings:
    """The settings `create_app` put on app.state, or the environment's own."""
    settings = getattr(request.app.state, "event_metrics_settings", None)
    if isinstance(settings, EventMetricsSettings):
        return settings
    return EventMetricsSettings.from_env()
