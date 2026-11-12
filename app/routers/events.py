"""Publishing domain events to Kafka, behind the shared service token."""

from fastapi import APIRouter, Depends, status

from ..kafka_producer import DeliveryResult, EventPublisher, get_event_publisher
from ..schemas import EventPublishIn, EventPublishOut
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
    publisher: EventPublisher = Depends(get_event_publisher),
) -> EventPublishOut:
    """Publish the envelope in one bounded attempt; a 503 means the broker let us down."""
    result: DeliveryResult = publisher.publish(
        event_type=payload.type,
        payload=payload.payload,
        version=payload.version,
        key=payload.key,
    )
    return EventPublishOut(
        eventId=result.event_id,
        topic=result.topic,
        partition=result.partition,
        offset=result.offset,
    )
