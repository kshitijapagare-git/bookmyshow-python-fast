from datetime import date
from enum import Enum
from typing import Annotated, Literal

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
