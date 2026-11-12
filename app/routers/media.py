"""Serving a stored media asset's bytes by its asset id.

The bytes always come back through `StorageBackend.resolve`, never by building a path
here, so a row whose file is gone -- or whose `storage_key` was tampered with -- is the
same 404 as an id with no row at all. Downloads carry no token: uploads stay gated by
`require_media_service_token`, while moviegoers fetch the images themselves.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..media import StorageBackend, get_storage_backend
from ..models import MediaAsset
from ..schemas import MediaAssetOut, MediaKind

router = APIRouter(prefix="/api/media", tags=["media"])


def _get_asset_row_or_404(asset_id: int, db: Session) -> MediaAsset:
    asset = db.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Media asset not found: {asset_id}",
        )
    return asset


def _asset_file_path_or_404(
    asset_id: int, asset: MediaAsset, storage: StorageBackend
) -> Path:
    """The file behind this row; a row whose bytes are gone reads as a deleted asset."""
    path = storage.resolve(asset.storage_key)
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Media asset not found: {asset_id}",
        )
    return path


@router.get("/{asset_id}")
def download_asset(
    asset_id: int,
    db: Session = Depends(get_db),
    storage: StorageBackend = Depends(get_storage_backend),
) -> FileResponse:
    """Stream one asset's own bytes, inline, whatever its `is_current` flag says."""
    asset = _get_asset_row_or_404(asset_id, db)
    path = _asset_file_path_or_404(asset_id, asset, storage)
    return FileResponse(
        path,
        media_type=asset.content_type,
        filename=path.name,
        content_disposition_type="inline",
    )


def _to_asset_response(asset: MediaAsset) -> MediaAssetOut:
    """The upload route's own response shape; the storage key is never part of it."""
    return MediaAssetOut(
        assetId=asset.id,
        kind=MediaKind(asset.kind),
        url=asset.url,
        size=asset.size_bytes,
        contentType=asset.content_type,
        sha256=asset.sha256,
    )


@router.get("/{asset_id}/metadata", response_model=MediaAssetOut)
def get_asset_metadata(
    asset_id: int,
    db: Session = Depends(get_db),
    storage: StorageBackend = Depends(get_storage_backend),
) -> MediaAssetOut:
    """One asset's metadata; a row whose bytes are gone reads as a deleted asset."""
    asset = _get_asset_row_or_404(asset_id, db)
    _asset_file_path_or_404(asset_id, asset, storage)
    return _to_asset_response(asset)
