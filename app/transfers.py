"""Ticket transfers: hand a confirmed booking to another moviegoer before its show.

A transfer is its own row and never touches the booking while the offer is pending:
the booking keeps its owner until a successful settlement with the payments service,
and only then does the seat's owner (and with it the review ownership) move. Expiry
is evaluated lazily on every read, accept and cancel, because the app has no
scheduler for transfers -- the hold sweeper is specific to seats.
"""

import logging
import secrets
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .bookings import (
    PAYMENTS_UNAVAILABLE_DETAIL,
    PAYMENT_DECLINED_DETAIL,
    get_booking,
)
from .models import Booking, BookingTransfer, Show, User
from .payments_client import (
    PaymentDeclinedError,
    PaymentsClient,
    PaymentsUnavailableError,
)
from .reviews_client import ReviewsClient, ReviewsUnavailableError
from .schemas import BookingStatus, BookingTransferOut, TransferStatus
from .timeutil import ist_local

logger = logging.getLogger(__name__)

TRANSFER_CODE_PREFIX = "TRF"
TRANSFER_CODE_LENGTH = 6
TRANSFER_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
TRANSFER_CODE_ATTEMPTS = 10

TRANSFER_CODE_EXHAUSTED_DETAIL = "Could not allocate a transfer code"
TRANSFER_AFTER_SHOW_DETAIL = (
    "A booking cannot be transferred once its show has started"
)
TRANSFER_ALREADY_PENDING_DETAIL = "This booking already has a pending transfer"
NOT_TRANSFER_RECIPIENT_DETAIL = "This transfer is addressed to another user"
NOT_TRANSFER_OWNER_DETAIL = "Only the sender can cancel this transfer"


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _moment(now: datetime | None) -> datetime:
    return _as_utc(now) if now is not None else datetime.now(timezone.utc)


def has_started(starts_at: datetime, now: datetime | None = None) -> bool:
    """Whether the show's start time is behind us, in UTC terms."""
    return _as_utc(starts_at) <= _moment(now)


def new_transfer_code() -> str:
    """Six characters starting with the ticket's TRF prefix, one draw per slot."""
    suffix_length = TRANSFER_CODE_LENGTH - len(TRANSFER_CODE_PREFIX)
    return TRANSFER_CODE_PREFIX + "".join(
        secrets.choice(TRANSFER_CODE_ALPHABET) for _ in range(suffix_length)
    )


def _unique_transfer_code(db: Session) -> str:
    """A code no other transfer holds; a collision is redrawn rather than reused."""
    for _ in range(TRANSFER_CODE_ATTEMPTS):
        code = new_transfer_code()
        taken = db.scalar(
            select(BookingTransfer.id).where(BookingTransfer.code == code)
        )
        if taken is None:
            return code
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=TRANSFER_CODE_EXHAUSTED_DETAIL,
    )


def transfer_out(transfer: BookingTransfer) -> BookingTransferOut:
    """The wire shape of a transfer, shared by every route that answers one."""
    return BookingTransferOut(
        transferId=transfer.id,
        transferCode=transfer.code,
        bookingId=transfer.booking_id,
        fromUserId=transfer.from_user_id,
        toUserId=transfer.to_user_id,
        status=TransferStatus(transfer.status),
        fee=float(transfer.fee),
        createdAt=ist_local(transfer.created_at),
        expiresAt=ist_local(transfer.expires_at),
    )


def _expire_if_due(
    db: Session, transfer: BookingTransfer, moment: datetime
) -> None:
    """Flip a PENDING transfer whose time is up to EXPIRED, and write that down."""
    if (
        transfer.status == TransferStatus.PENDING.value
        and _as_utc(transfer.expires_at) <= moment
    ):
        transfer.status = TransferStatus.EXPIRED.value
        db.commit()


def transfer_by_code(
    *, db: Session, code: str, now: datetime | None = None
) -> BookingTransfer:
    """The transfer a share link names; a PENDING one past its time reads EXPIRED."""
    moment = _moment(now)
    normalized = code.strip().upper()
    transfer = db.scalar(
        select(BookingTransfer).where(BookingTransfer.code == normalized)
    )
    if transfer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transfer not found: {normalized}",
        )
    _expire_if_due(db, transfer, moment)
    return transfer


def current_transfer(
    *, db: Session, booking_id: int, now: datetime | None = None
) -> BookingTransfer | None:
    """The newest transfer for a booking, expiring a past-due PENDING one first."""
    moment = _moment(now)
    transfer = db.scalar(
        select(BookingTransfer)
        .where(BookingTransfer.booking_id == booking_id)
        .order_by(BookingTransfer.id.desc())
        .limit(1)
    )
    if transfer is None:
        return None
    _expire_if_due(db, transfer, moment)
    return transfer


def create_transfer(
    *,
    db: Session,
    booking_id: int,
    to_user_id: int,
    user: User,
    now: datetime | None = None,
) -> BookingTransfer:
    """Offer a confirmed booking to another moviegoer; it expires when the show starts."""
    moment = _moment(now)
    booking = get_booking(db=db, booking_id=booking_id, user=user)
    if booking.status != BookingStatus.CONFIRMED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Booking is already {booking.status}",
        )

    show = db.get(Show, booking.show_id)
    if show is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Show not found: {booking.show_id}",
        )
    if has_started(show.starts_at, moment):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=TRANSFER_AFTER_SHOW_DETAIL
        )

    if db.get(User, to_user_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User not found: {to_user_id}",
        )

    pending = db.scalar(
        select(BookingTransfer.id).where(
            BookingTransfer.booking_id == booking.id,
            BookingTransfer.status == TransferStatus.PENDING.value,
        )
    )
    if pending is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=TRANSFER_ALREADY_PENDING_DETAIL,
        )

    transfer = BookingTransfer(
        booking_id=booking.id,
        from_user_id=booking.user_id,
        to_user_id=to_user_id,
        code=_unique_transfer_code(db),
        status=TransferStatus.PENDING.value,
        fee=Decimal("0.00"),
        payments_transfer_id=None,
        created_at=moment,
        expires_at=_as_utc(show.starts_at),
    )
    db.add(transfer)
    db.commit()
    return transfer


def _settlement_id(settlement: dict[str, object]) -> int | None:
    transfer_id = settlement.get("transferId")
    return transfer_id if isinstance(transfer_id, int) else None


def _mark_old_owner_review_transferred(
    *,
    db: Session,
    booking: Booking,
    transfer: BookingTransfer,
    reviews: ReviewsClient,
) -> None:
    """Mark the previous owner's review of this booking TRANSFERRED, if there is one.

    The booking already belongs to the recipient by the time this runs, so a ratings
    service that cannot answer only leaves the old review ACTIVE; it never undoes the
    move that already settled.
    """
    show = db.get(Show, booking.show_id)
    if show is None:
        return
    try:
        rows = reviews.list_booking_reviews(
            movie_id=show.movie_id, booking_id=booking.id
        )
    except ReviewsUnavailableError as exc:
        logger.warning(
            "reviews service could not list the reviews of booking %s: %s",
            booking.id,
            exc,
        )
        return

    for row in rows:
        if row.get("userId") != transfer.from_user_id:
            continue
        review_id = row.get("id")
        if not isinstance(review_id, int):
            continue
        try:
            reviews.transfer_review(review_id=review_id)
        except ReviewsUnavailableError as exc:
            logger.warning(
                "reviews service could not mark review %s transferred: %s",
                review_id,
                exc,
            )


def accept_transfer(
    *,
    db: Session,
    code: str,
    user: User,
    payments: PaymentsClient,
    reviews: ReviewsClient,
    now: datetime | None = None,
) -> BookingTransfer:
    """Accept a shared ticket: settle with payments, then move the booking's owner."""
    moment = _moment(now)
    transfer = transfer_by_code(db=db, code=code, now=moment)
    if user.id != transfer.to_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=NOT_TRANSFER_RECIPIENT_DETAIL
        )
    if transfer.status != TransferStatus.PENDING.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Transfer is {transfer.status}",
        )

    booking = db.get(Booking, transfer.booking_id)
    if booking is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking not found: {transfer.booking_id}",
        )
    if booking.status != BookingStatus.CONFIRMED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Booking is already {booking.status}",
        )

    try:
        settlement = payments.settle_transfer(
            booking_id=booking.id,
            from_user_id=transfer.from_user_id,
            to_user_id=transfer.to_user_id,
            amount=Decimal(booking.total),
            fee=Decimal(transfer.fee),
        )
    except PaymentDeclinedError:
        # Nothing is written before the call, so the transfer stays PENDING.
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=PAYMENT_DECLINED_DETAIL,
        ) from None
    except PaymentsUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=PAYMENTS_UNAVAILABLE_DETAIL,
        ) from None

    transfer.payments_transfer_id = _settlement_id(settlement)
    transfer.status = TransferStatus.ACCEPTED.value
    transfer.accepted_at = moment
    booking.user_id = transfer.to_user_id
    db.commit()

    _mark_old_owner_review_transferred(
        db=db, booking=booking, transfer=transfer, reviews=reviews
    )
    return transfer


def cancel_transfer(
    *,
    db: Session,
    transfer_id: int,
    user: User,
    payments: PaymentsClient,
    now: datetime | None = None,
) -> BookingTransfer:
    """Withdraw a pending offer as its sender, releasing a settlement that exists."""
    moment = _moment(now)
    transfer = db.get(BookingTransfer, transfer_id)
    if transfer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transfer not found: {transfer_id}",
        )
    if transfer.from_user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=NOT_TRANSFER_OWNER_DETAIL
        )
    _expire_if_due(db, transfer, moment)
    if transfer.status != TransferStatus.PENDING.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Transfer is {transfer.status}",
        )

    if transfer.payments_transfer_id is not None:
        try:
            payments.release_transfer(transfer_id=transfer.payments_transfer_id)
        except PaymentsUnavailableError:
            # Nothing below runs, so the transfer keeps reading as PENDING.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=PAYMENTS_UNAVAILABLE_DETAIL,
            ) from None

    transfer.status = TransferStatus.CANCELLED.value
    transfer.cancelled_at = moment
    db.commit()
    return transfer
