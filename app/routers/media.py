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

`POST /presign` is the direct-to-storage entry point: it records a pending row (no
storage key and no `uploaded_at` yet) and answers the signed url the browser PUTs the
bytes to, so a large image never passes through the API. That `PUT .../upload` is the
one route here without a session or a bearer token -- the token in its query string is
the authorization -- and the write-back that makes the asset current happens once
those bytes have landed.
"""

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..media import (
    ALLOWED_CONTENT_TYPES,
    ExactSizeMismatchError,
    IntegrityResult,
    MediaSettings,
    RequestStreamChunks,
    StorageBackend,
    digest_of_file,
    get_media_settings,
    get_storage_backend,
    integrity_report,
    max_bytes_for,
)
from ..models import MediaAsset, User
from ..presign import PresignTokenError, decode_upload_token, upload_url
from ..reviews_client import ReviewsClient, ReviewsUnavailableError, get_reviews_client
from ..schemas import (
    MediaAssetOut,
    MediaIntegrityOut,
    MediaKind,
    MediaPresignIn,
    MediaPresignOut,
    MediaPurpose,
    MediaVerifyIn,
    MediaVerifyOut,
)
from ..security import get_current_user

router = APIRouter(prefix="/api/media", tags=["media"])

PRESIGN_FORBIDDEN_DETAIL = "Only the review's author can upload a photo for this review"
PRESIGN_TYPE_DETAIL = "{kind} does not accept {content_type}"
PRESIGN_SIZE_DETAIL = "{kind} upload exceeds the {limit} byte limit"
REVIEWS_UNAVAILABLE_DETAIL = "Reviews are unavailable right now. Please try again."
INVALID_UPLOAD_TOKEN_DETAIL = "Upload token is missing, invalid or expired"
UPLOAD_TYPE_MISMATCH_DETAIL = (
    "Upload content type does not match the presigned declaration"
)
UPLOAD_SIZE_MISMATCH_DETAIL = "Upload size does not match the presigned declaration"
COMPLETE_FORBIDDEN_DETAIL = "This media asset belongs to another user"
UPLOAD_MISSING_DETAIL = "This media asset has no uploaded bytes to complete"
COMPLETE_MISMATCH_DETAIL = (
    "Stored bytes do not match the recorded size, digest or content type"
)

PURPOSE_KINDS: dict[MediaPurpose, MediaKind] = {
    MediaPurpose.AVATAR: MediaKind.AVATAR,
    MediaPurpose.REVIEW_PHOTO: MediaKind.REVIEW_PHOTO,
}


def _get_asset(asset_id: int, db: Session) -> MediaAsset:
    """The asset row for this id; only a missing row is a 404, never a superseded one."""
    asset = db.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Media asset not found: {asset_id}",
        )
    return asset


def _kind_for(purpose: MediaPurpose) -> MediaKind:
    """The media kind a presigned purpose is stored as."""
    return PURPOSE_KINDS[purpose]


def _normalise_content_type(raw: str) -> str:
    """The bare lower-cased content type, dropping any `; charset=...` parameter."""
    return raw.split(";")[0].strip().lower()


@router.get("/{asset_id}", response_class=FileResponse)
def download_media_asset(
    asset_id: int,
    db: Session = Depends(get_db),
    storage: StorageBackend = Depends(get_storage_backend),
) -> FileResponse:
    """Stream the stored bytes; Starlette fills Content-Length from the file's stat.

    A row created by a presign whose bytes never landed has no storage key, so there is
    nothing to serve and the asset reads as missing rather than as a broken file.
    """
    asset = _get_asset(asset_id, db)
    if not asset.storage_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Media asset not found: {asset_id}",
        )
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


@router.post(
    "/presign",
    response_model=MediaPresignOut,
    status_code=status.HTTP_201_CREATED,
)
def presign_media_upload(
    payload: MediaPresignIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: MediaSettings = Depends(get_media_settings),
    reviews: ReviewsClient = Depends(get_reviews_client),
) -> MediaPresignOut:
    """Record a pending asset and answer the signed url the bytes are PUT to.

    An avatar belongs to the caller; a review photo belongs to the review named by
    `ownerId`, which is only accepted when the ratings service says the caller wrote
    it. The row exists before the bytes do, so the upload has an assetId to fill in and
    `uploaded_at` stays NULL until they land.
    """
    kind = _kind_for(payload.purpose)
    content_type = _normalise_content_type(payload.contentType)
    if content_type not in ALLOWED_CONTENT_TYPES[kind]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=PRESIGN_TYPE_DETAIL.format(
                kind=kind.value, content_type=content_type or "that content type"
            ),
        )

    limit = max_bytes_for(kind, settings)
    if payload.size > limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=PRESIGN_SIZE_DETAIL.format(kind=kind.value, limit=limit),
        )

    review_id: int | None = None
    movie_id: int | None = None
    if payload.purpose is MediaPurpose.REVIEW_PHOTO:
        review_id = payload.ownerId
        if review_id is None:
            # The request model already refuses this, so it is only reachable when a
            # caller bypasses validation.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="ownerId is required when purpose is review_photo",
            )
        try:
            review = reviews.get_review(review_id=review_id)
        except ReviewsUnavailableError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=REVIEWS_UNAVAILABLE_DETAIL,
            ) from None
        # An unknown review and someone else's review look the same, so the endpoint
        # never confirms which review ids exist.
        if review is None or review.get("userId") != user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=PRESIGN_FORBIDDEN_DETAIL,
            )
        movie_id = review.get("movieId")
        if isinstance(movie_id, bool) or not isinstance(movie_id, int):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=REVIEWS_UNAVAILABLE_DETAIL,
            )

    asset = MediaAsset(
        movie_id=movie_id,
        user_id=user.id,
        review_id=review_id,
        kind=kind.value,
        storage_key="",
        url="",
        content_type=content_type,
        size_bytes=payload.size,
        sha256="",
        original_filename=payload.filename,
        is_current=False,
        verified=False,
        uploaded_at=None,
    )
    db.add(asset)
    db.commit()

    return MediaPresignOut(
        assetId=asset.id,
        uploadUrl=upload_url(
            settings,
            asset_id=asset.id,
            kind=kind,
            size=payload.size,
            content_type=content_type,
        ),
        method="PUT",
        expiresIn=settings.presign_expires_seconds,
        headers={"Content-Type": content_type},
    )


@router.put("/{asset_id}/upload", response_model=MediaAssetOut)
async def receive_presigned_upload(
    asset_id: int,
    request: Request,
    token: str | None = None,
    db: Session = Depends(get_db),
    settings: MediaSettings = Depends(get_media_settings),
    storage: StorageBackend = Depends(get_storage_backend),
) -> MediaAssetOut:
    """Store the bytes a signed token authorises, at exactly the declared size.

    The token in the query string is the whole authorization: a direct upload carries
    no encryption session and no bearer token, so a missing, forged, expired or
    foreign token all read as the same 403 and write nothing. The declared content
    type is compared before a byte is read, and the body is streamed straight into
    storage, so a short or long body is refused without a file being kept.
    """
    if token is None or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=INVALID_UPLOAD_TOKEN_DETAIL,
        )
    try:
        grant = decode_upload_token(settings, token=token)
    except PresignTokenError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=INVALID_UPLOAD_TOKEN_DETAIL,
        ) from None

    asset = db.get(MediaAsset, asset_id)
    if grant.asset_id != asset_id or asset is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=INVALID_UPLOAD_TOKEN_DETAIL,
        )

    declared = _normalise_content_type(request.headers.get("content-type", ""))
    if declared != grant.content_type:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=UPLOAD_TYPE_MISMATCH_DETAIL,
        )

    try:
        stored = await storage.save_exact(
            kind=grant.kind,
            content_type=grant.content_type,
            upload=RequestStreamChunks(request.stream()),
            expected_size=grant.size,
        )
    except ExactSizeMismatchError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=UPLOAD_SIZE_MISMATCH_DETAIL,
        ) from None

    asset.storage_key = stored.key
    asset.url = storage.public_url(stored.key)
    asset.content_type = grant.content_type
    asset.size_bytes = stored.size
    asset.sha256 = stored.sha256
    asset.uploaded_at = datetime.now(timezone.utc)
    # The bytes landed and match their declaration, so the asset is trusted; becoming
    # the owner's current asset waits for the completion step.
    asset.verified = True
    db.commit()

    return MediaAssetOut(
        assetId=asset.id,
        kind=MediaKind(asset.kind),
        url=asset.url,
        size=asset.size_bytes,
        contentType=asset.content_type,
        sha256=asset.sha256,
    )


def _demote_previous(asset: MediaAsset, kind: MediaKind, db: Session) -> None:
    """Clear the current flag of whatever this asset replaces, keeping row and file.

    An avatar supersedes the same user's previous avatar; a review photo supersedes the
    previous photo of the same review. The asset being completed is skipped, because it
    is the one that becomes current next.
    """
    if kind is MediaKind.AVATAR:
        owned = MediaAsset.user_id == asset.user_id
    else:
        owned = MediaAsset.review_id == asset.review_id
    for previous in db.scalars(
        select(MediaAsset).where(
            owned,
            MediaAsset.kind == asset.kind,
            MediaAsset.id != asset.id,
            MediaAsset.is_current.is_(True),
        )
    ):
        previous.is_current = False


@router.post("/{asset_id}/complete", response_model=MediaAssetOut)
def complete_presigned_upload(
    asset_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: MediaSettings = Depends(get_media_settings),
    storage: StorageBackend = Depends(get_storage_backend),
) -> MediaAssetOut:
    """Verify the uploaded bytes, then make this asset the owner's current one.

    The row's owner has to be the caller: a bearer token alone is not authorization for
    someone else's asset. The bytes have to be there, and they have to still be exactly
    the bytes the upload recorded, so a replaced file is refused instead of linked.
    """
    asset = _get_asset(asset_id, db)
    if asset.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=COMPLETE_FORBIDDEN_DETAIL,
        )

    if not asset.storage_key or asset.uploaded_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=UPLOAD_MISSING_DETAIL,
        )
    path = storage.path_for(asset.storage_key)
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=UPLOAD_MISSING_DETAIL,
        )

    kind = MediaKind(asset.kind)
    sha256, size = digest_of_file(path=path, algorithm=settings.hash_algorithm)
    if (
        size != asset.size_bytes
        or not secrets.compare_digest(sha256, asset.sha256)
        or asset.content_type not in ALLOWED_CONTENT_TYPES[kind]
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=COMPLETE_MISMATCH_DETAIL,
        )

    _demote_previous(asset, kind, db)
    asset.is_current = True
    asset.verified = True
    db.commit()

    return MediaAssetOut(
        assetId=asset.id,
        kind=kind,
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
