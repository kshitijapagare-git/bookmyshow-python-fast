"""The internal booking lookup the ratings service makes before storing a review.

It answers the booking's *current* owner, so a ticket that changed hands on the
booking API can never be reviewed by the moviegoer who no longer holds it.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Booking, Show, ShowSeat, User
from app.schemas import ShowSeatStatus
from app.security import BOOKINGS_SERVICE_TOKEN, issue_token

INTERNAL_URL = "/internal/bookings"
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
BOOKING_KEYS = {"bookingId", "userId", "status", "showId", "showStartsAt", "movieId"}
TOKEN_HEADER = "X-Service-Token"


def _create_user(phone: str) -> int:
    with SessionLocal() as db:
        user = User(phone=phone, name=None, created_at=datetime.now(timezone.utc))
        db.add(user)
        db.commit()
        return user.id


def _available_seat_ids(count: int = 1) -> tuple[int, list[int]]:
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


def _confirmed_booking_row(user_id: int) -> tuple[int, int, int]:
    """A CONFIRMED booking row for the user, plus its show id and movie id."""
    show_id, seat_ids = _available_seat_ids(1)
    with SessionLocal() as db:
        booking = Booking(
            user_id=user_id,
            show_id=show_id,
            hold_id=str(uuid.uuid4()),
            seat_ids=seat_ids,
            total=Decimal("250.00"),
            status="CONFIRMED",
            payment_id=700,
            idempotency_key=str(uuid.uuid4()),
            created_at=datetime.now(timezone.utc),
        )
        db.add(booking)
        db.commit()
        booking_id = booking.id
        movie_id = db.scalar(select(Show.movie_id).where(Show.id == show_id))
    assert movie_id is not None
    return booking_id, show_id, movie_id


def test_internal_booking_answers_the_current_owner(client) -> None:
    user_id = _create_user("+919000004001")
    booking_id, show_id, movie_id = _confirmed_booking_row(user_id)

    response = client.get(
        f"{INTERNAL_URL}/{booking_id}", headers={TOKEN_HEADER: BOOKINGS_SERVICE_TOKEN}
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == BOOKING_KEYS
    assert body["bookingId"] == booking_id
    assert body["userId"] == user_id
    assert body["status"] == "CONFIRMED"
    assert body["showId"] == show_id
    assert body["movieId"] == movie_id
    assert body["showStartsAt"].endswith("+05:30")


def test_internal_booking_follows_a_transferred_booking(client) -> None:
    owner_id = _create_user("+919000004002")
    recipient_id = _create_user("+919000004003")
    booking_id, _, _ = _confirmed_booking_row(owner_id)

    with SessionLocal() as db:
        booking = db.get(Booking, booking_id)
        assert booking is not None
        booking.user_id = recipient_id
        db.commit()

    response = client.get(
        f"{INTERNAL_URL}/{booking_id}", headers={TOKEN_HEADER: BOOKINGS_SERVICE_TOKEN}
    )

    assert response.status_code == 200
    assert response.json()["userId"] == recipient_id


def test_internal_booking_requires_the_service_token(client) -> None:
    user_id = _create_user("+919000004004")
    booking_id, _, _ = _confirmed_booking_row(user_id)
    url = f"{INTERNAL_URL}/{booking_id}"

    missing = client.get(url)

    assert missing.status_code == 401
    body = missing.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Unauthorized"
    assert body["status"] == 401
    assert body["detail"] == "Invalid service token"
    assert body["instance"] == url
    assert "userId" not in body

    wrong = client.get(url, headers={TOKEN_HEADER: "not-the-token"})

    assert wrong.status_code == 401
    assert set(wrong.json()) == PROBLEM_DETAIL_KEYS
    assert wrong.json()["detail"] == "Invalid service token"


def test_internal_booking_of_an_unknown_booking_is_404(client) -> None:
    response = client.get(
        f"{INTERNAL_URL}/999999", headers={TOKEN_HEADER: BOOKINGS_SERVICE_TOKEN}
    )

    assert response.status_code == 404
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == "Booking not found: 999999"
    assert body["instance"] == f"{INTERNAL_URL}/999999"


def test_the_public_booking_route_still_answers_beside_the_internal_one(client) -> None:
    user_id = _create_user("+919000004005")
    booking_id, show_id, _ = _confirmed_booking_row(user_id)

    response = client.get(
        f"/api/bookings/{booking_id}",
        headers={"Authorization": f"Bearer {issue_token(user_id=user_id)}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["bookingId"] == booking_id
    assert body["showId"] == show_id
    assert body["status"] == "CONFIRMED"
    assert body["transfer"] is None
