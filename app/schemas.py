from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StringConstraints, model_validator

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


class TransferStatus(str, Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class TransferStateOut(BaseModel):
    transferId: int
    status: TransferStatus
    toUserId: int | None = None
    expiresAt: datetime


class BookingOut(BaseModel):
    bookingId: int
    showId: int
    userId: int
    status: str
    total: float
    createdAt: datetime
    transfer: TransferStateOut | None = None


class TransferCreate(BaseModel):
    toUserId: int | None = Field(default=None, gt=0)
    toPhone: str | None = Field(default=None, pattern=r"^\+?\d{10,15}$")

    @model_validator(mode="after")
    def _check_recipient(self) -> "TransferCreate":
        if (self.toUserId is None) == (self.toPhone is None):
            raise ValueError("Exactly one of toUserId or toPhone is required")
        return self


class TransferOut(BaseModel):
    transferCode: str
    status: TransferStatus
    expiresAt: datetime


class TransferDetailOut(TransferOut):
    movieTitle: str
    cinemaName: str
    startsAt: datetime
