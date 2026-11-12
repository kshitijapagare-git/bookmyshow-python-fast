"""Publishing domain events to Kafka, behind the shared service token."""

from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..delivery import DeliveryPolicy, EventDeliveryService, get_delivery_policy
from ..event_bus import EventBus, EventEnvelope, get_event_bus
from ..event_metrics import (
    EventMetrics,
    EventMetricsSettings,
    get_event_metrics,
    get_event_metrics_settings,
    prometheus_text,
)
from ..kafka_consumer import get_consumer_status
from ..kafka_producer import DeliveryResult, EventPublisher, get_event_publisher
from ..models import EventDelivery
from ..rabbit_publisher import (
    RabbitDeliveryResult,
    RabbitPublisher,
    get_rabbit_publisher,
)
from ..schemas import (
    DeliveryStatusOut,
    EventBusHandlerErrorOut,
    EventBusPublishIn,
    EventBusPublishOut,
    EventBusSubscriptionOut,
    EventHealthOut,
    EventLogEntryOut,
    EventLogOut,
    EventMetricsBucketOut,
    EventMetricsOut,
    EventPublishIn,
    EventPublishOut,
    EventSubscriptionsOut,
    KafkaConsumerStatusOut,
    RabbitPublishOut,
)
from ..security import require_service_token

router = APIRouter(
    prefix="/api/events",
    tags=["events"],
    dependencies=[Depends(require_service_token)],
)


@router.post(
    "/kafka", response_model=EventPublishOut, status_code=status.HTTP_202_ACCEPTED
)
def publish_kafka_event(
    payload: EventPublishIn,
    db: Session = Depends(get_db),
    policy: DeliveryPolicy = Depends(get_delivery_policy),
    publisher: EventPublisher = Depends(get_event_publisher),
    metrics: EventMetrics = Depends(get_event_metrics),
) -> EventPublishOut:
    """Publish through the shared retry wrapper; a 503 means it was never parked."""
    outcome = EventDeliveryService(
        publisher=publisher,
        policy=policy,
        db=db,
        transport="KAFKA",
        metrics=metrics,
    ).deliver(
        event_type=payload.type,
        payload=payload.payload,
        version=payload.version,
        key=payload.key,
    )
    result = cast(DeliveryResult, outcome.result)
    return EventPublishOut(
        eventId=outcome.event_id,
        topic=result.topic,
        partition=result.partition,
        offset=result.offset,
    )


@router.post(
    "/rabbit", response_model=RabbitPublishOut, status_code=status.HTTP_202_ACCEPTED
)
def publish_rabbit_event(
    payload: EventPublishIn,
    db: Session = Depends(get_db),
    policy: DeliveryPolicy = Depends(get_delivery_policy),
    publisher: RabbitPublisher = Depends(get_rabbit_publisher),
    metrics: EventMetrics = Depends(get_event_metrics),
) -> RabbitPublishOut:
    """Publish through the shared retry wrapper; a 503 means it was never parked."""
    outcome = EventDeliveryService(
        publisher=publisher,
        policy=policy,
        db=db,
        transport="RABBIT",
        metrics=metrics,
    ).deliver(
        event_type=payload.type,
        payload=payload.payload,
        version=payload.version,
        key=payload.key,
    )
    result = cast(RabbitDeliveryResult, outcome.result)
    return RabbitPublishOut(
        eventId=outcome.event_id,
        exchange=result.exchange,
        routingKey=result.routing_key,
    )


@router.get("/deliveries/{eventId}", response_model=DeliveryStatusOut)
def get_delivery(eventId: str, db: Session = Depends(get_db)) -> DeliveryStatusOut:
    """One event's delivery state; the path keeps the ticket's `{eventId}` spelling."""
    row = db.scalar(
        select(EventDelivery).where(EventDelivery.event_id == eventId)
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found"
        )
    return DeliveryStatusOut(
        status=row.status,
        attempts=row.attempts,
        lastError=row.last_error,
        nextRetryAt=row.next_retry_at,
    )


@router.get("/kafka/status", response_model=KafkaConsumerStatusOut)
def get_kafka_consumer_status(request: Request) -> KafkaConsumerStatusOut:
    """This process's consumer state; a consumer that was never built reads running=false."""
    current = get_consumer_status(request)
    return KafkaConsumerStatusOut(
        running=current.running,
        topic=current.topic,
        group=current.group,
        processedCount=current.processed_count,
        lastEventAt=current.last_event_at,
        lastError=current.last_error,
    )


@router.post("", response_model=EventBusPublishOut, status_code=status.HTTP_202_ACCEPTED)
def publish_bus_event(
    payload: EventBusPublishIn,
    bus: EventBus = Depends(get_event_bus),
) -> EventBusPublishOut:
    """Publish straight through the selected transport; a failed handler is reported, not raised."""
    outcome = bus.publish(
        EventEnvelope(
            type=payload.type,
            version=payload.version,
            payload=payload.payload,
            metadata=payload.metadata,
        )
    )
    return EventBusPublishOut(
        id=outcome.event_id,
        handlerErrors=[
            EventBusHandlerErrorOut(
                eventType=error.event_type,
                handler=error.handler,
                error=error.error,
            )
            for error in outcome.handler_errors
        ],
    )


@router.get("/subscriptions", response_model=EventSubscriptionsOut)
def list_event_subscriptions(
    bus: EventBus = Depends(get_event_bus),
) -> EventSubscriptionsOut:
    """Every in-process subscription the bus holds, and the transport it publishes on."""
    return EventSubscriptionsOut(
        transport=bus.transport,
        subscriptions=[
            EventBusSubscriptionOut(
                eventType=subscription.event_type, handler=subscription.name
            )
            for subscription in bus.subscriptions()
        ],
    )


@router.get(
    "/metrics",
    response_model=EventMetricsOut,
    responses={200: {"content": {"text/plain": {}}}},
)
def read_event_metrics(
    metrics: EventMetrics = Depends(get_event_metrics),
    settings: EventMetricsSettings = Depends(get_event_metrics_settings),
) -> EventMetricsOut | Response:
    """The process's event counters; Prometheus text when EVENT_METRICS_FORMAT asks."""
    buckets = metrics.snapshot()
    last_event_at = metrics.last_event_at
    if settings.format == "prometheus":
        return PlainTextResponse(
            prometheus_text(buckets, last_event_at),
            media_type="text/plain; version=0.0.4",
        )
    return EventMetricsOut(
        counters=[
            EventMetricsBucketOut(
                type=bucket.event_type,
                transport=bucket.transport,
                published=bucket.published,
                consumed=bucket.consumed,
                failed=bucket.failed,
                retried=bucket.retried,
                deadLettered=bucket.dead_lettered,
            )
            for bucket in buckets
        ],
        lastEventAt=last_event_at,
    )


def _kafka_up(publisher: object | None) -> str:
    """ "up" only when the Kafka publisher exists and its probe answers successfully."""
    if publisher is None:
        return "down"
    check_health = getattr(publisher, "check_health", None)
    if not callable(check_health):
        return "down"
    try:
        return "up" if check_health() is True else "down"
    except Exception:  # a probe must never turn the health route into a 5xx
        return "down"


def _rabbit_up(publisher: object | None) -> str:
    """ "up" only when the RabbitMQ publisher exists and its probe answers successfully."""
    if publisher is None:
        return "down"
    check_health = getattr(publisher, "check_health", None)
    if not callable(check_health):
        return "down"
    try:
        return "up" if check_health() is True else "down"
    except Exception:  # a probe must never turn the health route into a 5xx
        return "down"


@router.get("/health", response_model=EventHealthOut)
def get_event_health(request: Request) -> EventHealthOut:
    """FastAPI, Kafka and RabbitMQ each up or down; always a 200, per the clarification."""
    return EventHealthOut(
        fastapi="up",
        kafka=_kafka_up(getattr(request.app.state, "event_publisher", None)),
        rabbitmq=_rabbit_up(getattr(request.app.state, "rabbit_publisher", None)),
    )


@router.get("/log", response_model=EventLogOut)
def list_recent_events(
    limit: int | None = Query(default=None, ge=1),
    type: str | None = Query(default=None),
    db: Session = Depends(get_db),
    settings: EventMetricsSettings = Depends(get_event_metrics_settings),
) -> EventLogOut:
    """Recent delivery summaries, newest first; they carry no payload content at all."""
    effective_limit = limit if limit is not None else settings.log_limit
    statement = select(EventDelivery)
    if type is not None:
        statement = statement.where(EventDelivery.event_type == type)
    rows = db.scalars(
        statement.order_by(
            EventDelivery.updated_at.desc(), EventDelivery.id.desc()
        ).limit(effective_limit)
    ).all()
    return EventLogOut(
        entries=[
            EventLogEntryOut(
                eventId=row.event_id,
                type=row.event_type,
                transport=row.transport,
                status=row.status,
                timestamp=row.updated_at,
            )
            for row in rows
        ],
        limit=effective_limit,
    )
