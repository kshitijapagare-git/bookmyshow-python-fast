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

`POST /presign` is the direct-to-storage entry point: it mints a short-lived signed
`PUT` url from `app/presign.py` so a large avatar or review photo never travels through
the API, and it creates the asset row up front so the upload and the complete step both
have an owner to check against.

`PUT /{asset_id}/upload` is that url's other end: the signed token, not a bearer token,
is the capability. It streams the raw body through `StorageBackend.save_stream` and
leaves the row unverified and not current, so a download of an asset whose bytes have
not arrived yet is a 409 rather than a missing file.

`POST /{asset_id}/complete` is what makes the upload real: the owner is checked, the
stored object is recomputed through `integrity_report`, and only then does the row
become verified and current -- which is the moment it shows up on the public read paths.
"""

import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..media import (
    ALLOWED_CONTENT_TYPES,
    EmptyUploadError,
    IntegrityResult,
    MediaSettings,
    MediaTooLargeError,
    StorageBackend,
    get_media_settings,
    get_storage_backend,
    integrity_report,
    max_bytes_for,
    sanitize_filename,
)
from ..models import MediaAsset, User
from ..presign import UPLOAD_METHOD, sign_upload_token, upload_url, verify_upload_token
from ..reviews_client import ReviewsClient, ReviewsUnavailableError, get_reviews_client
from ..schemas import (
    MediaAssetOut,
    MediaCompleteOut,
    MediaIntegrityOut,
    MediaKind,
    MediaPresignIn,
    MediaPresignOut,
    MediaPurpose,
    MediaVerifyIn,
    MediaVerifyOut,
)
from ..security import get_current_user
from .reviews import REVIEWS_UNAVAILABLE_DETAIL

router = APIRouter(prefix="/api/media", tags=["media"])

PURPOSE_KINDS: dict[MediaPurpose, MediaKind] = {
    MediaPurpose.AVATAR: MediaKind.AVATAR,
    MediaPurpose.REVIEW_PHOTO: MediaKind.REVIEW_PHOTO,
}
PRESIGN_FORBIDDEN_DETAIL = (
    "Only the moviegoer who wrote the review can add its photo."
)
UPLOAD_TOKEN_FORBIDDEN_DETAIL = "Upload link is invalid or has expired"
UPLOAD_MISMATCH_DETAIL = "Uploaded bytes do not match the presigned upload"
PENDING_UPLOAD_DETAIL = "Media asset has no uploaded bytes yet"
COMPLETE_FORBIDDEN_DETAIL = (
    "Only the moviegoer who presigned this upload can complete it."
)
COMPLETE_MISMATCH_DETAIL = "Stored bytes do not match the presigned upload"


def _presign_forbidden() -> HTTPException:
    """Every authorship refusal is the same 403, so it never says which check failed."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail=PRESIGN_FORBIDDEN_DETAIL
    )


def _upload_forbidden() -> HTTPException:
    """Every capability refusal is the same 403, so it never says which check failed."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail=UPLOAD_TOKEN_FORBIDDEN_DETAIL
    )


def _upload_mismatch() -> HTTPException:
    """Every refused byte stream is the same 422, whatever tripped it."""
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=UPLOAD_MISMATCH_DETAIL,
    )


def _complete_mismatch() -> HTTPException:
    """Every refused complete is the same 422, whatever the stored bytes look like."""
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=COMPLETE_MISMATCH_DETAIL,
    )


def _get_asset(asset_id: int, db: Session) -> MediaAsset:
    """The asset row for this id; only a missing row is a 404, never a superseded one."""
    asset = db.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Media asset not found: {asset_id}",
        )
    return asset


def _require_owner(asset: MediaAsset, user: User) -> None:
    """Only the moviegoer who presigned the asset may finish it.

    The same rule the write path uses: a bearer token proves who is calling, never that
    the asset is theirs, so an asset presigned by somebody else is a 403 for everyone.
    """
    if asset.user_id is None or asset.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=COMPLETE_FORBIDDEN_DETAIL,
        )


@router.post("/presign", response_model=MediaPresignOut)
def presign_media_upload(
    payload: MediaPresignIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: MediaSettings = Depends(get_media_settings),
    reviews: ReviewsClient = Depends(get_reviews_client),
) -> MediaPresignOut:
    """Reserve an asset and hand back the signed url the client PUTs its bytes to.

    The row is created here rather than at upload time, unverified and not current, so
    the upload route has a declared size and content type to check against and the
    complete route has an owner: an avatar belongs to the caller, a review photo to a
    review the caller demonstrably wrote.
    """
    kind = PURPOSE_KINDS[payload.purpose]

    declared = payload.contentType.split(";")[0].strip().lower()
    if declared not in ALLOWED_CONTENT_TYPES[kind]:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"{kind.value} does not accept {declared or 'that content type'}",
        )

    limit = max_bytes_for(kind, settings)
    if payload.size > limit:
        # An upload of this size could never be completed, so it is refused up front.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{kind.value} upload exceeds the {limit} byte limit",
        )

    review_id: int | None = None
    movie_id: int | None = None
    if payload.purpose is MediaPurpose.REVIEW_PHOTO:
        owner_id = payload.ownerId
        movie_id = payload.movieId
        if owner_id is None or movie_id is None:
            # The request model already requires both for a review photo.
            raise _presign_forbidden()
        try:
            review = reviews.find_review(movie_id=movie_id, review_id=owner_id)
        except ReviewsUnavailableError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=REVIEWS_UNAVAILABLE_DETAIL,
            ) from None
        # A review of another movie and a review of another user are the same refusal.
        if review is None or review.get("userId") != user.id:
            raise _presign_forbidden()
        review_id = owner_id

    asset = MediaAsset(
        movie_id=movie_id,
        user_id=user.id,
        review_id=review_id,
        # Metadata only: the storage key is content-addressed at upload time.
        original_filename=sanitize_filename(payload.filename),
        kind=kind.value,
        storage_key="",
        url="",
        content_type=declared,
        size_bytes=payload.size,
        sha256="",
        is_current=False,
        verified=False,
    )
    db.add(asset)
    db.commit()

    token = sign_upload_token(
        asset_id=asset.id,
        expires_at=int(time.time()) + settings.presign_expires_seconds,
        secret=settings.presign_secret,
    )
    return MediaPresignOut(
        assetId=asset.id,
        uploadUrl=upload_url(asset_id=asset.id, token=token),
        method=UPLOAD_METHOD,
        expiresIn=settings.presign_expires_seconds,
        headers={"Content-Type": declared},
    )


@router.put(
    "/{asset_id}/upload",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        403: {"description": "The token is invalid, expired or already used"},
        422: {"description": "The bytes or their content type differ from the presign"},
    },
)
async def upload_media_asset(
    asset_id: int,
    request: Request,
    token: str = Query(default=""),
    db: Session = Depends(get_db),
    settings: MediaSettings = Depends(get_media_settings),
    storage: StorageBackend = Depends(get_storage_backend),
) -> Response:
    """Stream the raw body of a presigned upload straight into storage.

    The signed token -- not a bearer token -- is the capability, and it is checked
    against the path's asset id before the row is even read, so a missing, expired,
    altered or other-asset token never reaches storage. A link that already wrote its
    bytes is refused rather than overwritten, and the row keeps its empty storage key on
    every refusal, so only a complete upload can be completed.
    """
    if not verify_upload_token(
        token=token, asset_id=asset_id, secret=settings.presign_secret
    ):
        raise _upload_forbidden()

    asset = _get_asset(asset_id, db)
    if asset.storage_key or asset.verified:
        # Single use: an asset that already has bytes is never written a second time.
        raise _upload_forbidden()

    declared = (
        (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    )
    if declared != asset.content_type:
        raise _upload_mismatch()

    try:
        stored = await storage.save_stream(
            kind=MediaKind(asset.kind),
            content_type=asset.content_type,
            chunks=request.stream(),
            max_bytes=asset.size_bytes,
        )
    except MediaTooLargeError:
        # More bytes than declared: the .part file is already gone.
        raise _upload_mismatch() from None
    except EmptyUploadError:
        raise _upload_mismatch() from None

    if stored.size != asset.size_bytes:
        # Fewer bytes than declared. The object landed, so it is removed again unless a
        # row already serves that content-addressed key, and the row stays unwritten.
        shared = db.scalar(
            select(MediaAsset.id)
            .where(MediaAsset.storage_key == stored.key)
            .limit(1)
        )
        if shared is None:
            storage.path_for(stored.key).unlink(missing_ok=True)
        raise _upload_mismatch()

    asset.storage_key = stored.key
    asset.url = storage.public_url(stored.key)
    asset.sha256 = stored.sha256
    asset.size_bytes = stored.size
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{asset_id}/complete",
    response_model=MediaCompleteOut,
    responses={
        403: {"description": "The caller did not presign this asset"},
        422: {"description": "The stored bytes are missing or differ from the presign"},
    },
)
def complete_media_asset(
    asset_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: StorageBackend = Depends(get_storage_backend),
    settings: MediaSettings = Depends(get_media_settings),
) -> MediaCompleteOut:
    """Validate the uploaded bytes and make the asset its owner's current one.

    This is the step that turns a presigned row into a real asset: the stored object is
    recomputed through `integrity_report`, so an upload that never arrived, or one whose
    bytes were replaced, is refused instead of being published. Only then does the flag
    move, and the owner's previous avatar -- or the review's previous photo -- stops
    being current, exactly as a second multipart upload would move it. A repeat call by
    the owner is idempotent and answers the same body.
    """
    asset = _get_asset(asset_id, db)
    _require_owner(asset, user)

    if not asset.storage_key:
        # The bytes never arrived, so there is nothing to validate or link.
        raise _complete_mismatch()

    if asset.content_type not in ALLOWED_CONTENT_TYPES[MediaKind(asset.kind)]:
        # The asset's declared type is not one its kind accepts, so it is not linked.
        raise _complete_mismatch()

    report = _report(asset, storage, settings)
    if (
        not report.verified
        or report.recomputed_size is None
        or report.recomputed_size != asset.size_bytes
    ):
        raise _complete_mismatch()

    if asset.kind == MediaKind.AVATAR.value:
        owner_filter = MediaAsset.user_id == asset.user_id
    elif asset.kind == MediaKind.REVIEW_PHOTO.value:
        owner_filter = MediaAsset.review_id == asset.review_id
    else:
        owner_filter = None
    if owner_filter is not None:
        # The superseded row keeps its row and its file; only the current flag moves.
        for previous in db.scalars(
            select(MediaAsset).where(
                owner_filter,
                MediaAsset.kind == asset.kind,
                MediaAsset.is_current.is_(True),
                MediaAsset.id != asset.id,
            )
        ):
            previous.is_current = False

    asset.verified = True
    asset.is_current = True
    db.commit()

    return MediaCompleteOut(
        assetId=asset.id,
        url=asset.url,
        sha256=asset.sha256,
        size=asset.size_bytes,
        contentType=asset.content_type,
        kind=MediaKind(asset.kind),
        verified=asset.verified,
    )


@router.get("/{asset_id}", response_class=FileResponse)
def download_media_asset(
    asset_id: int,
    db: Session = Depends(get_db),
    storage: StorageBackend = Depends(get_storage_backend),
) -> FileResponse:
    """Stream the stored bytes; Starlette fills Content-Length from the file's stat."""
    asset = _get_asset(asset_id, db)
    if not asset.storage_key:
        # A presigned asset whose bytes have not arrived yet has nothing to serve.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=PENDING_UPLOAD_DETAIL,
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
