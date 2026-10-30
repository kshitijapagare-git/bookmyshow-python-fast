from datetime import date, datetime
from enum import Enum
from typing import Annotated

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StringConstraints

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


class CinemaAmenity(str, Enum):
    M_TICKET = "M_TICKET"
    FOOD = "FOOD"
    PARKING = "PARKING"
    WHEELCHAIR = "WHEELCHAIR"


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
    chainLogoUrl: str | None = None
    cancellable: bool = False
    amenities: list[CinemaAmenity] = Field(default_factory=list)


class Certificate(str, Enum):
    U = "U"
    UA = "UA"
    A = "A"
    S = "S"


class MovieFormat(str, Enum):
    TWO_D = "2D"
    THREE_D = "3D"
    IMAX = "IMAX"
    FOUR_D = "4DX"


class MovieOut(BaseModel):
    id: int
    title: str
    slug: str
    genres: str
    posterUrl: str | None = None
    rating: float | None = None
    votes: int | None = None
    likes: int | None = None
    promoted: bool = False


class MovieDetailOut(MovieOut):
    synopsis: str = Field(max_length=1000)
    languages: list[NonBlank] = Field(min_length=1)
    formats: list[MovieFormat] = Field(min_length=1)
    durationMinutes: int = Field(ge=30, le=300)
    certificate: Certificate
    releaseDate: date
    backdropUrl: str
    trailerUrl: str | None = None
    interestedCount: int = 0


class Availability(str, Enum):
    AVAILABLE = "AVAILABLE"
    FAST_FILLING = "FAST_FILLING"


class ShowOut(BaseModel):
    id: int
    startsAt: datetime
    format: str
    language: str
    availability: Availability
    lateNight: bool
    earlyMorning: bool


class CinemaShowsOut(BaseModel):
    id: int
    name: str
    address: str
    chainLogoUrl: str | None = None
    cancellable: bool = False
    amenities: list[CinemaAmenity] = Field(default_factory=list)
    shows: list[ShowOut]


class ShowsDateOut(BaseModel):
    date: date
    hasShows: bool


class ShowMovieOut(BaseModel):
    id: int
    title: str
    durationMinutes: int
    certificate: Certificate
    genres: str


class MovieShowsOut(BaseModel):
    movie: ShowMovieOut
    dates: list[ShowsDateOut]
    cinemas: list[CinemaShowsOut]
