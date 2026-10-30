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


class MovieOut(BaseModel):
    id: int
    title: str
    genres: str
    posterUrl: str | None = None
    rating: float | None = None
    votes: int | None = None
    likes: int | None = None
    promoted: bool = False
