from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Booking
from ..schemas import BookingOut, TransferStateOut
from ..security import get_current_user_id

router = APIRouter(prefix="/api/bookings", tags=["bookings"])


def _booking_out(booking: Booking, transfer: TransferStateOut | None = None) -> BookingOut:
    return BookingOut(
        bookingId=booking.id,
        showId=booking.show_id,
        userId=booking.user_id,
        status=booking.status,
        total=float(booking.total),
        createdAt=booking.created_at,
        transfer=transfer,
    )


@router.get("/{booking_id}", response_model=BookingOut)
def get_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    if booking.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Booking belongs to another user",
        )
    return _booking_out(booking)
