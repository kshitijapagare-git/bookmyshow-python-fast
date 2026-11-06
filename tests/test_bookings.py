import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.db import SessionLocal
from app.models import Booking, Cinema, City, Movie, Show
from app.security import JWT_SECRET


def _b64encode(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def _make_token(user_id: int) -> str:
    header = _b64encode({"alg": "HS256", "typ": "JWT"})
    payload = _b64encode({"sub": user_id})
    signing_input = f"{header}.{payload}".encode()
    signature = hmac.new(JWT_SECRET.encode(), signing_input, hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{header}.{payload}.{encoded_signature}"


def _seed_booking(*, owner_id: int = 42, starts_at: datetime | None = None) -> int:
    with SessionLocal() as db:
        city = City(name="Pune", state="Maharashtra")
        db.add(city)
        db.flush()

        cinema = Cinema(name="PVR Icon", address="Phoenix Mall", city_id=city.id)
        db.add(cinema)
        db.flush()

        movie = Movie(title="Inception", genres="Sci-Fi/Thriller")
        db.add(movie)
        db.flush()

        show = Show(
            movie_id=movie.id,
            cinema_id=cinema.id,
            starts_at=starts_at or datetime.now(timezone.utc) + timedelta(days=1),
        )
        db.add(show)
        db.flush()

        booking = Booking(
            user_id=owner_id,
            show_id=show.id,
            status="CONFIRMED",
            total=Decimal("400.00"),
        )
        db.add(booking)
        db.commit()
        return booking.id


def test_get_booking_returns_owner_and_no_transfer(client):
    booking_id = _seed_booking()

    response = client.get(
        f"/api/bookings/{booking_id}",
        headers={"Authorization": f"Bearer {_make_token(42)}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["bookingId"] == booking_id
    assert body["userId"] == 42
    assert body["status"] == "CONFIRMED"
    assert body["total"] == 400.0
    assert body["showId"] > 0
    assert body["createdAt"]
    assert body["transfer"] is None


def test_get_booking_returns_cancelled_status(client):
    booking_id = _seed_booking()
    with SessionLocal() as db:
        booking = db.get(Booking, booking_id)
        booking.status = "CANCELLED"
        db.commit()

    response = client.get(
        f"/api/bookings/{booking_id}",
        headers={"Authorization": f"Bearer {_make_token(42)}"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"


def test_get_booking_requires_a_token(client):
    booking_id = _seed_booking()

    response = client.get(f"/api/bookings/{booking_id}")

    assert response.status_code == 401
    assert response.json()["detail"] == "Not authenticated"


def test_get_booking_other_user_is_forbidden(client):
    booking_id = _seed_booking()

    response = client.get(
        f"/api/bookings/{booking_id}",
        headers={"Authorization": f"Bearer {_make_token(77)}"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Booking belongs to another user"


def test_unknown_booking_answers_404(client):
    response = client.get(
        "/api/bookings/999",
        headers={"Authorization": f"Bearer {_make_token(42)}"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Booking not found"
