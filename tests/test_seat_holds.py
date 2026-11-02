import threading
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as OrmSession

from app.db import Base, SessionLocal
from app.models import Seat, SeatCategory, Show, ShowSeat, User
from app.schemas import ShowSeatStatus
from app.seating import (
    HOLD_MINUTES,
    SEAT_COUNT_DETAIL,
    SEATS_UNAVAILABLE_DETAIL,
    SeatsUnavailableError,
    acquire_hold,
)
from app.security import issue_token
from seed import seed_data

COUNT_DETAIL = "Between 1 and 10 seats per booking"


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


def _seat_ids_by_status() -> dict[int, dict[str, list[int]]]:
    with SessionLocal() as db:
        rows = db.execute(
            select(ShowSeat.show_id, ShowSeat.status, ShowSeat.id).order_by(
                ShowSeat.show_id, ShowSeat.id
            )
        ).all()

    seats: dict[int, dict[str, list[int]]] = {}
    for show_id, seat_status, seat_id in rows:
        seats.setdefault(show_id, {}).setdefault(seat_status, []).append(seat_id)
    return seats


def _pick_show(available: int, sold: int = 0) -> tuple[int, list[int], list[int]]:
    """A seeded show with at least `available` free and `sold` sold seats."""
    for show_id, by_status in _seat_ids_by_status().items():
        free = by_status.get(ShowSeatStatus.AVAILABLE.value, [])
        taken = by_status.get(ShowSeatStatus.SOLD.value, [])
        if len(free) >= available and len(taken) >= sold:
            return show_id, free[:available], taken[:sold]
    raise AssertionError("no seeded show has the requested seat mix")


def _seat_facts(seat_ids: list[int]) -> dict[int, tuple[str, float]]:
    with SessionLocal() as db:
        rows = db.execute(
            select(ShowSeat.id, Seat.row, Seat.number, SeatCategory.price)
            .join(Seat, ShowSeat.seat_id == Seat.id)
            .join(SeatCategory, Seat.category_id == SeatCategory.id)
            .where(ShowSeat.id.in_(seat_ids))
        ).all()
    return {
        seat_id: (f"{row}{number}", float(price))
        for seat_id, row, number, price in rows
    }


def test_hold_returns_hold_id_seats_and_total(client) -> None:
    user_id = _create_user("+919000000101")
    show_id, seat_ids, _ = _pick_show(2)
    before = datetime.now(timezone.utc)

    response = client.post(
        f"/api/shows/{show_id}/holds",
        json={"seatIds": seat_ids},
        headers=_auth(user_id),
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"holdId", "heldUntil", "seats", "total"}
    assert len(body["holdId"]) == 36
    assert uuid.UUID(body["holdId"])

    held_until = datetime.fromisoformat(body["heldUntil"])
    assert held_until.utcoffset() == timedelta(hours=5, minutes=30)
    window = _as_utc(held_until) - before
    assert timedelta(minutes=HOLD_MINUTES - 1) <= window <= timedelta(
        minutes=HOLD_MINUTES + 1
    )

    facts = _seat_facts(seat_ids)
    assert [seat["id"] for seat in body["seats"]] == seat_ids
    for seat in body["seats"]:
        assert set(seat) == {"id", "label"}
        assert seat["label"] == facts[seat["id"]][0]
    assert body["total"] == sum(facts[seat_id][1] for seat_id in seat_ids)

    with SessionLocal() as db:
        for seat_id in seat_ids:
            row = db.get(ShowSeat, seat_id)
            assert row is not None
            assert row.status == ShowSeatStatus.HELD.value
            assert row.held_by == user_id
            assert row.hold_id == body["holdId"]
            assert _as_utc(row.held_until) == _as_utc(held_until)
            assert row.version == 2


def test_hold_requires_a_token(client) -> None:
    show_id, seat_ids, _ = _pick_show(1)
    url = f"/api/shows/{show_id}/holds"

    missing = client.post(url, json={"seatIds": seat_ids})

    assert missing.status_code == 401
    body = missing.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Unauthorized"
    assert body["status"] == 401
    assert body["detail"] == "Unauthorized"
    assert body["instance"] == url

    garbage = client.post(
        url, json={"seatIds": seat_ids}, headers={"Authorization": "Bearer not-a-jwt"}
    )

    assert garbage.status_code == 401
    assert garbage.json()["title"] == "Unauthorized"

    with SessionLocal() as db:
        for seat_id in seat_ids:
            row = db.get(ShowSeat, seat_id)
            assert row is not None
            assert row.status == ShowSeatStatus.AVAILABLE.value
            assert row.hold_id is None


def test_hold_rejects_out_of_range_counts(client) -> None:
    user_id = _create_user("+919000000102")
    show_id, seat_ids, _ = _pick_show(11)
    url = f"/api/shows/{show_id}/holds"

    for requested in ([], seat_ids):
        response = client.post(
            url, json={"seatIds": requested}, headers=_auth(user_id)
        )

        assert response.status_code == 400, requested
        body = response.json()
        assert body["type"] == "about:blank"
        assert body["title"] == "Bad Request"
        assert body["status"] == 400
        assert body["detail"] == COUNT_DETAIL
        assert body["detail"] == SEAT_COUNT_DETAIL
        assert body["instance"] == url

    with SessionLocal() as db:
        for seat_id in seat_ids:
            row = db.get(ShowSeat, seat_id)
            assert row is not None
            assert row.status == ShowSeatStatus.AVAILABLE.value


def test_hold_conflict_holds_nothing(client) -> None:
    user_id = _create_user("+919000000103")
    show_id, free_seat_ids, sold_seat_ids = _pick_show(2, 1)
    requested = free_seat_ids + sold_seat_ids
    url = f"/api/shows/{show_id}/holds"

    response = client.post(url, json={"seatIds": requested}, headers=_auth(user_id))

    assert response.status_code == 409
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Conflict"
    assert body["status"] == 409
    assert body["detail"] == SEATS_UNAVAILABLE_DETAIL
    assert body["seatIds"] == sold_seat_ids
    assert body["instance"] == url

    with SessionLocal() as db:
        for seat_id in free_seat_ids:
            row = db.get(ShowSeat, seat_id)
            assert row is not None
            assert row.status == ShowSeatStatus.AVAILABLE.value
            assert row.held_by is None
            assert row.held_until is None
            assert row.hold_id is None

        for seat_id in sold_seat_ids:
            row = db.get(ShowSeat, seat_id)
            assert row is not None
            assert row.status == ShowSeatStatus.SOLD.value
            assert row.held_by is None


def _mark_expired_hold(seat_id: int, held_by: int) -> None:
    with SessionLocal() as db:
        row = db.get(ShowSeat, seat_id)
        assert row is not None
        row.status = ShowSeatStatus.HELD.value
        row.held_by = held_by
        row.hold_id = str(uuid.uuid4())
        row.held_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()


def test_expired_hold_can_be_claimed_again(client) -> None:
    first_user = _create_user("+919000000105")
    second_user = _create_user("+919000000106")
    show_id, seat_ids, _ = _pick_show(1)
    seat_id = seat_ids[0]
    _mark_expired_hold(seat_id, first_user)

    response = client.post(
        f"/api/shows/{show_id}/holds",
        json={"seatIds": [seat_id]},
        headers=_auth(second_user),
    )

    assert response.status_code == 201
    body = response.json()

    with SessionLocal() as db:
        row = db.get(ShowSeat, seat_id)
        assert row is not None
        assert row.status == ShowSeatStatus.HELD.value
        assert row.held_by == second_user
        assert row.hold_id == body["holdId"]
        assert _as_utc(row.held_until) > datetime.now(timezone.utc) + timedelta(
            minutes=8
        )


def test_hold_rejects_a_seat_from_another_show(client) -> None:
    user_id = _create_user("+919000000104")
    show_id, seat_ids, _ = _pick_show(1)
    with SessionLocal() as db:
        foreign_seat_id = db.scalar(
            select(ShowSeat.id)
            .where(ShowSeat.show_id != show_id)
            .order_by(ShowSeat.id)
            .limit(1)
        )
    assert foreign_seat_id is not None
    url = f"/api/shows/{show_id}/holds"

    response = client.post(
        url,
        json={"seatIds": seat_ids + [foreign_seat_id]},
        headers=_auth(user_id),
    )

    assert response.status_code == 404
    body = response.json()
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == f"Seat not found: {foreign_seat_id}"
    assert body["instance"] == url

    with SessionLocal() as db:
        row = db.get(ShowSeat, seat_ids[0])
        assert row is not None
        assert row.status == ShowSeatStatus.AVAILABLE.value
        assert row.hold_id is None


def test_two_threads_cannot_both_hold_the_same_seat(tmp_path) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'holds.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(bind=engine)
    with OrmSession(engine) as db:
        seed_data(db)

    with OrmSession(engine) as db:
        seat_id = db.scalar(
            select(ShowSeat.id)
            .where(ShowSeat.status == ShowSeatStatus.AVAILABLE.value)
            .order_by(ShowSeat.id)
            .limit(1)
        )
        assert seat_id is not None
        show_id = db.scalar(select(ShowSeat.show_id).where(ShowSeat.id == seat_id))
        assert show_id is not None
        user_ids: list[int] = []
        for phone in ("+919000000201", "+919000000202"):
            user = User(phone=phone, name=None, created_at=datetime.now(timezone.utc))
            db.add(user)
            db.flush()
            user_ids.append(user.id)
        db.commit()

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def worker(user_id: int) -> None:
        with OrmSession(engine, expire_on_commit=False) as db:
            show = db.get(Show, show_id)
            user = db.get(User, user_id)
            assert show is not None and user is not None
            # Close the read transaction so both threads race on the write alone.
            db.commit()
            barrier.wait(timeout=10)
            try:
                acquire_hold(db=db, show=show, seat_ids=[seat_id], user=user)
            except SeatsUnavailableError:
                outcomes.append("conflict")
            except Exception as exc:  # surfaced by the assertion below
                outcomes.append(f"error: {exc!r}")
            else:
                outcomes.append("held")

    threads = [
        threading.Thread(target=worker, args=(user_id,)) for user_id in user_ids
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["conflict", "held"]

    with OrmSession(engine) as db:
        row = db.get(ShowSeat, seat_id)
        assert row is not None
        assert row.status == ShowSeatStatus.HELD.value
        assert row.held_by in user_ids
        assert row.hold_id is not None
        holders = list(
            db.scalars(select(ShowSeat.held_by).where(ShowSeat.id == seat_id))
        )
        assert holders == [row.held_by]

    engine.dispose()
