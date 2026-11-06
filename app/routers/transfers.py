import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Booking, BookingTransfer
from ..payments_client import PaymentsUnavailable, release_transfer, settle_transfer
from ..schemas import TransferCreate, TransferDetailOut, TransferOut, TransferStatus
from ..security import get_current_user_id

router = APIRouter(prefix="/api", tags=["transfers"])

_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_ACTIVE_TRANSFER_STATUSES = (TransferStatus.PENDING.value, TransferStatus.ACCEPTED.value)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes even for timezone-aware columns."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _new_transfer_code(db: Session) -> str:
    while True:
        code = "TRF" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(3))
        exists = db.scalar(select(BookingTransfer.id).where(BookingTransfer.code == code).limit(1))
        if exists is None:
            return code


def _expire_if_due(transfer: BookingTransfer, db: Session) -> BookingTransfer:
    if transfer.status != TransferStatus.PENDING.value:
        return transfer

    booking = db.get(Booking, transfer.booking_id)
    deadline = _as_utc(transfer.expires_at)
    if booking is not None:
        deadline = min(deadline, _as_utc(booking.show.starts_at))

    if deadline <= _now():
        transfer.status = TransferStatus.EXPIRED.value
        db.commit()
        db.refresh(transfer)
    return transfer


@router.post(
    "/bookings/{booking_id}/transfer",
    response_model=TransferOut,
    status_code=status.HTTP_201_CREATED,
)
def create_transfer(
    booking_id: int,
    payload: TransferCreate,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
) -> TransferOut:
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    if booking.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Booking belongs to another user",
        )
    if booking.status != "CONFIRMED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Booking is not CONFIRMED",
        )

    starts_at = _as_utc(booking.show.starts_at)
    if starts_at <= _now():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The show has already started",
        )

    in_progress = db.scalar(
        select(BookingTransfer.id)
        .where(
            BookingTransfer.booking_id == booking.id,
            BookingTransfer.status.in_(_ACTIVE_TRANSFER_STATUSES),
        )
        .limit(1)
    )
    if in_progress is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A transfer is already in progress for this booking",
        )

    transfer = BookingTransfer(
        booking_id=booking.id,
        from_user_id=booking.user_id,
        to_user_id=payload.toUserId,
        to_phone=payload.toPhone,
        code=_new_transfer_code(db),
        status=TransferStatus.PENDING.value,
        expires_at=starts_at,
    )
    db.add(transfer)
    db.commit()
    db.refresh(transfer)
    return TransferOut(
        transferCode=transfer.code,
        status=TransferStatus(transfer.status),
        expiresAt=transfer.expires_at,
    )


@router.get("/transfers/{code}", response_model=TransferDetailOut)
def get_transfer(code: str, db: Session = Depends(get_db)) -> TransferDetailOut:
    transfer = db.scalar(select(BookingTransfer).where(BookingTransfer.code == code))
    if transfer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transfer not found")

    transfer = _expire_if_due(transfer, db)

    booking = db.get(Booking, transfer.booking_id)
    show = booking.show
    return TransferDetailOut(
        transferCode=transfer.code,
        status=TransferStatus(transfer.status),
        expiresAt=transfer.expires_at,
        movieTitle=show.movie.title,
        cinemaName=show.cinema.name,
        startsAt=show.starts_at,
    )


@router.post("/transfers/{code}/accept", response_model=TransferOut)
def accept_transfer(
    code: str,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
) -> TransferOut:
    transfer = db.scalar(select(BookingTransfer).where(BookingTransfer.code == code))
    if transfer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transfer not found")

    transfer = _expire_if_due(transfer, db)
    if transfer.status != TransferStatus.PENDING.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Transfer is not pending",
        )
    if transfer.to_user_id is not None and transfer.to_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Transfer belongs to another recipient",
        )

    booking = db.get(Booking, transfer.booking_id)
    if booking is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")

    if transfer.to_user_id is None:
        transfer.to_user_id = user_id

    try:
        settle_transfer(
            booking_id=booking.id,
            from_user_id=transfer.from_user_id,
            to_user_id=transfer.to_user_id,
            amount=float(booking.total),
            fee=float(transfer.fee),
        )
    except PaymentsUnavailable:
        # Settlement must succeed before ownership moves; a failure leaves it pending.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Transfer settlement failed",
        ) from None

    transfer.status = TransferStatus.ACCEPTED.value
    booking.user_id = transfer.to_user_id
    db.commit()
    db.refresh(transfer)
    return TransferOut(
        transferCode=transfer.code,
        status=TransferStatus(transfer.status),
        expiresAt=transfer.expires_at,
    )


@router.post("/transfers/{transfer_id}/cancel", response_model=TransferOut)
def cancel_transfer(
    transfer_id: int,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
) -> TransferOut:
    transfer = db.get(BookingTransfer, transfer_id)
    if transfer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transfer not found")

    if transfer.from_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Transfer belongs to another user",
        )

    transfer = _expire_if_due(transfer, db)
    if transfer.status != TransferStatus.PENDING.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Transfer is not pending",
        )

    try:
        release_transfer(booking_id=transfer.booking_id)
    except PaymentsUnavailable:
        # The settlement is still held, so the transfer has to stay pending.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Transfer release failed",
        ) from None

    transfer.status = TransferStatus.CANCELLED.value
    db.commit()
    db.refresh(transfer)
    return TransferOut(
        transferCode=transfer.code,
        status=TransferStatus(transfer.status),
        expiresAt=transfer.expires_at,
    )
