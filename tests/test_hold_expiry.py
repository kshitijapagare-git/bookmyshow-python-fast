import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ShowSeat, User
from app.schemas import ShowSeatStatus
from app.seating import HOLD_SWEEP_INTERVAL_SECONDS, release_expired_holds
from app.security import issue_token


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _create_user(phone: str) -> int:
    with SessionLocal() as db:
        user = User(phone=phone, name=None, created_at=datetime.now(timezone.utc))
        db.add(user)
        db.commit()
        return user.id


def _auth(user_id: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(user_id=user_id)}"}


def _available_seats(count: int = 1) -> tuple[int, list[int]]:
    """A seeded show with at least `count` free ShowSeats, plus those seat ids."""
    with SessionLocal() as db:
        rows = db.execute(
            select(ShowSeat.show_id, ShowSeat.id)
            .where(ShowSeat.status == ShowSeatStatus.AVAILABLE.value)
            .order_by(ShowSeat.id)
        ).all()

    by_show: dict[int, list[int]] = {}
    for show_id, seat_id in rows:
        by_show.setdefault(show_id, []).append(seat_id)
    for show_id, seat_ids in by_show.items():
        if len(seat_ids) >= count:
            return show_id, seat_ids[:count]
    raise AssertionError("no seeded show has enough free seats")


def _solid_sold_seat_id() -> int:
    with SessionLocal() as db:
        seat_id = db.scalar(
            select(ShowSeat.id)
            .where(ShowSeat.status == ShowSeatStatus.SOLD.value)
            .order_by(ShowSeat.id)
            .limit(1)
        )
    assert seat_id is not None
    return seat_id


def _book_hold(seat_id: int, held_by: int, *, held_until: datetime) -> str:
    hold_id = str(uuid.uuid4())
    with SessionLocal() as db:
        row = db.get(ShowSeat, seat_id)
        assert row is not None
        row.status = ShowSeatStatus.HELD.value
        row.held_by = held_by
        row.held_until = held_until
        row.hold_id = hold_id
        db.commit()
    return hold_id


def _seat_status(layout, seat_id: int) -> str:
    for category in layout["categories"]:
        for row in category["rows"]:
            for seat in row["seats"]:
                if seat["id"] == seat_id:
                    return seat["status"]
    raise AssertionError(f"seat {seat_id} missing from the layout")


def _expired_hold_moment() -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=1)


def test_expired_hold_is_released(client) -> None:
    assert HOLD_SWEEP_INTERVAL_SECONDS == 60
    user_id = _create_user("+919000000401")
    show_id, seat_ids = _available_seats(1)
    seat_id = seat_ids[0]
    _book_hold(seat_id, user_id, held_until=_expired_hold_moment())

    with SessionLocal() as db:
        released = release_expired_holds(db)

    assert released >= 1
    with SessionLocal() as db:
        row = db.get(ShowSeat, seat_id)
        assert row is not None
        assert row.status == ShowSeatStatus.AVAILABLE.value
        assert row.held_by is None
        assert row.held_until is None
        assert row.hold_id is None

    layout = client.get(f"/api/shows/{show_id}/seats")

    assert layout.status_code == 200
    assert _seat_status(layout.json(), seat_id) == "AVAILABLE"


def test_sweep_leaves_live_holds_and_sold_seats_alone(client) -> None:
    user_id = _create_user("+919000000402")
    _, seat_ids = _available_seats(1)
    seat_id = seat_ids[0]
    held_until = datetime.now(timezone.utc) + timedelta(minutes=9)
    hold_id = _book_hold(seat_id, user_id, held_until=held_until)

    sold_seat_id = _solid_sold_seat_id()
    with SessionLocal() as db:
        sold = db.get(ShowSeat, sold_seat_id)
        assert sold is not None
        sold_snapshot = (sold.status, sold.held_by, sold.held_until, sold.hold_id)

    with SessionLocal() as db:
        released = release_expired_holds(db)

    assert released == 0

    with SessionLocal() as db:
        live = db.get(ShowSeat, seat_id)
        assert live is not None
        assert live.status == ShowSeatStatus.HELD.value
        assert live.held_by == user_id
        assert live.hold_id == hold_id
        assert live.held_until is not None
        assert _as_utc(live.held_until) == _as_utc(held_until)

        still_sold = db.get(ShowSeat, sold_seat_id)
        assert still_sold is not None
        assert still_sold.status == ShowSeatStatus.SOLD.value
        assert (
            still_sold.status,
            still_sold.held_by,
            still_sold.held_until,
            still_sold.hold_id,
        ) == sold_snapshot


def test_expired_seat_can_be_held_by_another_user(client) -> None:
    first_user = _create_user("+919000000403")
    second_user = _create_user("+919000000404")
    show_id, seat_ids = _available_seats(1)
    seat_id = seat_ids[0]
    expired_hold_id = _book_hold(seat_id, first_user, held_until=_expired_hold_moment())

    response = client.post(
        f"/api/shows/{show_id}/holds",
        json={"seatIds": [seat_id]},
        headers=_auth(second_user),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["holdId"] != expired_hold_id

    with SessionLocal() as db:
        row = db.get(ShowSeat, seat_id)
        assert row is not None
        assert row.status == ShowSeatStatus.HELD.value
        assert row.held_by == second_user
        assert row.hold_id == body["holdId"]
        assert row.held_until is not None
        assert _as_utc(row.held_until) > datetime.now(timezone.utc) + timedelta(
            minutes=8
        )
