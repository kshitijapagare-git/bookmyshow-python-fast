from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class City(Base):
    __tablename__ = "cities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)

    cinemas: Mapped[list["Cinema"]] = relationship(back_populates="city")


class Cinema(Base):
    __tablename__ = "cinemas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    address: Mapped[str] = mapped_column(String, nullable=False)
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id"), nullable=False)
    chain_logo_url: Mapped[str | None] = mapped_column(String, nullable=True)
    cancellable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    amenities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    city: Mapped[City] = relationship(back_populates="cinemas")
    screens: Mapped[list["Screen"]] = relationship(back_populates="cinema")


class Movie(Base):
    __tablename__ = "movies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    genres: Mapped[str] = mapped_column(String, nullable=False)
    poster_url: Mapped[str | None] = mapped_column(String, nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    votes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    promoted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    synopsis: Mapped[str] = mapped_column(String(1000), nullable=False)
    languages: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    formats: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    certificate: Mapped[str] = mapped_column(String(2), nullable=False)
    release_date: Mapped[date] = mapped_column(Date, nullable=False)
    backdrop_url: Mapped[str] = mapped_column(String, nullable=False)
    trailer_url: Mapped[str | None] = mapped_column(String, nullable=True)
    interested_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Screen(Base):
    __tablename__ = "screens"
    __table_args__ = (
        UniqueConstraint("cinema_id", "name", name="uq_screens_cinema_id_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    cinema_id: Mapped[int] = mapped_column(ForeignKey("cinemas.id"), nullable=False)

    cinema: Mapped[Cinema] = relationship(back_populates="screens")
    shows: Mapped[list["Show"]] = relationship(back_populates="screen")


class Show(Base):
    __tablename__ = "shows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    screen_id: Mapped[int] = mapped_column(ForeignKey("screens.id"), nullable=False)
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id"), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    format: Mapped[str] = mapped_column(String, nullable=False)
    language: Mapped[str] = mapped_column(String, nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    seats_sold: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    screen: Mapped[Screen] = relationship(back_populates="shows")
