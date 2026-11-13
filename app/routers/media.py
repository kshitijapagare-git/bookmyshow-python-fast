"""Reads of a stored media asset: the bytes, their metadata and their integrity.

The upload route writes the bytes through one `StorageBackend`, and these routes read
them back through the same seam: `StorageBackend.path_for` turns the row's storage key
into a file `FileResponse` streams, so the router never touches a disk path or an SDK of
its own. The bytes and the metadata are public -- a poster or backdrop has to load
directly in the client -- and neither filters on `is_current`, so an asset superseded by
a later upload of the same kind still serves its own bytes.

The integrity and verify routes are authenticated instead: they recompute the stored
object's digest through `integrity_report` and write the resulting flag onto the row,
which is what the public review card reads before it shows a review photo.
"""

import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..media import (
    IntegrityResult,
    MediaSettings,
    StorageBackend,
    get_media_settings,
    get_storage_backend,
    integrity_report,
)
from ..models import MediaAsset, User
from ..schemas import (
    MediaAssetOut,
    MediaIntegrityOut,
    MediaKind,
    MediaVerifyIn,
    MediaVerifyOut,
)
from ..security import get_current_user

router = APIRouter(prefix="/api/media", tags=["media"])


def _get_asset(asset_id: int, db: Session) -> MediaAsset:
    """The asset row for this id; only a missing row is a 404, never a superseded one."""
    asset = db.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Media asset not found: {asset_id}",
        )
    return asset


@router.get("/{asset_id}", response_class=FileResponse)
def download_media_asset(
    asset_id: int,
    db: Session = Depends(get_db),
    storage: StorageBackend = Depends(get_storage_backend),
) -> FileResponse:
    """Stream the stored bytes; Starlette fills Content-Length from the file's stat."""
    asset = _get_asset(asset_id, db)
    return FileResponse(
        path=storage.path_for(asset.storage_key),
        media_type=asset.content_type,
        headers={"Content-Disposition": "inline"},
    )


@router.get("/{asset_id}/metadata", response_model=MediaAssetOut)
def get_media_asset_metadata(
    asset_id: int,
    db: Session = Depends(get_db),
) -> MediaAssetOut:
    """The asset's six public fields; its storage key and the bytes themselves stay out."""
    asset = _get_asset(asset_id, db)
    return MediaAssetOut(
        assetId=asset.id,
        kind=MediaKind(asset.kind),
        url=asset.url,
        size=asset.size_bytes,
        contentType=asset.content_type,
        sha256=asset.sha256,
    )


def _report(
    asset: MediaAsset, storage: StorageBackend, settings: MediaSettings
) -> IntegrityResult:
    """Recompute this asset's stored digest through the one read-back seam."""
    return integrity_report(
        storage=storage,
        storage_key=asset.storage_key,
        expected_sha256=asset.sha256,
        expected_size=asset.size_bytes,
        algorithm=settings.hash_algorithm,
    )


@router.get("/{asset_id}/integrity", response_model=MediaIntegrityOut)
def get_media_integrity(
    asset_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: StorageBackend = Depends(get_storage_backend),
    settings: MediaSettings = Depends(get_media_settings),
) -> MediaIntegrityOut:
    """The digest and size recorded at upload, plus whether storage still matches them."""
    asset = _get_asset(asset_id, db)
    report = _report(asset, storage, settings)
    # The public review card reads this persisted flag, so the recompute is written back.
    asset.verified = report.verified
    db.commit()
    return MediaIntegrityOut(
        sha256=asset.sha256,
        size=asset.size_bytes,
        verified=report.verified,
    )


@router.post("/{asset_id}/verify", response_model=MediaVerifyOut)
def verify_media_asset(
    asset_id: int,
    payload: MediaVerifyIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: StorageBackend = Depends(get_storage_backend),
    settings: MediaSettings = Depends(get_media_settings),
) -> MediaVerifyOut:
    """Compare a moderator's digest with the bytes storage holds right now."""
    asset = _get_asset(asset_id, db)
    report = _report(asset, storage, settings)
    match = report.recomputed_sha256 is not None and secrets.compare_digest(
        report.recomputed_sha256, payload.sha256
    )
    # A digest that matches tampered bytes must not leave the asset looking trusted.
    asset.verified = report.verified and match
    db.commit()
    return MediaVerifyOut(match=match)
