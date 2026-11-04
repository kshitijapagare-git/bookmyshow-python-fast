from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..bookings import create_booking, find_booking, get_booking
from ..db import get_db
from ..models import Booking, Seat, ShowSeat, User
from ..payments_client import PaymentsClient, get_payments_client
from ..schemas import BookingCreate, BookingOut, BookingStatus, HeldSeatOut
from ..security import get_current_user
from ..seating import seat_label

router = APIRouter(prefix="/api", tags=["bookings"])


def _booking_out(booking: Booking, seats: list[HeldSeatOut]) -> BookingOut:
    return BookingOut(
        bookingId=booking.id,
        showId=booking.show_id,
        holdId=booking.hold_id,
        status=BookingStatus(booking.status),
        seats=seats,
        total=float(booking.total),
        paymentId=booking.payment_id,
        createdAt=booking.created_at,
    )


def _seats_out(db: Session, seat_ids: list[int]) -> list[HeldSeatOut]:
    """Label the hold's ShowSeats exactly as the hold response labelled them."""
    rows = db.execute(
        select(ShowSeat.id, Seat.row, Seat.number)
        .join(Seat, ShowSeat.seat_id == Seat.id)
        .where(ShowSeat.id.in_(seat_ids))
    ).all()
    labels = {seat_id: seat_label(row, number) for seat_id, row, number in rows}
    return [
        HeldSeatOut(id=seat_id, label=labels.get(seat_id, "")) for seat_id in seat_ids
    ]


@router.post(
    "/bookings", response_model=BookingOut, status_code=status.HTTP_201_CREATED
)
def post_booking(
    payload: BookingCreate,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    client: PaymentsClient = Depends(get_payments_client),
) -> BookingOut:
    """Pay for a live hold; a replayed Idempotency-Key answers the stored booking."""
    replay = find_booking(db=db, user=user, idempotency_key=idempotency_key)
    booking = create_booking(
        db=db,
        hold_id=payload.holdId,
        idempotency_key=idempotency_key,
        user=user,
        client=client,
    )
    if replay is not None:
        response.status_code = status.HTTP_200_OK
    return _booking_out(booking, _seats_out(db, booking.seat_ids))


@router.get("/bookings/{booking_id}", response_model=BookingOut)
def read_booking(
    booking_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BookingOut:
    booking = get_booking(db=db, booking_id=booking_id, user=user)
    return _booking_out(booking, _seats_out(db, booking.seat_ids))
