import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .models import Seat, SeatCategory, Show, ShowSeat, User
from .schemas import (
    Availability,
    SeatCategoryLayoutOut,
    SeatCategoryName,
    SeatKind,
    SeatLayoutOut,
    SeatLayoutShowOut,
    SeatOut,
    SeatRowOut,
    ShowSeatStatus,
)
from .timeutil import ist_local

MAX_SEATS_PER_BOOKING = 10
FAST_FILLING_RATIO = 0.5
HOLD_MINUTES = 10
HOLD_SWEEP_INTERVAL_SECONDS = 60
SEATS_UNAVAILABLE_DETAIL = "Those seats were just taken"
SEAT_COUNT_DETAIL = "Between 1 and 10 seats per booking"


class SeatsUnavailableError(Exception):
    """Raised when at least one requested seat is already held or sold."""

    def __init__(
        self, seat_ids: list[int], detail: str = SEATS_UNAVAILABLE_DETAIL
    ) -> None:
        super().__init__(detail)
        self.seat_ids = seat_ids
        self.detail = detail


@dataclass
class HoldResult:
    hold_id: str
    held_until: datetime
    seats: list[tuple[int, str]]
    total: Decimal


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def seat_label(row: str, number: int) -> str:
    return f"{row}{number}"


def category_label(name: str) -> str:
    return name.replace("_", " ")


def effective_status(show_seat: ShowSeat, now: datetime) -> ShowSeatStatus:
    """A hold whose held_until has passed no longer reserves the seat."""
    status = ShowSeatStatus(show_seat.status)
    if (
        status is ShowSeatStatus.HELD
        and show_seat.held_until is not None
        and _as_utc(show_seat.held_until) <= _as_utc(now)
    ):
        return ShowSeatStatus.AVAILABLE
    return status


def category_availability(statuses: list[ShowSeatStatus]) -> Availability:
    total = len(statuses)
    if total == 0:
        return Availability.AVAILABLE
    held_or_sold = sum(
        1 for status in statuses if status is not ShowSeatStatus.AVAILABLE
    )
    if held_or_sold / total >= FAST_FILLING_RATIO:
        return Availability.FAST_FILLING
    return Availability.AVAILABLE


def build_seat_layout(db: Session, show: Show, viewer: User | None) -> SeatLayoutOut:
    now = datetime.now(timezone.utc)

    seat_rows = db.execute(
        select(Seat, ShowSeat)
        .join(ShowSeat, ShowSeat.seat_id == Seat.id)
        .where(Seat.screen_id == show.screen_id, ShowSeat.show_id == show.id)
        .order_by(Seat.row, Seat.number.desc())
    ).all()

    categories = list(
        db.scalars(
            select(SeatCategory)
            .where(SeatCategory.screen_id == show.screen_id)
            .order_by(SeatCategory.price.desc(), SeatCategory.id)
        )
    )

    seats_by_category: dict[int, dict[str, list[SeatOut]]] = {}
    statuses_by_category: dict[int, list[ShowSeatStatus]] = {}
    for seat, show_seat in seat_rows:
        status = effective_status(show_seat, now)
        seats_by_category.setdefault(seat.category_id, {}).setdefault(
            seat.row, []
        ).append(
            SeatOut(
                id=show_seat.id,
                number=seat.number,
                kind=SeatKind(seat.kind),
                gapBefore=seat.gap_before,
                bestseller=seat.bestseller,
                status=status,
                heldByMe=viewer is not None and show_seat.held_by == viewer.id,
            )
        )
        statuses_by_category.setdefault(seat.category_id, []).append(status)

    layout: list[SeatCategoryLayoutOut] = []
    for category in categories:
        rows_by_letter = seats_by_category.get(category.id, {})
        if not rows_by_letter:
            continue
        layout.append(
            SeatCategoryLayoutOut(
                id=category.id,
                name=SeatCategoryName(category.name),
                label=category_label(category.name),
                price=float(category.price),
                availability=category_availability(statuses_by_category[category.id]),
                rows=[
                    SeatRowOut(row=row, seats=seats)
                    for row, seats in sorted(rows_by_letter.items())
                ],
            )
        )

    movie = show.movie
    cinema = show.screen.cinema
    return SeatLayoutOut(
        show=SeatLayoutShowOut(
            id=show.id,
            startsAt=ist_local(show.starts_at),
            format=show.format,
            movieId=movie.id,
            movieTitle=movie.title,
            language=show.language,
            cinemaId=cinema.id,
            cinemaName=cinema.name,
        ),
        maxSeatsPerBooking=MAX_SEATS_PER_BOOKING,
        categories=layout,
    )


def _conflicting_seat_ids(
    db: Session, show_id: int, seat_ids: list[int], moment: datetime
) -> list[int]:
    rows = db.scalars(
        select(ShowSeat.id).where(
            ShowSeat.show_id == show_id,
            ShowSeat.id.in_(seat_ids),
            ShowSeat.status != ShowSeatStatus.AVAILABLE.value,
            or_(ShowSeat.held_until.is_(None), ShowSeat.held_until >= moment),
        )
    )
    return sorted(rows)


def acquire_hold(
    *,
    db: Session,
    show: Show,
    seat_ids: list[int],
    user: User,
    now: datetime | None = None,
) -> HoldResult:
    """Claim the requested ShowSeats for `user` in one transaction, or fail with nothing held."""
    requested = list(seat_ids)
    if (
        not 1 <= len(requested) <= MAX_SEATS_PER_BOOKING
        or len(set(requested)) != len(requested)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=SEAT_COUNT_DETAIL
        )

    moment = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    held_until = moment + timedelta(minutes=HOLD_MINUTES)

    locked = {
        show_seat.id: show_seat
        for show_seat in db.scalars(
            select(ShowSeat)
            .where(ShowSeat.show_id == show.id, ShowSeat.id.in_(requested))
            .with_for_update()
        )
    }
    for seat_id in requested:
        if seat_id not in locked:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Seat not found: {seat_id}",
            )

    hold_id = str(uuid.uuid4())

    # One conditional UPDATE is the real guard: SQLite ignores FOR UPDATE, so the claim
    # only succeeds when every requested row is still free at the moment it runs.
    claimed = db.execute(
        update(ShowSeat)
        .where(
            ShowSeat.show_id == show.id,
            ShowSeat.id.in_(requested),
            or_(
                ShowSeat.status == ShowSeatStatus.AVAILABLE.value,
                ShowSeat.held_until < moment,
            ),
        )
        .values(
            status=ShowSeatStatus.HELD.value,
            held_by=user.id,
            held_until=held_until,
            hold_id=hold_id,
            version=ShowSeat.version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != len(requested):
        db.rollback()
        raise SeatsUnavailableError(
            _conflicting_seat_ids(db, show.id, requested, moment)
        )

    db.commit()

    info = {
        seat_id: (seat_label(row, number), Decimal(price))
        for seat_id, row, number, price in db.execute(
            select(ShowSeat.id, Seat.row, Seat.number, SeatCategory.price)
            .join(Seat, ShowSeat.seat_id == Seat.id)
            .join(SeatCategory, Seat.category_id == SeatCategory.id)
            .where(ShowSeat.id.in_(requested))
        ).all()
    }
    seats = [(seat_id, info[seat_id][0]) for seat_id in requested]
    total = sum((info[seat_id][1] for seat_id in requested), Decimal("0"))
    return HoldResult(
        hold_id=hold_id, held_until=held_until, seats=seats, total=total
    )


def release_expired_holds(db: Session, *, now: datetime | None = None) -> int:
    """Clear holds whose ten minutes are over and report how many seats came back."""
    moment = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    released = db.execute(
        update(ShowSeat)
        .where(
            ShowSeat.status == ShowSeatStatus.HELD.value,
            ShowSeat.held_until.is_not(None),
            ShowSeat.held_until < moment,
        )
        .values(
            status=ShowSeatStatus.AVAILABLE.value,
            held_by=None,
            held_until=None,
            hold_id=None,
            version=ShowSeat.version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return released.rowcount or 0


def release_hold(*, db: Session, hold_id: str, user: User) -> None:
    """Give a hold's seats back, but only for the moviegoer who made the hold."""
    rows = list(
        db.scalars(
            select(ShowSeat).where(ShowSeat.hold_id == hold_id).with_for_update()
        )
    )
    moment = datetime.now(timezone.utc)
    live = [
        row
        for row in rows
        if row.held_until is not None and _as_utc(row.held_until) > moment
    ]
    if not live:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Hold not found: {hold_id}",
        )
    if any(row.held_by != user.id for row in live):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This hold belongs to another user",
        )

    for row in live:
        row.status = ShowSeatStatus.AVAILABLE.value
        row.held_by = None
        row.held_until = None
        row.hold_id = None
    db.commit()
