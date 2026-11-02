import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ShowSeat, User
from app.schemas import ShowSeatStatus
from app.security import issue_token

NOT_OWNER_DETAIL = "This hold belongs to another user"


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


def _create_hold(client, user_id: int, count: int = 2) -> tuple[int, list[int], str]:
    show_id, seat_ids = _available_seats(count)
    response = client.post(
        f"/api/shows/{show_id}/holds",
        json={"seatIds": seat_ids},
        headers=_auth(user_id),
    )
    assert response.status_code == 201
    return show_id, seat_ids, response.json()["holdId"]


def test_owner_can_release_a_hold(client) -> None:
    user_id = _create_user("+919000000301")
    _, seat_ids, hold_id = _create_hold(client, user_id, 2)

    response = client.delete(f"/api/holds/{hold_id}", headers=_auth(user_id))

    assert response.status_code == 204
    assert response.content == b""

    with SessionLocal() as db:
        for seat_id in seat_ids:
            row = db.get(ShowSeat, seat_id)
            assert row is not None
            assert row.status == ShowSeatStatus.AVAILABLE.value
            assert row.held_by is None
            assert row.held_until is None
            assert row.hold_id is None


def test_another_user_cannot_release_a_hold(client) -> None:
    owner_id = _create_user("+919000000302")
    stranger_id = _create_user("+919000000303")
    _, seat_ids, hold_id = _create_hold(client, owner_id, 1)

    response = client.delete(f"/api/holds/{hold_id}", headers=_auth(stranger_id))

    assert response.status_code == 403
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Forbidden"
    assert body["status"] == 403
    assert body["detail"] == NOT_OWNER_DETAIL
    assert body["instance"] == f"/api/holds/{hold_id}"

    with SessionLocal() as db:
        for seat_id in seat_ids:
            row = db.get(ShowSeat, seat_id)
            assert row is not None
            assert row.status == ShowSeatStatus.HELD.value
            assert row.held_by == owner_id
            assert row.hold_id == hold_id
            assert row.held_until is not None


def test_release_requires_a_token_and_a_known_hold(client) -> None:
    user_id = _create_user("+919000000304")
    _, seat_ids, hold_id = _create_hold(client, user_id, 1)
    url = f"/api/holds/{hold_id}"

    missing = client.delete(url)

    assert missing.status_code == 401
    body = missing.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Unauthorized"
    assert body["status"] == 401
    assert body["instance"] == url

    garbage = client.delete(url, headers={"Authorization": "Bearer not-a-jwt"})

    assert garbage.status_code == 401
    assert garbage.json()["title"] == "Unauthorized"

    unknown_hold_id = str(uuid.uuid4())
    unknown = client.delete(f"/api/holds/{unknown_hold_id}", headers=_auth(user_id))

    assert unknown.status_code == 404
    unknown_body = unknown.json()
    assert unknown_body["title"] == "Not Found"
    assert unknown_body["status"] == 404
    assert unknown_body["detail"] == f"Hold not found: {unknown_hold_id}"

    with SessionLocal() as db:
        row = db.get(ShowSeat, seat_ids[0])
        assert row is not None
        assert row.status == ShowSeatStatus.HELD.value
        assert row.held_by == user_id
        assert row.hold_id == hold_id
