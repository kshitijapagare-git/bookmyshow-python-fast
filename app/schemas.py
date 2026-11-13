from datetime import date, datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from .event_bus import EventBusTransport

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CityBase(BaseModel):
    name: NonBlank
    state: NonBlank


class CityCreate(CityBase):
    pass


class CityUpdate(CityBase):
    pass


class CityOut(CityBase):
    id: int
    slug: str

    model_config = ConfigDict(from_attributes=True)


class CinemaBase(BaseModel):
    name: NonBlank
    address: NonBlank
    city_id: int = Field(
        validation_alias=AliasChoices("cityId", "city_id"),
        serialization_alias="cityId",
    )


class CinemaCreate(CinemaBase):
    pass


class CinemaUpdate(CinemaBase):
    pass


class CinemaOut(BaseModel):
    id: int
    name: str
    address: str
    cityId: int


class Certificate(str, Enum):
    U = "U"
    UA = "UA"
    A = "A"
    S = "S"


class MovieSummaryOut(BaseModel):
    id: int
    title: str
    slug: str
    genres: str
    posterUrl: str | None = None
    rating: float | None = Field(default=None, ge=0.0, le=10.0)
    votes: int | None = None
    likes: int | None = None
    promoted: bool = False


class MovieOut(BaseModel):
    id: int
    title: str
    slug: str
    genres: str
    posterUrl: str | None = None
    rating: float | None = Field(default=None, ge=0.0, le=10.0)
    votes: int | None = None
    likes: int | None = None
    promoted: bool = False
    synopsis: str = Field(max_length=1000)
    languages: list[str] = Field(min_length=1)
    formats: list[Literal["2D", "3D", "IMAX", "4DX"]]
    durationMinutes: int = Field(ge=30, le=300)
    certificate: Certificate
    releaseDate: date
    backdropUrl: str
    trailerUrl: str | None = None
    interestedCount: int = 0


class MediaKind(str, Enum):
    POSTER = "POSTER"
    BACKDROP = "BACKDROP"
    TRAILER = "TRAILER"
    REVIEW_PHOTO = "REVIEW_PHOTO"


class MediaAssetOut(BaseModel):
    assetId: int
    kind: MediaKind
    url: str
    size: int
    contentType: str
    sha256: str


class MediaIntegrityOut(BaseModel):
    """An asset's recorded digest and size, plus whether its stored bytes still match."""

    sha256: str
    size: int
    verified: bool


class MediaVerifyIn(BaseModel):
    """A digest to check an asset's stored bytes against."""

    sha256: Annotated[
        str, StringConstraints(strip_whitespace=True, pattern=r"^[0-9a-fA-F]{64}$")
    ]


class MediaVerifyOut(BaseModel):
    """Whether the submitted digest matched the bytes now in storage."""

    match: bool


class Availability(str, Enum):
    AVAILABLE = "AVAILABLE"
    FAST_FILLING = "FAST_FILLING"


class SeatKind(str, Enum):
    STANDARD = "STANDARD"
    WHEELCHAIR = "WHEELCHAIR"


class ShowSeatStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    HELD = "HELD"
    SOLD = "SOLD"


class Amenity(str, Enum):
    M_TICKET = "M_TICKET"
    FOOD = "FOOD"
    PARKING = "PARKING"
    WHEELCHAIR = "WHEELCHAIR"


class ShowOut(BaseModel):
    id: int
    startsAt: datetime
    format: str
    language: str
    availability: Availability
    lateNight: bool
    earlyMorning: bool


class ShowCinemaOut(BaseModel):
    id: int
    name: str
    address: str
    chainLogoUrl: str | None = None
    cancellable: bool = False
    amenities: list[Amenity]
    shows: list[ShowOut]


class ShowDateOut(BaseModel):
    date: date
    hasShows: bool


class ShowMovieOut(BaseModel):
    id: int
    title: str
    durationMinutes: int
    certificate: Certificate
    genres: str


class SeatCategoryName(str, Enum):
    NORMAL = "NORMAL"
    EXECUTIVE = "EXECUTIVE"
    PREMIUM = "PREMIUM"
    PREMIUM_XL = "PREMIUM_XL"
    PREMIUM_PLUS = "PREMIUM_PLUS"


class TimeOfDay(str, Enum):
    MORNING = "MORNING"
    AFTERNOON = "AFTERNOON"
    EVENING = "EVENING"
    NIGHT = "NIGHT"


class ShowSort(str, Enum):
    NAME = "NAME"
    EARLIEST = "EARLIEST"


class PriceBandOut(BaseModel):
    min: int = Field(ge=0)
    max: int | None = None


class AvailableFiltersOut(BaseModel):
    priceBands: list[PriceBandOut]
    formats: list[str]


class AppliedFiltersOut(BaseModel):
    priceMin: int | None = None
    priceMax: int | None = None
    timeOfDay: list[TimeOfDay] = Field(default_factory=list)
    format: list[str] = Field(default_factory=list)
    cancellable: bool | None = None
    wheelchair: bool | None = None
    sort: ShowSort = ShowSort.NAME


class MovieShowsOut(BaseModel):
    movie: ShowMovieOut
    dates: list[ShowDateOut]
    cinemas: list[ShowCinemaOut]
    appliedFilters: AppliedFiltersOut
    availableFilters: AvailableFiltersOut


class SeatOut(BaseModel):
    id: int
    number: int
    kind: SeatKind
    gapBefore: bool
    bestseller: bool
    status: ShowSeatStatus
    heldByMe: bool = False


class SeatRowOut(BaseModel):
    row: str
    seats: list[SeatOut]


class SeatCategoryLayoutOut(BaseModel):
    id: int
    name: SeatCategoryName
    label: str
    price: float
    availability: Availability
    rows: list[SeatRowOut]


class SeatLayoutShowOut(BaseModel):
    id: int
    startsAt: datetime
    format: str
    movieId: int
    movieTitle: str
    language: str
    cinemaId: int
    cinemaName: str


class SeatLayoutOut(BaseModel):
    show: SeatLayoutShowOut
    maxSeatsPerBooking: int = 10
    categories: list[SeatCategoryLayoutOut]


class OtpRequestIn(BaseModel):
    countryCode: str
    phone: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{10}$")]


class OtpRequestOut(BaseModel):
    challengeId: str
    resendAfterSeconds: int = 30
    expiresInSeconds: int = 300


class OtpVerifyIn(BaseModel):
    challengeId: str
    code: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{6}$")]


class UserOut(BaseModel):
    id: int
    phone: str
    name: str | None = None


class UpdateNameRequest(BaseModel):
    """A name to store, trimmed before the 1–80 check sees it."""

    name: str

    @model_validator(mode="before")
    @classmethod
    def _trim_name(cls, data: object) -> object:
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            return {**data, "name": data["name"].strip()}
        return data


class OtpVerifyOut(BaseModel):
    token: str
    expiresInSeconds: int = 604800
    user: UserOut


class SeatHoldIn(BaseModel):
    seatIds: list[int]


class HeldSeatOut(BaseModel):
    id: int
    label: str


class SeatHoldOut(BaseModel):
    holdId: str
    heldUntil: datetime
    seats: list[HeldSeatOut]
    total: float


class BookingStatus(str, Enum):
    PENDING_PAYMENT = "PENDING_PAYMENT"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class BookingCreate(BaseModel):
    holdId: NonBlank
    promoCode: str | None = None


class BookingQuoteIn(BaseModel):
    holdId: NonBlank
    promoCode: NonBlank


class BookingQuoteOut(BaseModel):
    originalAmount: float
    discount: float
    total: float
    promoCode: str


class TransferStatus(str, Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class BookingTransferIn(BaseModel):
    toUserId: int = Field(gt=0)


class BookingTransferOut(BaseModel):
    transferId: int
    transferCode: str
    bookingId: int
    fromUserId: int
    toUserId: int
    status: TransferStatus
    fee: float
    createdAt: datetime
    expiresAt: datetime


class TransferDetailOut(BaseModel):
    transfer: BookingTransferOut
    bookingId: int
    movieId: int
    movieTitle: str
    cinemaName: str
    showStartsAt: datetime
    seats: list[HeldSeatOut]
    reviewEligible: bool


class BookingOut(BaseModel):
    bookingId: int
    showId: int
    holdId: str
    status: BookingStatus
    seats: list[HeldSeatOut]
    total: float
    paymentId: int | None = None
    createdAt: datetime
    refundedAt: datetime | None = None
    promoCode: str | None = None
    originalAmount: float | None = None
    discount: float | None = None
    transfer: BookingTransferOut | None = None


class BookingCancelOut(BaseModel):
    id: int
    status: BookingStatus
    refundedAt: datetime | None = None


class InternalBookingOut(BaseModel):
    """A booking's current owner, as the ratings service reads it before a review."""

    bookingId: int
    userId: int
    status: BookingStatus
    showId: int
    showStartsAt: datetime
    movieId: int


class MemberPaymentStatus(str, Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    REFUNDED = "REFUNDED"


class GroupCreate(BaseModel):
    holdId: NonBlank


class GroupJoin(BaseModel):
    inviteCode: NonBlank


class GroupMemberOut(BaseModel):
    userId: int
    seatIds: list[int]
    seats: list[HeldSeatOut]
    shareAmount: float
    paymentStatus: MemberPaymentStatus


class GroupOut(BaseModel):
    groupId: int
    inviteCode: str
    status: BookingStatus
    showId: int
    ownerId: int
    seatIds: list[int]
    expiresAt: datetime
    total: float
    members: list[GroupMemberOut]


class ReviewsSummaryOut(BaseModel):
    average: float
    count: int
    distribution: dict[str, int]


class ReviewPhotoOut(BaseModel):
    """A review's photo: its asset id and the digest storage was checked against."""

    assetId: int
    sha256: str
    verified: bool


class ReviewOut(BaseModel):
    id: int
    movieId: int
    userId: int
    bookingId: int | None = None
    groupId: int | None = None
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=1000)
    verified: bool = False
    status: str = "ACTIVE"
    createdAt: datetime
    photo: ReviewPhotoOut | None = None


class ReviewsPageOut(BaseModel):
    summary: ReviewsSummaryOut
    reviews: list[ReviewOut]
    page: int
    pageSize: int
    total: int


class ReviewSubmitIn(BaseModel):
    """A review against exactly one source: the caller's booking or their group share."""

    bookingId: int | None = Field(default=None, gt=0)
    groupId: int | None = Field(default=None, gt=0)
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "ReviewSubmitIn":
        if (self.bookingId is None) == (self.groupId is None):
            raise ValueError("exactly one of bookingId or groupId is required")
        return self


class ReviewEligibilityOut(BaseModel):
    eligible: bool
    movieId: int
    showStartsAt: datetime


class EventPublishIn(BaseModel):
    """A caller-supplied domain event; the publisher builds the envelope around it."""

    type: NonBlank
    version: int = Field(default=1, ge=1)
    payload: dict[str, object]
    key: NonBlank | None = None


class EventPublishOut(BaseModel):
    eventId: str
    topic: str
    partition: int
    offset: int


class RabbitPublishOut(BaseModel):
    eventId: str
    exchange: str
    routingKey: str


class DeliveryStatusOut(BaseModel):
    """One event's delivery state, as `GET /api/events/deliveries/{eventId}` reads it."""

    status: str
    attempts: int
    lastError: str | None
    nextRetryAt: datetime | None


class KafkaConsumerStatusOut(BaseModel):
    """This process's Kafka consumer, as `GET /api/events/kafka/status` reports it."""

    running: bool
    topic: str
    group: str
    processedCount: int
    lastEventAt: datetime | None
    lastError: str | None


class EventBusPublishIn(BaseModel):
    """A caller-supplied event; the bus generates its `id` and `occurredAt`."""

    type: NonBlank
    version: int = Field(default=1, ge=1)
    payload: dict[str, object]
    metadata: dict[str, object] = Field(default_factory=dict)


class EventBusHandlerErrorOut(BaseModel):
    """One handler that raised while a published event was being dispatched."""

    eventType: str
    handler: str
    error: str


class EventBusPublishOut(BaseModel):
    """The bus publish answer: the envelope's id and the handlers that failed."""

    id: str
    handlerErrors: list[EventBusHandlerErrorOut] = Field(default_factory=list)


class EventBusSubscriptionOut(BaseModel):
    """One in-process subscription, as `GET /api/events/subscriptions` lists it."""

    eventType: str
    handler: str


class EventSubscriptionsOut(BaseModel):
    """The transport the bus publishes on, plus every registration it holds."""

    transport: EventBusTransport
    subscriptions: list[EventBusSubscriptionOut]


class EventMetricsBucketOut(BaseModel):
    """One (event type, transport) pair's five counters, as `/metrics` reports them."""

    type: str
    transport: str
    published: int
    consumed: int
    failed: int
    retried: int
    deadLettered: int


class EventMetricsOut(BaseModel):
    """The process's event counters plus when any of them last moved."""

    counters: list[EventMetricsBucketOut] = Field(default_factory=list)
    lastEventAt: datetime | None = None


class EventHealthOut(BaseModel):
    """The three components `/health` reports, each "up" or "down"."""

    fastapi: str
    kafka: str
    rabbitmq: str


class EventLogEntryOut(BaseModel):
    """One recent delivery summary: the ticket's five fields, so no payload is exposed."""

    eventId: str
    type: str
    transport: str
    status: str
    timestamp: datetime


class EventLogOut(BaseModel):
    """The recent delivery summaries, newest first, and the limit that produced them."""

    entries: list[EventLogEntryOut] = Field(default_factory=list)
    limit: int
