"""Moviegoer lookups: resolving a phone number to the user a transfer names."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..schemas import UserOut
from ..security import get_current_user

router = APIRouter(prefix="/api", tags=["users"])


@router.get("/users/lookup", response_model=UserOut)
def lookup_user(
    phone: Annotated[str, Query(min_length=1)],
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserOut:
    """Find the moviegoer a phone number belongs to, for the transfer dialog."""
    found = db.scalar(select(User).where(User.phone == phone.strip()))
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User not found: {phone}",
        )
    return UserOut(id=found.id, phone=found.phone, name=found.name)
