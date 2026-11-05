from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..bookings import PAYMENTS_UNAVAILABLE_DETAIL
from ..db import get_db
from ..models import Booking, Show, User
from ..payments_client import (
    PaymentsClient,
    PaymentsUnavailableError,
    get_payments_client,
)
from ..reviews_client import ReviewsClient, ReviewsUnavailableError, get_reviews_client
from ..schemas import BookingStatus, ReviewOut, ReviewSubmitIn, ReviewsPageOut
from ..security import get_current_user

router = APIRouter(prefix="/api/movies", tags=["reviews"])

REVIEWS_UNAVAILABLE_DETAIL = "Reviews are unavailable right now. Please try again."
REVIEW_FORBIDDEN_DETAIL = "Only moviegoers with a confirmed booking can review."
PAID_PAYMENT_STATUS = "SUCCEEDED"
PAGE_SIZE_DEFAULT = 10
PAGE_SIZE_MAX = 50


def _clamp_paging(page: int, page_size: int) -> tuple[int, int]:
    """Floor the page at 1 and keep the page size inside 1..50, as the ratings API does."""
    return max(page, 1), min(max(page_size, 1), PAGE_SIZE_MAX)


@router.get("/{movie_id}/reviews", response_model=ReviewsPageOut)
def list_movie_reviews(
    movie_id: int,
    page: int = 1,
    pageSize: int = PAGE_SIZE_DEFAULT,
    client: ReviewsClient = Depends(get_reviews_client),
) -> ReviewsPageOut:
    """Read a movie's reviews and summary; the ratings service owns the data."""
    safe_page, safe_page_size = _clamp_paging(page, pageSize)
    try:
        body = client.list_reviews(
            movie_id=movie_id, page=safe_page, page_size=safe_page_size
        )
    except ReviewsUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=REVIEWS_UNAVAILABLE_DETAIL,
        ) from None
    return ReviewsPageOut.model_validate(body)


def _review_forbidden() -> HTTPException:
    """Every refusal of a review looks the same, so the ticket's sentence is the detail."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail=REVIEW_FORBIDDEN_DETAIL
    )


@router.post(
    "/{movie_id}/reviews",
    response_model=ReviewOut,
    status_code=status.HTTP_201_CREATED,
)
def create_movie_review(
    movie_id: int,
    payload: ReviewSubmitIn,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    payments: PaymentsClient = Depends(get_payments_client),
    reviews: ReviewsClient = Depends(get_reviews_client),
) -> ReviewOut:
    """Review a movie, but only against the caller's own confirmed and paid booking."""
    booking = db.get(Booking, payload.bookingId)
    if (
        booking is None
        or booking.user_id != user.id
        or booking.status != BookingStatus.CONFIRMED.value
    ):
        raise _review_forbidden()

    booked_movie_id = db.scalar(select(Show.movie_id).where(Show.id == booking.show_id))
    if booked_movie_id != movie_id:
        raise _review_forbidden()

    try:
        paid_rows = payments.list_payments(booking_id=payload.bookingId)
    except PaymentsUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=PAYMENTS_UNAVAILABLE_DETAIL,
        ) from None
    if not any(row.get("status") == PAID_PAYMENT_STATUS for row in paid_rows):
        raise _review_forbidden()

    try:
        body, created = reviews.create_review(
            movie_id=movie_id,
            user_id=user.id,
            booking_id=payload.bookingId,
            rating=payload.rating,
            comment=payload.comment,
            verified=True,
        )
    except ReviewsUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=REVIEWS_UNAVAILABLE_DETAIL,
        ) from None

    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return ReviewOut.model_validate(body)
