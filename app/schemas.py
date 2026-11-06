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


class BookingCancelOut(BaseModel):
    id: int
    status: BookingStatus
    refundedAt: datetime | None = None


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


class ReviewOut(BaseModel):
    id: int
    movieId: int
    userId: int
    bookingId: int | None = None
    groupId: int | None = None
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=1000)
    verified: bool = False
    createdAt: datetime


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
