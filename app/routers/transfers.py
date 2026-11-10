from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Booking, Show, User
from ..payments_client import PaymentsClient, get_payments_client
from ..reviews_client import ReviewsClient, get_reviews_client
from ..schemas import (
    BookingTransferIn,
    BookingTransferOut,
    TransferDetailOut,
)
from ..security import get_current_user
from ..timeutil import ist_local
from ..transfers import (
    accept_transfer as accept_transfer_service,
    cancel_transfer as cancel_transfer_service,
    create_transfer as create_transfer_service,
    has_started,
    transfer_by_code,
    transfer_out,
)
from .bookings import _seats_out as labelled_seats

router = APIRouter(prefix="/api", tags=["transfers"])

NOT_A_TRANSFER_PARTY_DETAIL = "This transfer belongs to another moviegoer"


@router.post(
    "/bookings/{booking_id}/transfer",
    response_model=BookingTransferOut,
    status_code=status.HTTP_201_CREATED,
)
def post_transfer(
    booking_id: int,
    payload: BookingTransferIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BookingTransferOut:
    """Offer a confirmed booking to another moviegoer; it expires when the show starts."""
    transfer = create_transfer_service(
        db=db, booking_id=booking_id, to_user_id=payload.toUserId, user=user
    )
    return transfer_out(transfer)


@router.post("/transfers/{code}/accept", response_model=BookingTransferOut)
def post_accept(
    code: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    payments: PaymentsClient = Depends(get_payments_client),
    reviews: ReviewsClient = Depends(get_reviews_client),
) -> BookingTransferOut:
    """Accept a shared ticket: the settlement runs first, then the booking changes hands."""
    transfer = accept_transfer_service(
        db=db, code=code, user=user, payments=payments, reviews=reviews
    )
    return transfer_out(transfer)


@router.post("/transfers/{transfer_id}/cancel", response_model=BookingTransferOut)
def post_cancel(
    transfer_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    payments: PaymentsClient = Depends(get_payments_client),
) -> BookingTransferOut:
    """Withdraw a pending offer as its sender, releasing any settlement behind it."""
    transfer = cancel_transfer_service(
        db=db, transfer_id=transfer_id, user=user, payments=payments
    )
    return transfer_out(transfer)


@router.get("/transfers/{code}", response_model=TransferDetailOut)
def get_transfer(
    code: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TransferDetailOut:
    """What the recipient's accept page shows: the movie, the cinema, the show and seats."""
    transfer = transfer_by_code(db=db, code=code)
    if user.id not in (transfer.from_user_id, transfer.to_user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=NOT_A_TRANSFER_PARTY_DETAIL,
        )

    booking = db.get(Booking, transfer.booking_id)
    if booking is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Booking not found: {transfer.booking_id}",
        )
    show = db.get(Show, booking.show_id)
    if show is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Show not found: {booking.show_id}",
        )

    # The client owns the "after the show" clock, exactly as the group eligibility
    # endpoint leaves it: the server only says whether the reward is unlocked now.
    review_eligible = booking.user_id == user.id and has_started(
        show.starts_at, datetime.now(timezone.utc)
    )

    return TransferDetailOut(
        transfer=transfer_out(transfer),
        bookingId=booking.id,
        movieId=show.movie_id,
        movieTitle=show.movie.title,
        cinemaName=show.screen.cinema.name,
        showStartsAt=ist_local(show.starts_at),
        seats=labelled_seats(db, list(booking.seat_ids)),
        reviewEligible=review_eligible,
    )
