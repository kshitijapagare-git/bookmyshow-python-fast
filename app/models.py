from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
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
    screens: Mapped[list["Screen"]] = relationship(
        back_populates="cinema", cascade="all, delete-orphan"
    )


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
    certificate: Mapped[str] = mapped_column(String, nullable=False)
    release_date: Mapped[date] = mapped_column(Date, nullable=False)
    backdrop_url: Mapped[str] = mapped_column(String, nullable=False)
    trailer_url: Mapped[str | None] = mapped_column(String, nullable=True)
    interested_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Screen(Base):
    __tablename__ = "screens"
    __table_args__ = (UniqueConstraint("cinema_id", "name", name="uq_screens_cinema_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cinema_id: Mapped[int] = mapped_column(ForeignKey("cinemas.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)

    cinema: Mapped[Cinema] = relationship(back_populates="screens")
    shows: Mapped[list["Show"]] = relationship(
        back_populates="screen", cascade="all, delete-orphan"
    )
    seat_categories: Mapped[list["SeatCategory"]] = relationship(
        back_populates="screen", cascade="all, delete-orphan"
    )
    seats: Mapped[list["Seat"]] = relationship(
        back_populates="screen", cascade="all, delete-orphan"
    )


class SeatCategory(Base):
    __tablename__ = "seat_categories"
    __table_args__ = (
        UniqueConstraint("screen_id", "name", name="uq_seat_categories_screen_name"),
        CheckConstraint(
            "name IN ('NORMAL','EXECUTIVE','PREMIUM','PREMIUM_XL','PREMIUM_PLUS')",
            name="ck_seat_categories_name",
        ),
        CheckConstraint("price >= 1 AND price <= 10000", name="ck_seat_categories_price"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    screen_id: Mapped[int] = mapped_column(ForeignKey("screens.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    screen: Mapped[Screen] = relationship(back_populates="seat_categories")


class Seat(Base):
    __tablename__ = "seats"
    __table_args__ = (
        UniqueConstraint("screen_id", "row", "number", name="uq_seats_screen_row_number"),
        CheckConstraint(
            "length(row) = 1 AND row >= 'A' AND row <= 'Z'", name="ck_seats_row"
        ),
        CheckConstraint("number >= 1 AND number <= 50", name="ck_seats_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    screen_id: Mapped[int] = mapped_column(ForeignKey("screens.id"), nullable=False)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("seat_categories.id"), nullable=False
    )
    row: Mapped[str] = mapped_column(String(1), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="STANDARD")
    gap_before: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    bestseller: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    category: Mapped[SeatCategory] = relationship()
    screen: Mapped[Screen] = relationship(back_populates="seats")


class Show(Base):
    __tablename__ = "shows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    screen_id: Mapped[int] = mapped_column(ForeignKey("screens.id"), nullable=False)
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id"), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    format: Mapped[str] = mapped_column(String, nullable=False)
    language: Mapped[str] = mapped_column(String, nullable=False)

    screen: Mapped[Screen] = relationship(back_populates="shows")
    movie: Mapped[Movie] = relationship()
    show_seats: Mapped[list["ShowSeat"]] = relationship(
        back_populates="show", cascade="all, delete-orphan"
    )


class ShowSeat(Base):
    __tablename__ = "show_seats"
    __table_args__ = (
        UniqueConstraint("show_id", "seat_id", name="uq_show_seats_show_seat"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id"), nullable=False)
    seat_id: Mapped[int] = mapped_column(ForeignKey("seats.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="AVAILABLE")
    held_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    held_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    hold_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    show: Mapped[Show] = relationship(back_populates="show_seats")

    __mapper_args__ = {"version_id_col": version}


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("name IS NULL OR length(name) <= 80", name="ck_users_name_length"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name: Mapped[str | None] = mapped_column(String(80), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def rename(self, name: str) -> None:
        """Store a trimmed name; the caller owns the transaction."""
        self.name = name.strip()


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id"), nullable=False)
    hold_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    seat_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    promo_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    original_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    discount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="PENDING_PAYMENT"
    )
    payment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    refunded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id"), nullable=False)
    hold_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    seat_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="PENDING_PAYMENT"
    )
    invite_code: Mapped[str] = mapped_column(String(6), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    members: Mapped[list["GroupMember"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class BookingTransfer(Base):
    __tablename__ = "booking_transfers"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','ACCEPTED','CANCELLED','EXPIRED')",
            name="ck_booking_transfers_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id"), nullable=False, index=True
    )
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    code: Mapped[str] = mapped_column(String(6), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    fee: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00")
    )
    payments_transfer_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_group_members_group_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    seat_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    share_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    payment_status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    payment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    group: Mapped[Group] = relationship(back_populates="members")


class OtpChallenge(Base):
    __tablename__ = "otp_challenges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    phone: Mapped[str] = mapped_column(String, nullable=False)
    code_hash: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class OtpRequest(Base):
    __tablename__ = "otp_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String, nullable=False, index=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class EventDelivery(Base):
    """One published event's delivery state, keyed by the eventId every attempt carried."""

    __tablename__ = "event_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','RETRYING','DELIVERED','DEAD_LETTERED')",
            name="ck_event_deliveries_status",
        ),
        CheckConstraint(
            "transport IN ('KAFKA','RABBIT')",
            name="ck_event_deliveries_transport",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(
        String(36), nullable=False, unique=True, index=True
    )
    transport: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class MediaAsset(Base):
    """One uploaded file for a movie's media kind, keyed by its content hash."""

    __tablename__ = "media_assets"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('POSTER','BACKDROP','TRAILER')",
            name="ck_media_assets_kind",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    movie_id: Mapped[int] = mapped_column(
        ForeignKey("movies.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    storage_key: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    content_type: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
