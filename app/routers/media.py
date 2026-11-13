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

`POST /api/media/presign` and `POST /api/media/{assetId}/complete` are the two
authenticated ends of the direct-upload flow: presign reserves one owner-scoped row and
answers the signed local upload url, and complete confirms that the declared bytes
really landed before the asset becomes the caller's current one.
"""

import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
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
    digest_of_file,
    get_media_settings,
    get_storage_backend,
    integrity_report,
    max_bytes_for,
)
from ..media_presign import PresignTokenError, sign_upload_token, verify_upload_token
from ..models import MediaAsset, User
from ..reviews_client import ReviewsClient, ReviewsUnavailableError, get_reviews_client
from ..schemas import (
    MediaAssetOut,
    MediaCompleteOut,
    MediaIntegrityOut,
    MediaKind,
    MediaPresignIn,
    MediaPresignOut,
    MediaUploadPurpose,
    MediaVerifyIn,
    MediaVerifyOut,
)
from ..security import get_current_user
from .reviews import REVIEWS_UNAVAILABLE_DETAIL

router = APIRouter(prefix="/api/media", tags=["media"])

REVIEW_PHOTO_FORBIDDEN_DETAIL = "Only the author of a review can upload its photo."
REVIEW_NOT_FOUND_DETAIL = "Review not found"
PRESIGN_INVALID_DETAIL = "The upload does not match the presigned media limits"
COMPLETE_NOT_UPLOADED_DETAIL = "The upload has not landed yet"
ASSET_FORBIDDEN_DETAIL = "This media asset belongs to another user"
UPLOAD_TOKEN_INVALID_DETAIL = "The upload link is invalid or has expired"
UPLOAD_ALREADY_LANDED_DETAIL = "This media asset already has its bytes"
EMPTY_UPLOAD_DETAIL = "Uploaded file is empty"


def _get_asset(asset_id: int, db: Session) -> MediaAsset:
    """The asset row for this id; only a missing row is a 404, never a superseded one.

    A presigned asset exists before its bytes do and has no storage key yet, so it is
    a 404 here too: there is nothing to read back or report on until an upload lands.
    """
    asset = db.get(MediaAsset, asset_id)
    if asset is None or asset.storage_key is None:
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


@router.post("/presign", response_model=MediaPresignOut)
def presign_media_upload(
    payload: MediaPresignIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: MediaSettings = Depends(get_media_settings),
    reviews: ReviewsClient = Depends(get_reviews_client),
) -> MediaPresignOut:
    """Reserve one owner-scoped asset and answer the signed url its bytes go to.

    Nothing is written when the declaration is refused: the content type and the size
    are checked against the purpose's own allow-list and byte limit first, and a review
    photo's ownerId is resolved against the ratings service before the row exists.
    """
    kind = (
        MediaKind.AVATAR
        if payload.purpose is MediaUploadPurpose.AVATAR
        else MediaKind.REVIEW_PHOTO
    )
    # A size or type the kind does not accept is the ticket's size/type mismatch: a 422,
    # never the upload routes' 413/415.
    if payload.contentType not in ALLOWED_CONTENT_TYPES[kind]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=PRESIGN_INVALID_DETAIL,
        )
    if payload.size > max_bytes_for(kind, settings):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=PRESIGN_INVALID_DETAIL,
        )

    review_id: int | None = None
    if payload.purpose is MediaUploadPurpose.REVIEW_PHOTO:
        owner_id = payload.ownerId
        if owner_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=PRESIGN_INVALID_DETAIL,
            )
        try:
            review = reviews.get_review(review_id=owner_id)
        except ReviewsUnavailableError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=REVIEWS_UNAVAILABLE_DETAIL,
            ) from None
        if not review:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"{REVIEW_NOT_FOUND_DETAIL}: {owner_id}",
            )
        if review.get("userId") != user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=REVIEW_PHOTO_FORBIDDEN_DETAIL,
            )
        review_id = owner_id

    asset = MediaAsset(
        movie_id=None,
        # The presigner owns the asset: an avatar is scoped to them, and so is the
        # review photo they alone are allowed to have presigned.
        user_id=user.id,
        review_id=review_id,
        kind=kind.value,
        storage_key=None,
        url=None,
        content_type=payload.contentType,
        # Holds the declared size until the upload and its completion confirm it.
        size_bytes=payload.size,
        sha256=None,
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
        uploadUrl=f"/api/media/{asset.id}/upload?token={token}",
        method="PUT",
        expiresIn=settings.presign_expires_seconds,
        headers={"Content-Type": payload.contentType},
    )


@router.put(
    "/{asset_id}/upload",
    response_class=Response,
    status_code=status.HTTP_204_NO_CONTENT,
)
async def upload_presigned_media(
    asset_id: int,
    request: Request,
    token: str = Query(default=""),
    db: Session = Depends(get_db),
    settings: MediaSettings = Depends(get_media_settings),
    storage: StorageBackend = Depends(get_storage_backend),
) -> Response:
    """Stream a presigned upload's bytes straight into storage.

    The url carries its own signed token rather than a JWT, so a browser can post the
    bytes without an encryption session: the token is verified against this asset id
    first, and only then is the row loaded. The body is consumed as it arrives and
    never buffered whole, and the row keeps the key, url, digest and size it earned.
    """
    try:
        verify_upload_token(
            token=token, asset_id=asset_id, secret=settings.presign_secret
        )
    except PresignTokenError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=UPLOAD_TOKEN_INVALID_DETAIL,
        ) from None

    asset = db.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Media asset not found: {asset_id}",
        )
    if asset.storage_key is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=UPLOAD_ALREADY_LANDED_DETAIL,
        )

    kind = MediaKind(asset.kind)
    # The declared type is stored at presign, so storage only ever sees one it accepts.
    if asset.content_type not in ALLOWED_CONTENT_TYPES[kind]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=PRESIGN_INVALID_DETAIL,
        )
    declared_type = (
        (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    )
    if declared_type != asset.content_type:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=PRESIGN_INVALID_DETAIL,
        )

    # The size declared at presign is the ceiling here, so a longer body is a mismatch.
    max_bytes = max_bytes_for(kind, settings)
    if asset.size_bytes is not None:
        max_bytes = min(asset.size_bytes, max_bytes)

    try:
        stored = await storage.save_stream(
            kind=kind,
            content_type=asset.content_type,
            chunks=request.stream(),
            max_bytes=max_bytes,
        )
    except MediaTooLargeError:
        # A body past the presigned size is the ticket's mismatch: a 422, never a 413.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=PRESIGN_INVALID_DETAIL,
        ) from None
    except EmptyUploadError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=EMPTY_UPLOAD_DETAIL,
        ) from None

    asset.storage_key = stored.key
    asset.url = storage.public_url(stored.key)
    asset.sha256 = stored.sha256
    asset.size_bytes = stored.size
    db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{asset_id}/complete", response_model=MediaCompleteOut)
def complete_media_upload(
    asset_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: MediaSettings = Depends(get_media_settings),
    storage: StorageBackend = Depends(get_storage_backend),
) -> MediaCompleteOut:
    """Confirm the direct upload landed, then make it the caller's current asset."""
    # Deliberately not `_get_asset`: a pending row has no storage key yet, and this is
    # the route that turns it into a servable one.
    asset = db.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Media asset not found: {asset_id}",
        )
    if asset.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ASSET_FORBIDDEN_DETAIL,
        )
    if (
        asset.storage_key is None
        or asset.url is None
        or asset.sha256 is None
        or asset.size_bytes is None
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=COMPLETE_NOT_UPLOADED_DETAIL,
        )

    kind = MediaKind(asset.kind)
    if asset.content_type not in ALLOWED_CONTENT_TYPES[kind]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=PRESIGN_INVALID_DETAIL,
        )
    path = storage.path_for(asset.storage_key)
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=COMPLETE_NOT_UPLOADED_DETAIL,
        )
    _, recomputed_size = digest_of_file(path=path, algorithm=settings.hash_algorithm)
    if recomputed_size != asset.size_bytes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=PRESIGN_INVALID_DETAIL,
        )

    # The caller's previous avatar, or this review's previous photo, is superseded: it
    # keeps its row and its bytes, exactly as a second poster does.
    superseded = select(MediaAsset).where(
        MediaAsset.id != asset.id,
        MediaAsset.kind == asset.kind,
        MediaAsset.is_current.is_(True),
    )
    if kind is MediaKind.REVIEW_PHOTO:
        superseded = superseded.where(MediaAsset.review_id == asset.review_id)
    else:
        superseded = superseded.where(MediaAsset.user_id == user.id)
    for previous in db.scalars(superseded):
        previous.is_current = False

    asset.verified = True
    asset.is_current = True
    db.commit()

    return MediaCompleteOut(
        assetId=asset.id,
        url=asset.url,
        contentType=asset.content_type,
        size=asset.size_bytes,
        sha256=asset.sha256,
        kind=kind,
        verified=asset.verified,
    )
