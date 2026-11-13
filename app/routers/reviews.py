from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..bookings import PAYMENTS_UNAVAILABLE_DETAIL
from ..db import get_db
from ..groups import find_member
from ..media import (
    ALLOWED_CONTENT_TYPES,
    EmptyUploadError,
    MediaSettings,
    MediaTooLargeError,
    StorageBackend,
    get_media_settings,
    get_storage_backend,
    max_bytes_for,
)
from ..models import Booking, Group, MediaAsset, Movie, Show, User
from ..payments_client import (
    PaymentsClient,
    PaymentsUnavailableError,
    get_payments_client,
)
from ..reviews_client import ReviewsClient, ReviewsUnavailableError, get_reviews_client
from ..schemas import (
    BookingStatus,
    MediaKind,
    MemberPaymentStatus,
    ReviewOut,
    ReviewPhotoOut,
    ReviewSubmitIn,
    ReviewsPageOut,
)
from ..security import get_current_user

router = APIRouter(prefix="/api/movies", tags=["reviews"])

REVIEWS_UNAVAILABLE_DETAIL = "Reviews are unavailable right now. Please try again."
REVIEW_FORBIDDEN_DETAIL = "Only moviegoers with a confirmed booking can review."
PAID_PAYMENT_STATUS = "SUCCEEDED"
PAGE_SIZE_DEFAULT = 10
PAGE_SIZE_MAX = 50


def _clamp_paging(page: int, page_size: int) -> tuple[int, int]:
    """Floor the page at 1 and keep the page size inside 1..50, as the ratings API does."""
    return max(page, 1), min(max(page_size, 1), PAGE_SIZE_MAX)


def _photo_by_review(
    review_ids: list[int], db: Session
) -> dict[int, dict[str, object]]:
    """Each review's current, trusted photo, keyed by review id.

    Only a row that still passes integrity is returned, so a tampered photo never
    reaches the public card even though its review is listed as usual.
    """
    if not review_ids:
        return {}
    rows = db.scalars(
        select(MediaAsset).where(
            MediaAsset.review_id.in_(review_ids),
            MediaAsset.kind == MediaKind.REVIEW_PHOTO.value,
            MediaAsset.is_current.is_(True),
            MediaAsset.verified.is_(True),
        )
    )
    return {
        row.review_id: {
            "url": row.url,
            "sha256": row.sha256,
            "verified": row.verified,
        }
        for row in rows
        if row.review_id is not None
    }


@router.get("/{movie_id}/reviews", response_model=ReviewsPageOut)
def list_movie_reviews(
    movie_id: int,
    page: int = 1,
    pageSize: int = PAGE_SIZE_DEFAULT,
    db: Session = Depends(get_db),
    client: ReviewsClient = Depends(get_reviews_client),
) -> ReviewsPageOut:
    """Read a movie's reviews and summary; the ratings service owns the data."""
    safe_page, safe_page_size = _clamp_paging(page, pageSize)
    try:
        body = client.list_reviews(
            movie_id=movie_id, page=safe_page, page_size=safe_page_size
        )
    except ReviewsUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=REVIEWS_UNAVAILABLE_DETAIL,
        ) from None

    reviews = body.get("reviews")
    if isinstance(reviews, list):
        photos = _photo_by_review(
            [
                review["id"]
                for review in reviews
                if isinstance(review, dict) and isinstance(review.get("id"), int)
            ],
            db,
        )
        if photos:
            # Copied rather than mutated: the page belongs to the ratings service.
            body = {
                **body,
                "reviews": [
                    (
                        {**review, "photo": photos[review["id"]]}
                        if isinstance(review, dict)
                        and isinstance(review.get("id"), int)
                        and review["id"] in photos
                        else review
                    )
                    for review in reviews
                ],
            }

    return ReviewsPageOut.model_validate(body)


def _review_forbidden() -> HTTPException:
    """Every refusal of a review looks the same, so the ticket's sentence is the detail."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail=REVIEW_FORBIDDEN_DETAIL
    )


@router.post(
    "/{movie_id}/reviews",
    response_model=ReviewOut,
    status_code=status.HTTP_201_CREATED,
)
def create_movie_review(
    movie_id: int,
    payload: ReviewSubmitIn,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    payments: PaymentsClient = Depends(get_payments_client),
    reviews: ReviewsClient = Depends(get_reviews_client),
) -> ReviewOut:
    """Review a movie, against the caller's own paid booking or their paid group share."""
    if payload.groupId is not None:
        group = db.get(Group, payload.groupId)
        if group is None:
            raise _review_forbidden()
        member = find_member(db=db, group_id=group.id, user_id=user.id)
        if (
            member is None
            or member.payment_status != MemberPaymentStatus.PAID.value
            or group.status != BookingStatus.CONFIRMED.value
        ):
            raise _review_forbidden()

        group_movie_id = db.scalar(
            select(Show.movie_id).where(Show.id == group.show_id)
        )
        if group_movie_id != movie_id:
            raise _review_forbidden()
    else:
        booking = db.get(Booking, payload.bookingId)
        if (
            booking is None
            or booking.user_id != user.id
            or booking.status != BookingStatus.CONFIRMED.value
        ):
            raise _review_forbidden()

        booked_movie_id = db.scalar(
            select(Show.movie_id).where(Show.id == booking.show_id)
        )
        if booked_movie_id != movie_id:
            raise _review_forbidden()

        try:
            paid_rows = payments.list_payments(booking_id=payload.bookingId)
        except PaymentsUnavailableError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=PAYMENTS_UNAVAILABLE_DETAIL,
            ) from None
        if not any(row.get("status") == PAID_PAYMENT_STATUS for row in paid_rows):
            raise _review_forbidden()

    try:
        body, created = reviews.create_review(
            movie_id=movie_id,
            user_id=user.id,
            booking_id=payload.bookingId,
            group_id=payload.groupId,
            rating=payload.rating,
            comment=payload.comment,
            verified=True,
        )
    except ReviewsUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=REVIEWS_UNAVAILABLE_DETAIL,
        ) from None

    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return ReviewOut.model_validate(body)


@router.post(
    "/{movie_id}/reviews/{review_id}/photo",
    response_model=ReviewPhotoOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_review_photo(
    movie_id: int,
    review_id: int,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: MediaSettings = Depends(get_media_settings),
    storage: StorageBackend = Depends(get_storage_backend),
) -> ReviewPhotoOut:
    """Store one photo for a review, keeping the SHA-256 of the bytes that landed."""
    if db.get(Movie, movie_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Movie not found: {movie_id}",
        )

    declared = (file.content_type or "").split(";")[0].strip().lower()
    if declared not in ALLOWED_CONTENT_TYPES[MediaKind.REVIEW_PHOTO]:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"{MediaKind.REVIEW_PHOTO.value} does not accept "
                f"{declared or 'that content type'}"
            ),
        )

    limit = max_bytes_for(MediaKind.REVIEW_PHOTO, settings)
    try:
        stored = await storage.save(
            kind=MediaKind.REVIEW_PHOTO,
            content_type=declared,
            upload=file,
            max_bytes=limit,
        )
    except MediaTooLargeError:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"{MediaKind.REVIEW_PHOTO.value} upload exceeds the "
                f"{limit} byte limit"
            ),
        ) from None
    except EmptyUploadError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Uploaded file is empty",
        ) from None

    url = storage.public_url(stored.key)
    # A second photo for the same review keeps the first row and its file; only the
    # current flag moves, exactly as a second poster does.
    for previous in db.scalars(
        select(MediaAsset).where(
            MediaAsset.review_id == review_id,
            MediaAsset.kind == MediaKind.REVIEW_PHOTO.value,
            MediaAsset.is_current.is_(True),
        )
    ):
        previous.is_current = False

    asset = MediaAsset(
        movie_id=movie_id,
        review_id=review_id,
        kind=MediaKind.REVIEW_PHOTO.value,
        storage_key=stored.key,
        url=url,
        content_type=declared,
        size_bytes=stored.size,
        sha256=stored.sha256,
        is_current=True,
        # The digest was computed from the bytes that landed, so the photo starts trusted.
        verified=True,
    )
    db.add(asset)
    db.commit()

    return ReviewPhotoOut(
        url=asset.url, sha256=asset.sha256, verified=asset.verified
    )
