"""Moviegoer lookups: resolving a phone number to the user a transfer names."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import MediaAsset, User
from ..schemas import MediaAssetOut, MediaKind, UserOut
from ..security import get_current_user

router = APIRouter(prefix="/api", tags=["users"])

AVATAR_NOT_FOUND_DETAIL = "No avatar has been uploaded by this user"


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


@router.get("/users/me/avatar", response_model=MediaAssetOut)
def get_my_avatar(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MediaAssetOut:
    """The caller's current avatar, in the same shape completion answered with.

    A reload has no memory of the completion response, so the persisted row is read
    back here rather than folded into `UserOut`, which the `/api/me` contract fixes.
    """
    asset = db.scalars(
        select(MediaAsset)
        .where(
            MediaAsset.user_id == user.id,
            MediaAsset.kind == MediaKind.AVATAR.value,
            MediaAsset.is_current.is_(True),
        )
        .order_by(MediaAsset.id.desc())
        .limit(1)
    ).first()
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=AVATAR_NOT_FOUND_DETAIL,
        )
    return MediaAssetOut(
        assetId=asset.id,
        kind=MediaKind.AVATAR,
        url=asset.url,
        size=asset.size_bytes,
        contentType=asset.content_type,
        sha256=asset.sha256,
    )
