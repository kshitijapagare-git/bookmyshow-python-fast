import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Seat, Show, ShowSeat, User
from app.schemas import ShowSeatStatus
from app.security import issue_token

SHOW_KEYS = {
    "id",
    "startsAt",
    "format",
    "movieId",
    "movieTitle",
    "language",
    "cinemaId",
    "cinemaName",
}
CATEGORY_KEYS = {"id", "name", "label", "price", "availability", "rows"}
SEAT_KEYS = {"id", "number", "kind", "gapBefore", "bestseller", "status", "heldByMe"}


def _all_seats(body) -> list[dict]:
    return [
        seat
        for category in body["categories"]
        for row in category["rows"]
        for seat in row["seats"]
    ]


def _find_seat(body, seat_id: int) -> dict:
    for seat in _all_seats(body):
        if seat["id"] == seat_id:
            return seat
    raise AssertionError(f"seat {seat_id} missing from the layout")


def _find_category(body, category_id: int) -> dict:
    for category in body["categories"]:
        if category["id"] == category_id:
            return category
    raise AssertionError(f"category {category_id} missing from the layout")


def _create_user(phone: str) -> int:
    with SessionLocal() as db:
        user = User(phone=phone, name=None, created_at=datetime.now(timezone.utc))
        db.add(user)
        db.commit()
        return user.id


def _first_show_id() -> int:
    with SessionLocal() as db:
        show_id = db.scalar(select(Show.id).order_by(Show.id).limit(1))
    assert show_id is not None
    return show_id


def _full_screen_show_id() -> int:
    """The first show of the one screen that carries the full D-Q layout."""
    with SessionLocal() as db:
        screen_id = db.scalar(select(Seat.screen_id).where(Seat.row == "Q").limit(1))
        assert screen_id is not None
        show_id = db.scalar(
            select(Show.id)
            .where(Show.screen_id == screen_id)
            .order_by(Show.id)
            .limit(1)
        )
    assert show_id is not None
    return show_id


def _hold_seat(*, show_id: int, held_by: int, held_until: datetime) -> int:
    with SessionLocal() as db:
        show_seat = db.scalar(
            select(ShowSeat)
            .where(
                ShowSeat.show_id == show_id,
                ShowSeat.status == ShowSeatStatus.AVAILABLE.value,
            )
            .order_by(ShowSeat.id)
        )
        assert show_seat is not None
        show_seat.status = ShowSeatStatus.HELD.value
        show_seat.held_by = held_by
        show_seat.hold_id = str(uuid.uuid4())
        show_seat.held_until = held_until
        db.commit()
        return show_seat.id


def test_seat_layout_groups_by_category_highest_price_first(client) -> None:
    show_id = _full_screen_show_id()

    with SessionLocal() as db:
        show = db.get(Show, show_id)
        assert show is not None
        expected_movie_title = show.movie.title
        expected_cinema_name = show.screen.cinema.name
        expected_movie_id = show.movie_id

    response = client.get(f"/api/shows/{show_id}/seats")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"show", "maxSeatsPerBooking", "categories"}
    assert body["maxSeatsPerBooking"] == 10

    show_body = body["show"]
    assert set(show_body) == SHOW_KEYS
    assert show_body["id"] == show_id
    assert show_body["startsAt"].endswith("+05:30")
    assert show_body["format"]
    assert show_body["movieId"] == expected_movie_id
    assert show_body["movieTitle"] == expected_movie_title
    assert show_body["language"] == "hindi"
    assert show_body["cinemaName"] == expected_cinema_name

    categories = body["categories"]
    assert categories
    prices = [category["price"] for category in categories]
    assert prices == sorted(prices, reverse=True)
    assert {category["name"] for category in categories} == {
        "NORMAL",
        "EXECUTIVE",
        "PREMIUM",
        "PREMIUM_XL",
        "PREMIUM_PLUS",
    }

    for category in categories:
        assert set(category) == CATEGORY_KEYS
        assert category["label"] == category["name"].replace("_", " ")
        assert "_" not in category["label"]
        assert category["availability"] in {"AVAILABLE", "FAST_FILLING"}
        assert category["rows"]

        row_letters = [row["row"] for row in category["rows"]]
        assert row_letters == sorted(row_letters)
        for row in category["rows"]:
            seats = row["seats"]
            assert seats
            numbers = [seat["number"] for seat in seats]
            assert numbers == sorted(numbers, reverse=True)
            for seat in seats:
                assert set(seat) == SEAT_KEYS
                assert seat["kind"] in {"STANDARD", "WHEELCHAIR"}
                assert seat["status"] in {"AVAILABLE", "HELD", "SOLD"}
                assert seat["heldByMe"] is False


def test_category_availability_uses_fifty_percent_threshold(client) -> None:
    show_id = _first_show_id()

    with SessionLocal() as db:
        seat_rows = db.execute(
            select(ShowSeat.id, Seat.category_id)
            .join(Seat, ShowSeat.seat_id == Seat.id)
            .where(ShowSeat.show_id == show_id)
        ).all()
        for show_seat in db.scalars(
            select(ShowSeat).where(ShowSeat.show_id == show_id)
        ):
            show_seat.status = ShowSeatStatus.AVAILABLE.value
            show_seat.held_by = None
            show_seat.held_until = None
            show_seat.hold_id = None
        db.commit()

        seats_by_category: dict[int, list[int]] = {}
        for show_seat_id, category_id in seat_rows:
            seats_by_category.setdefault(category_id, []).append(show_seat_id)

        even_categories = {
            category_id: seat_ids
            for category_id, seat_ids in seats_by_category.items()
            if len(seat_ids) % 2 == 0
        }
        assert even_categories
        category_id, category_seat_ids = max(
            even_categories.items(), key=lambda item: len(item[1])
        )

        half = len(category_seat_ids) // 2
        assert half > 0
        for show_seat_id in category_seat_ids[:half]:
            db.get(ShowSeat, show_seat_id).status = ShowSeatStatus.SOLD.value
        db.commit()

    url = f"/api/shows/{show_id}/seats"

    at_threshold = client.get(url)
    assert at_threshold.status_code == 200
    assert _find_category(at_threshold.json(), category_id)["availability"] == (
        "FAST_FILLING"
    )

    with SessionLocal() as db:
        db.get(ShowSeat, category_seat_ids[0]).status = ShowSeatStatus.AVAILABLE.value
        db.commit()

    below_threshold = client.get(url)
    assert below_threshold.status_code == 200
    assert _find_category(below_threshold.json(), category_id)["availability"] == (
        "AVAILABLE"
    )


def test_expired_hold_reads_as_available(client) -> None:
    show_id = _first_show_id()
    holder_id = _create_user("+919000000010")
    seat_id = _hold_seat(
        show_id=show_id,
        held_by=holder_id,
        held_until=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    response = client.get(
        f"/api/shows/{show_id}/seats",
        headers={"Authorization": f"Bearer {issue_token(user_id=holder_id)}"},
    )

    assert response.status_code == 200
    assert _find_seat(response.json(), seat_id)["status"] == "AVAILABLE"

    with SessionLocal() as db:
        show_seat = db.get(ShowSeat, seat_id)
        assert show_seat is not None
        assert show_seat.status == ShowSeatStatus.HELD.value


def test_held_by_me_only_for_the_holder(client) -> None:
    show_id = _first_show_id()
    holder_id = _create_user("+919000000020")
    other_id = _create_user("+919000000021")
    seat_id = _hold_seat(
        show_id=show_id,
        held_by=holder_id,
        held_until=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    url = f"/api/shows/{show_id}/seats"

    guest = client.get(url)

    assert guest.status_code == 200
    guest_seat = _find_seat(guest.json(), seat_id)
    assert guest_seat["status"] == "HELD"
    assert guest_seat["heldByMe"] is False

    garbage = client.get(url, headers={"Authorization": "Bearer not-a-jwt"})

    assert garbage.status_code == 200
    assert all(seat["heldByMe"] is False for seat in _all_seats(garbage.json()))

    stranger = client.get(
        url, headers={"Authorization": f"Bearer {issue_token(user_id=other_id)}"}
    )

    assert stranger.status_code == 200
    stranger_seat = _find_seat(stranger.json(), seat_id)
    assert stranger_seat["status"] == "HELD"
    assert stranger_seat["heldByMe"] is False

    owner = client.get(
        url, headers={"Authorization": f"Bearer {issue_token(user_id=holder_id)}"}
    )

    assert owner.status_code == 200
    owner_seat = _find_seat(owner.json(), seat_id)
    assert owner_seat["status"] == "HELD"
    assert owner_seat["heldByMe"] is True


def test_unknown_show_returns_404(client) -> None:
    response = client.get("/api/shows/999999/seats")

    assert response.status_code == 404
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == "Show not found: 999999"
    assert body["instance"] == "/api/shows/999999/seats"
