"""Public reads of a stored media asset: the bytes themselves and their metadata.

The upload route writes the bytes through one `StorageBackend`, and these routes read
them back through the same seam: `StorageBackend.path_for` turns the row's storage key
into a file `FileResponse` streams, so the router never touches a disk path or an SDK of
its own. Both routes are public -- a poster or backdrop has to load directly in the
client -- and neither filters on `is_current`, so an asset superseded by a later upload
of the same kind still serves its own bytes.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..media import StorageBackend, get_storage_backend
from ..models import MediaAsset
from ..schemas import MediaAssetOut, MediaKind

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
