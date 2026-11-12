"""Publishing domain events to Kafka, behind the shared service token."""

from typing import cast

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..delivery import DeliveryPolicy, EventDeliveryService, get_delivery_policy
from ..kafka_producer import DeliveryResult, EventPublisher, get_event_publisher
from ..models import EventDelivery
from ..rabbit_publisher import (
    RabbitDeliveryResult,
    RabbitPublisher,
    get_rabbit_publisher,
)
from ..schemas import (
    DeliveryStatusOut,
    EventPublishIn,
    EventPublishOut,
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
) -> EventPublishOut:
    """Publish through the shared retry wrapper; a 503 means it was never parked."""
    outcome = EventDeliveryService(
        publisher=publisher, policy=policy, db=db, transport="KAFKA"
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
) -> RabbitPublishOut:
    """Publish through the shared retry wrapper; a 503 means it was never parked."""
    outcome = EventDeliveryService(
        publisher=publisher, policy=policy, db=db, transport="RABBIT"
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
