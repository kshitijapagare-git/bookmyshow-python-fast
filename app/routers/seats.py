from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Show, User
from ..schemas import SeatLayoutOut
from ..seating import build_seat_layout
from ..security import get_optional_user

router = APIRouter(prefix="/api/shows", tags=["seats"])


@router.get("/{show_id}/seats", response_model=SeatLayoutOut)
def get_seat_layout(
    show_id: int,
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> SeatLayoutOut:
    show = db.get(Show, show_id)
    if show is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Show not found: {show_id}",
        )
    return build_seat_layout(db, show, user)
