from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..bookings import (
    cancel_booking as cancel_booking_service,
    create_booking,
    find_booking,
    get_booking,
    quote_hold,
)
from ..db import get_db
from ..models import Booking, BookingTransfer, Seat, ShowSeat, User
from ..payments_client import PaymentsClient, get_payments_client
from ..schemas import (
    BookingCancelOut,
    BookingCreate,
    BookingOut,
    BookingQuoteIn,
    BookingQuoteOut,
    BookingStatus,
    HeldSeatOut,
)
from ..security import get_current_user
from ..seating import seat_label
from ..transfers import current_transfer, transfer_out

router = APIRouter(prefix="/api", tags=["bookings"])


def _booking_out(
    booking: Booking,
    seats: list[HeldSeatOut],
    transfer: BookingTransfer | None = None,
) -> BookingOut:
    return BookingOut(
        bookingId=booking.id,
        showId=booking.show_id,
        holdId=booking.hold_id,
        status=BookingStatus(booking.status),
        seats=seats,
        total=float(booking.total),
        paymentId=booking.payment_id,
        createdAt=booking.created_at,
        refundedAt=booking.refunded_at,
        promoCode=booking.promo_code,
        originalAmount=(
            float(booking.original_amount)
            if booking.original_amount is not None
            else None
        ),
        discount=float(booking.discount) if booking.discount is not None else None,
        transfer=transfer_out(transfer) if transfer is not None else None,
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
        promo_code=payload.promoCode,
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
    transfer = current_transfer(db=db, booking_id=booking.id)
    return _booking_out(booking, _seats_out(db, booking.seat_ids), transfer)


@router.post(
    "/bookings/quote", response_model=BookingQuoteOut, status_code=status.HTTP_200_OK
)
def post_booking_quote(
    payload: BookingQuoteIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    client: PaymentsClient = Depends(get_payments_client),
) -> BookingQuoteOut:
    """Price a promo code against the caller's live hold; nothing is charged."""
    quote = quote_hold(
        db=db,
        hold_id=payload.holdId,
        promo_code=payload.promoCode,
        user=user,
        client=client,
    )
    return BookingQuoteOut(
        originalAmount=float(quote["originalAmount"]),
        discount=float(quote["discount"]),
        total=float(quote["total"]),
        promoCode=str(quote["promoCode"]),
    )


@router.post(
    "/bookings/{booking_id}/cancel",
    response_model=BookingCancelOut,
    status_code=status.HTTP_200_OK,
)
def cancel_booking(
    booking_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    client: PaymentsClient = Depends(get_payments_client),
) -> BookingCancelOut:
    """Refund a confirmed booking and put its seats back on sale."""
    booking = cancel_booking_service(
        db=db, booking_id=booking_id, user=user, client=client
    )
    return BookingCancelOut(
        id=booking.id,
        status=BookingStatus(booking.status),
        refundedAt=booking.refunded_at,
    )
