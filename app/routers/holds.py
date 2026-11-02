from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Show, User
from ..schemas import HeldSeatOut, SeatHoldIn, SeatHoldOut
from ..seating import acquire_hold, release_hold
from ..security import get_current_user
from ..timeutil import ist_local

router = APIRouter(prefix="/api", tags=["holds"])


@router.post(
    "/shows/{show_id}/holds",
    response_model=SeatHoldOut,
    status_code=status.HTTP_201_CREATED,
)
def create_hold(
    show_id: int,
    payload: SeatHoldIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SeatHoldOut:
    show = db.get(Show, show_id)
    if show is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Show not found: {show_id}",
        )

    held = acquire_hold(db=db, show=show, seat_ids=payload.seatIds, user=user)
    return SeatHoldOut(
        holdId=held.hold_id,
        heldUntil=ist_local(held.held_until),
        seats=[
            HeldSeatOut(id=seat_id, label=label) for seat_id, label in held.seats
        ],
        total=float(held.total),
    )


@router.delete("/holds/{hold_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_hold(
    hold_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    release_hold(db=db, hold_id=hold_id, user=user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
