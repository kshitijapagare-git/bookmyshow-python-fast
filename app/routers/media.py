"""Reads of a stored media asset: the bytes, their metadata and their integrity.

The upload route writes the bytes through one `StorageBackend`, and these routes read
them back through the same seam: `StorageBackend.path_for` turns the row's storage key
into a file `FileResponse` streams, so the router never touches a disk path or an SDK of
its own. The bytes and the metadata are public -- a poster or backdrop has to load
directly in the client -- and neither filters on `is_current`, so an asset superseded by
a later upload of the same kind still serves its own bytes.

The presigned flow is the other way in, for a browser that uploads straight to storage:
`POST /presign` creates a pending asset row and answers a signed `PUT` url, the upload
route streams the raw bytes from `request.stream()` into the same backend, and
`POST /{assetId}/complete` re-derives the digest and size from what storage holds and
makes the asset current for its owner. The upload route is authorised by its query-string
token alone, so it is exempt from the encryption middleware.

The integrity and verify routes are authenticated instead: they recompute the stored
object's digest through `integrity_report` and write the resulting flag onto the row,
which is what the public review card reads before it shows a review photo.
"""

import secrets
import time
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
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
from ..models import MediaAsset, ReviewAuthor, User
from ..presign import PresignTokenError, sign_presign_token, verify_presign_token
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

router = APIRouter(prefix="/api/media", tags=["media"])

PRESIGN_TOKEN_QUERY = "token"
PRESIGN_FORBIDDEN_DETAIL = "Upload token is invalid or has expired"
REVIEW_OWNER_FORBIDDEN_DETAIL = "This review belongs to another user"
SIZE_MISMATCH_DETAIL = "Uploaded size does not match the declared size"
TYPE_MISMATCH_DETAIL = "Uploaded content type is not allowed for this asset"
INCOMPLETE_ASSET_DETAIL = "Asset bytes have not been uploaded yet"


def _get_asset(asset_id: int, db: Session) -> MediaAsset:
    """The asset row for this id; only a missing row is a 404, never a superseded one."""
    asset = db.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Media asset not found: {asset_id}",
        )
    return asset


def _bare_content_type(raw: str) -> str:
    """The bare media type, so `image/png; charset=…` compares as `image/png`."""
    return raw.split(";")[0].strip().lower()


def _demote_previous_current(asset: MediaAsset, db: Session) -> None:
    """Move the `is_current` flag off this owner's previous asset of the same kind.

    The owner is the user for an avatar and the review for a review photo, which is
    exactly what a second upload is meant to supersede; the row and its file are kept.
    """
    query = select(MediaAsset).where(
        MediaAsset.kind == asset.kind,
        MediaAsset.is_current.is_(True),
        MediaAsset.id != asset.id,
    )
    if asset.kind == MediaKind.AVATAR.value:
        query = query.where(MediaAsset.user_id == asset.user_id)
    elif asset.kind == MediaKind.REVIEW_PHOTO.value:
        query = query.where(MediaAsset.review_id == asset.review_id)
    else:
        query = query.where(MediaAsset.movie_id == asset.movie_id)
    for previous in db.scalars(query):
        previous.is_current = False


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
    storage: StorageBackend = Depends(get_storage_backend),
) -> MediaPresignOut:
    """Create the pending asset row and answer where the bytes should be PUT.

    An avatar always belongs to the caller, whatever `ownerId` carried. A review photo
    belongs to the review `ownerId` names, which is authorised against the local
    `review_authors` index because the ratings service answers no by-id review read.
    The declared size and content type are checked against the same `MediaSettings`
    limits the multipart routes enforce, so oversized or unsupported bytes are refused
    before anything is stored.
    """
    if payload.purpose is MediaPurpose.AVATAR:
        kind = MediaKind.AVATAR
        movie_id: int | None = None
        review_id: int | None = None
        user_id: int | None = user.id
    else:
        owner_id = payload.ownerId
        author = db.get(ReviewAuthor, owner_id) if owner_id is not None else None
        if author is None or author.user_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=REVIEW_OWNER_FORBIDDEN_DETAIL,
            )
        kind = MediaKind.REVIEW_PHOTO
        movie_id = author.movie_id
        review_id = author.review_id
        user_id = None

    declared = _bare_content_type(payload.contentType)
    if declared not in ALLOWED_CONTENT_TYPES[kind]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=TYPE_MISMATCH_DETAIL,
        )
    limit = max_bytes_for(kind, settings)
    if payload.size > limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{kind.value} upload exceeds the {limit} byte limit",
        )

    # An empty storage key and digest mark "bytes not landed yet"; the declared size
    # stays on the row so `complete` can still tell a short upload from a correct one.
    asset = MediaAsset(
        movie_id=movie_id,
        user_id=user_id,
        review_id=review_id,
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

    expires_at = int(time.time()) + settings.presign_expires_seconds
    token = sign_presign_token(
        asset_id=asset.id, expires_at=expires_at, secret=settings.presign_secret
    )
    return MediaPresignOut(
        assetId=asset.id,
        uploadUrl=f"/api/media/{asset.id}/upload?{PRESIGN_TOKEN_QUERY}={token}",
        method="PUT",
        expiresIn=settings.presign_expires_seconds,
        headers={"Content-Type": declared},
    )


@router.put("/{asset_id}/upload", status_code=status.HTTP_204_NO_CONTENT)
async def upload_presigned_media(
    asset_id: int,
    request: Request,
    token: Annotated[str, Query(alias=PRESIGN_TOKEN_QUERY)],
    db: Session = Depends(get_db),
    settings: MediaSettings = Depends(get_media_settings),
    storage: StorageBackend = Depends(get_storage_backend),
) -> Response:
    """Accept the raw bytes the signed token authorises, streaming them to storage.

    The token is the only authorisation: the browser sends neither a bearer token nor
    an encryption session. The body is taken from `request.stream()` rather than a
    `Body` parameter, so no large image is ever buffered by FastAPI or read whole here.
    """
    asset = _get_asset(asset_id, db)
    try:
        verify_presign_token(
            token=token, asset_id=asset_id, secret=settings.presign_secret
        )
    except PresignTokenError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=PRESIGN_FORBIDDEN_DETAIL,
        ) from None

    presented = _bare_content_type(request.headers.get("content-type", ""))
    if presented and presented != asset.content_type:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=TYPE_MISMATCH_DETAIL,
        )

    kind = MediaKind(asset.kind)
    limit = max_bytes_for(kind, settings)
    try:
        stored = await storage.save_stream(
            kind=kind,
            content_type=asset.content_type,
            stream=request.stream(),
            max_bytes=limit,
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

    # The declared size is deliberately left alone: `complete` compares it with what
    # storage actually holds, so writing the real size here would hide a short upload.
    asset.storage_key = stored.key
    asset.url = storage.public_url(stored.key)
    asset.sha256 = stored.sha256
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
    """Verify the stored bytes against the declared upload and make the asset current."""
    asset = _get_asset(asset_id, db)
    if not asset.sha256:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=INCOMPLETE_ASSET_DETAIL,
        )

    kind = MediaKind(asset.kind)
    if asset.content_type not in ALLOWED_CONTENT_TYPES[kind]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=TYPE_MISMATCH_DETAIL,
        )

    # Re-derived from the bytes storage holds: a client-supplied digest is never trusted.
    sha256, size = digest_of_file(
        path=storage.path_for(asset.storage_key), algorithm=settings.hash_algorithm
    )
    if size != asset.size_bytes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=SIZE_MISMATCH_DETAIL,
        )

    asset.sha256 = sha256
    asset.size_bytes = size
    asset.verified = True
    _demote_previous_current(asset, db)
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
    # A presigned row whose bytes never landed has nothing to serve.
    if not asset.sha256:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=INCOMPLETE_ASSET_DETAIL,
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
