"""Internal booking lookups other services make, behind the shared service token."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Booking, Show
from ..schemas import BookingStatus, InternalBookingOut
from ..security import require_service_token
from ..timeutil import ist_local

router = APIRouter(
    prefix="/internal",
    tags=["internal-bookings"],
    dependencies=[Depends(require_service_token)],
)


@router.get("/bookings/{booking_id}", response_model=InternalBookingOut)
def read_internal_booking(
    booking_id: int,
    db: Session = Depends(get_db),
) -> InternalBookingOut:
    """The booking's current owner, so the ratings service can check a review's author."""
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking not found: {booking_id}",
        )

    show = db.get(Show, booking.show_id)
    if show is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Show not found: {booking.show_id}",
        )

    return InternalBookingOut(
        bookingId=booking.id,
        userId=booking.user_id,
        status=BookingStatus(booking.status),
        showId=booking.show_id,
        showStartsAt=ist_local(show.starts_at),
        movieId=show.movie_id,
    )
