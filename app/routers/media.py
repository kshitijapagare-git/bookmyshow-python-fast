"""Public reads of a stored media asset, plus the upload of a review's photo.

The upload route writes the bytes through one `StorageBackend`, and these routes read
them back through the same seam: `StorageBackend.path_for` turns the row's storage key
into a file `FileResponse` streams, so the router never touches a disk path or an SDK of
its own. The two reads are public -- a poster or backdrop has to load directly in the
client -- and neither filters on `is_current`, so an asset superseded by a later upload
of the same kind still serves its own bytes.

`GET /{assetId}/integrity` and `POST /{assetId}/verify` are the moderator's reads: they
re-hash the stored bytes through `integrity_for`/`recompute_digest`, so their answer
always comes from storage rather than from the digest column, and both take the internal
media service token.

`review_router` is the one upload that does not belong to a movie: a review's photo is
posted against the review it illustrates, so its row carries `review_id` instead of
`movie_id` and the SHA-256 of its bytes is recorded on that row by the same streaming
save the movie upload uses.
"""

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..media import (
    ALLOWED_CONTENT_TYPES,
    EmptyUploadError,
    MediaSettings,
    MediaTooLargeError,
    StorageBackend,
    get_media_settings,
    get_storage_backend,
    integrity_for,
    max_bytes_for,
    recompute_digest,
)
from ..models import MediaAsset
from ..schemas import (
    MediaAssetOut,
    MediaIntegrityOut,
    MediaKind,
    MediaVerifyIn,
    MediaVerifyOut,
)
from ..security import require_media_service_token

router = APIRouter(prefix="/api/media", tags=["media"])
review_router = APIRouter(prefix="/api/reviews", tags=["media"])


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


@router.get(
    "/{asset_id}/integrity",
    response_model=MediaIntegrityOut,
    dependencies=[Depends(require_media_service_token)],
)
def get_media_asset_integrity(
    asset_id: int,
    db: Session = Depends(get_db),
    storage: StorageBackend = Depends(get_storage_backend),
    settings: MediaSettings = Depends(get_media_settings),
) -> MediaIntegrityOut:
    """Report the digest recorded at upload and whether storage still matches it.

    The digest and size are the row's own values; only `verified` is recomputed, so a
    file whose bytes changed after upload reads as untrusted while the metadata it was
    stored with stays visible.
    """
    asset = _get_asset(asset_id, db)
    integrity = integrity_for(
        storage=storage,
        storage_key=asset.storage_key,
        recorded_sha256=asset.sha256,
        size_bytes=asset.size_bytes,
        algorithm=settings.hash_algorithm,
    )
    return MediaIntegrityOut(
        sha256=integrity.sha256,
        size=integrity.size,
        verified=integrity.verified,
    )


@router.post(
    "/{asset_id}/verify",
    response_model=MediaVerifyOut,
    dependencies=[Depends(require_media_service_token)],
)
def verify_media_asset_checksum(
    asset_id: int,
    payload: MediaVerifyIn,
    db: Session = Depends(get_db),
    storage: StorageBackend = Depends(get_storage_backend),
    settings: MediaSettings = Depends(get_media_settings),
) -> MediaVerifyOut:
    """Check a submitted digest against the digest of the bytes now in storage.

    The submission only matches while the stored bytes still match the digest recorded
    at upload, so a tampered file answers `match=False` for the original, correct digest
    and for the digest of the bytes now on disk alike.
    """
    asset = _get_asset(asset_id, db)
    recomputed = recompute_digest(
        storage.path_for(asset.storage_key), algorithm=settings.hash_algorithm
    )
    return MediaVerifyOut(
        match=(
            recomputed is not None
            and recomputed == asset.sha256
            and payload.sha256.lower() == recomputed
        )
    )


@review_router.post(
    "/{review_id}/media",
    response_model=MediaAssetOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_media_service_token)],
)
async def upload_review_photo(
    review_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    settings: MediaSettings = Depends(get_media_settings),
    storage: StorageBackend = Depends(get_storage_backend),
) -> MediaAssetOut:
    """Store one review's photo and record the SHA-256 computed while streaming it.

    The kind is fixed to REVIEW_PHOTO, so this route can only ever write a review
    photo; the image allow-list and the image byte limit apply as they do to a poster.
    The reviews themselves live in the ratings service, so no existence check is made
    here: the uploaded row simply references the review id from the path.
    """
    kind = MediaKind.REVIEW_PHOTO

    declared = (file.content_type or "").split(";")[0].strip().lower()
    if declared not in ALLOWED_CONTENT_TYPES[kind]:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"{kind.value} does not accept {declared or 'that content type'}",
        )

    limit = max_bytes_for(kind, settings)
    try:
        stored = await storage.save(
            kind=kind, content_type=declared, upload=file, max_bytes=limit
        )
    except MediaTooLargeError:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"{kind.value} upload exceeds the {limit} byte limit",
        ) from None
    except EmptyUploadError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Uploaded file is empty",
        ) from None

    url = storage.public_url(stored.key)
    # The review's previous photo keeps its row and its file; only the flag moves.
    for previous in db.scalars(
        select(MediaAsset).where(
            MediaAsset.review_id == review_id,
            MediaAsset.kind == kind.value,
            MediaAsset.is_current.is_(True),
        )
    ):
        previous.is_current = False

    asset = MediaAsset(
        movie_id=None,
        review_id=review_id,
        kind=kind.value,
        storage_key=stored.key,
        url=url,
        content_type=declared,
        size_bytes=stored.size,
        sha256=stored.sha256,
        is_current=True,
    )
    db.add(asset)
    db.commit()

    return MediaAssetOut(
        assetId=asset.id,
        kind=MediaKind(asset.kind),
        url=asset.url,
        size=asset.size_bytes,
        contentType=asset.content_type,
        sha256=asset.sha256,
    )
