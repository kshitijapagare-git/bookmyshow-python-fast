"""The booking transaction: check the hold, charge it, then settle the seats.

The hold's seats are priced server-side from the current seat-category prices, the
booking is committed as PENDING_PAYMENT before the payment call so a timeout leaves
a durable record, and only a successful payment turns the seats SOLD.
"""

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Booking, Seat, SeatCategory, ShowSeat, User
from .payments_client import (
    PaymentDeclinedError,
    PaymentsClient,
    PaymentsUnavailableError,
    PromoCodeInvalidError,
)
from .schemas import BookingStatus, ShowSeatStatus
from .seating import effective_status

BOOKING_CURRENCY = "INR"
PAYMENT_REFUND_FAILED_DETAIL = "Refund could not be processed"
NOT_OWNER_DETAIL = "This hold belongs to another user"
HOLD_EXPIRED_DETAIL = "Hold has expired"
PAYMENT_DECLINED_DETAIL = "Payment declined"
PAYMENTS_UNAVAILABLE_DETAIL = "Payment service unavailable"
PROMO_INVALID_DETAIL = "That promo code is not valid."
CENT = Decimal("0.01")


def find_booking(*, db: Session, user: User, idempotency_key: str) -> Booking | None:
    """The booking this key already produced for this moviegoer, if any."""
    return db.scalar(
        select(Booking).where(
            Booking.user_id == user.id,
            Booking.idempotency_key == idempotency_key,
        )
    )


def _hold_total(db: Session, seat_ids: list[int]) -> Decimal:
    """Sum the current seat-category prices of the hold's ShowSeats, to the paisa."""
    prices = db.scalars(
        select(SeatCategory.price)
        .join(Seat, Seat.category_id == SeatCategory.id)
        .join(ShowSeat, ShowSeat.seat_id == Seat.id)
        .where(ShowSeat.id.in_(seat_ids))
    )
    total = sum((Decimal(price) for price in prices), Decimal("0"))
    return total.quantize(CENT)


def _release(db: Session, rows: list[ShowSeat]) -> None:
    """Hand the seats back so another moviegoer can hold them; the caller commits."""
    for row in rows:
        row.status = ShowSeatStatus.AVAILABLE.value
        row.held_by = None
        row.held_until = None
        row.hold_id = None


def _payment_id(payment: dict[str, object]) -> int | None:
    payment_id = payment.get("paymentId")
    return payment_id if isinstance(payment_id, int) else None


def _money(value: object, fallback: Decimal) -> Decimal:
    """A money figure from the quote body, to the paisa, or the caller's fallback."""
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value)).quantize(CENT)
    return fallback


def _applied_code(value: object, typed_code: str) -> str:
    """The code to store and echo: the quote's answer, else the self-uppercased input."""
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    return typed_code.strip().upper()


def create_booking(
    *,
    db: Session,
    hold_id: str,
    idempotency_key: str,
    user: User,
    client: PaymentsClient,
    promo_code: str | None = None,
    now: datetime | None = None,
) -> Booking:
    """Turn a live hold into a paid booking, or fail leaving the hold's seats coherent."""
    existing = find_booking(db=db, user=user, idempotency_key=idempotency_key)
    if existing is not None:
        return existing

    moment = now if now is not None else datetime.now(timezone.utc)
    rows = list(
        db.scalars(
            select(ShowSeat).where(ShowSeat.hold_id == hold_id).with_for_update()
        )
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Hold not found: {hold_id}",
        )
    if any(row.held_by != user.id for row in rows):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=NOT_OWNER_DETAIL
        )

    live = [row for row in rows if effective_status(row, moment) is ShowSeatStatus.HELD]
    if not live:
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail=HOLD_EXPIRED_DETAIL
        )

    seat_ids = [row.id for row in live]
    original_total = _hold_total(db, seat_ids)
    total = original_total
    discount = Decimal("0")
    applied_code: str | None = None
    if promo_code is not None:
        try:
            quote = client.quote_payment(
                booking_id=0, amount=original_total, promo_code=promo_code
            )
        except PromoCodeInvalidError:
            # Nothing is written yet, so a bad code leaves no booking behind.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=PROMO_INVALID_DETAIL
            ) from None
        except PaymentsUnavailableError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=PAYMENTS_UNAVAILABLE_DETAIL,
            ) from None
        total = _money(quote.get("total"), original_total)
        discount = _money(quote.get("discount"), Decimal("0"))
        applied_code = _applied_code(quote.get("promoCode"), promo_code)

    booking = Booking(
        user_id=user.id,
        show_id=live[0].show_id,
        hold_id=hold_id,
        seat_ids=seat_ids,
        total=total,
        original_amount=original_total,
        discount=discount,
        promo_code=applied_code,
        status=BookingStatus.PENDING_PAYMENT.value,
        payment_id=None,
        idempotency_key=idempotency_key,
        created_at=moment,
    )
    db.add(booking)
    db.commit()

    try:
        payment = client.create_payment(
            booking_id=booking.id,
            amount=total,
            currency=BOOKING_CURRENCY,
            idempotency_key=str(booking.id),
            promo_code=applied_code,
        )
    except PaymentDeclinedError:
        _release(db, live)
        booking.status = BookingStatus.FAILED.value
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=PAYMENT_DECLINED_DETAIL,
        ) from None
    except PaymentsUnavailableError:
        # The booking stays PENDING_PAYMENT: a retry with the same key settles it.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=PAYMENTS_UNAVAILABLE_DETAIL,
        ) from None

    for row in live:
        row.status = ShowSeatStatus.SOLD.value
        row.held_by = None
        row.held_until = None
        row.hold_id = None
    booking.status = BookingStatus.CONFIRMED.value
    booking.payment_id = _payment_id(payment)
    db.commit()
    return booking


def quote_hold(
    *,
    db: Session,
    hold_id: str,
    promo_code: str,
    user: User,
    client: PaymentsClient,
    now: datetime | None = None,
) -> dict[str, object]:
    """Price a promo code against a live hold; nothing is written and nothing is charged."""
    moment = now if now is not None else datetime.now(timezone.utc)
    rows = list(db.scalars(select(ShowSeat).where(ShowSeat.hold_id == hold_id)))
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Hold not found: {hold_id}",
        )
    if any(row.held_by != user.id for row in rows):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=NOT_OWNER_DETAIL
        )

    live = [row for row in rows if effective_status(row, moment) is ShowSeatStatus.HELD]
    if not live:
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail=HOLD_EXPIRED_DETAIL
        )

    total = _hold_total(db, [row.id for row in live])
    try:
        return client.quote_payment(booking_id=0, amount=total, promo_code=promo_code)
    except PromoCodeInvalidError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=PROMO_INVALID_DETAIL
        ) from None
    except PaymentsUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=PAYMENTS_UNAVAILABLE_DETAIL,
        ) from None


def get_booking(*, db: Session, booking_id: int, user: User) -> Booking:
    """Read one booking, for its owner only."""
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking not found: {booking_id}",
        )
    if booking.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This booking belongs to another user",
        )
    return booking


def cancel_booking(
    *,
    db: Session,
    booking_id: int,
    user: User,
    client: PaymentsClient,
    now: datetime | None = None,
) -> Booking:
    """Refund a confirmed booking's payment and put its seats back on sale."""
    booking = get_booking(db=db, booking_id=booking_id, user=user)
    if booking.status != BookingStatus.CONFIRMED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Booking is already {booking.status}",
        )
    if booking.payment_id is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=PAYMENT_REFUND_FAILED_DETAIL,
        )

    try:
        client.refund_payment(
            payment_id=booking.payment_id,
            idempotency_key=f"cancel-{booking.id}",
        )
    except PaymentsUnavailableError:
        # Nothing is written before the call, so the booking stays CONFIRMED and paid.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=PAYMENT_REFUND_FAILED_DETAIL,
        ) from None

    moment = now if now is not None else datetime.now(timezone.utc)
    booking.status = BookingStatus.CANCELLED.value
    booking.refunded_at = moment
    _release(
        db,
        list(db.scalars(select(ShowSeat).where(ShowSeat.id.in_(booking.seat_ids)))),
    )
    db.commit()
    return booking
