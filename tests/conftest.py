import base64
import hashlib
import hmac
import itertools
import json
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Booking, BookingTransfer, Cinema, City, Movie, Show
from app.payments_client import PaymentsUnavailable
from app.routers import transfers as transfer_routes
from app.security import JWT_SECRET
from seed import seed_movies


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_movies(db)

    with TestClient(app) as test_client:
        yield test_client

    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def city(client):
    response = client.post("/api/cities", json={"name": "Pune", "state": "Maharashtra"})
    assert response.status_code == 201
    return response.json()


@pytest.fixture()
def seeded_booking(client):
    """Creates a city, cinema, movie, show and CONFIRMED booking; returns its facts."""

    def _create(
        *,
        owner_id: int = 42,
        starts_at: datetime | None = None,
        booking_status: str = "CONFIRMED",
    ) -> dict:
        show_starts_at = starts_at or datetime.now(timezone.utc) + timedelta(days=1)
        with SessionLocal() as db:
            city_row = City(name="Pune", state="Maharashtra")
            db.add(city_row)
            db.flush()

            cinema = Cinema(name="PVR Icon", address="Phoenix Mall", city_id=city_row.id)
            db.add(cinema)
            db.flush()

            movie = Movie(title="Inception", genres="Sci-Fi/Thriller")
            db.add(movie)
            db.flush()

            show = Show(movie_id=movie.id, cinema_id=cinema.id, starts_at=show_starts_at)
            db.add(show)
            db.flush()

            booking = Booking(
                user_id=owner_id,
                show_id=show.id,
                status=booking_status,
                total=Decimal("400.00"),
            )
            db.add(booking)
            db.commit()
            return {
                "bookingId": booking.id,
                "ownerId": owner_id,
                "showId": show.id,
                "startsAt": show_starts_at,
            }

    return _create


def _b64encode(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def _make_token(user_id: int) -> str:
    header = _b64encode({"alg": "HS256", "typ": "JWT"})
    payload = _b64encode({"sub": user_id})
    signing_input = f"{header}.{payload}".encode()
    signature = hmac.new(JWT_SECRET.encode(), signing_input, hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{header}.{payload}.{encoded_signature}"


def _auth_headers(user_id: int) -> dict:
    return {"Authorization": f"Bearer {_make_token(user_id)}"}


_transfer_code_sequence = itertools.count(1)


def _next_transfer_code() -> str:
    return f"TRF{next(_transfer_code_sequence):03d}"


@pytest.fixture()
def pending_transfer(seeded_booking):
    """Creates a booking plus a PENDING transfer for it and returns both rows' facts."""

    def _create(
        *,
        owner_id: int = 42,
        to_user_id: int | None = 77,
        starts_at: datetime | None = None,
    ) -> dict:
        booking = seeded_booking(owner_id=owner_id, starts_at=starts_at)
        with SessionLocal() as db:
            transfer = BookingTransfer(
                booking_id=booking["bookingId"],
                from_user_id=owner_id,
                to_user_id=to_user_id,
                to_phone=None if to_user_id is not None else "+919876543210",
                code=_next_transfer_code(),
                status="PENDING",
                expires_at=booking["startsAt"],
            )
            db.add(transfer)
            db.commit()
            db.refresh(transfer)
            return {
                **booking,
                "transferId": transfer.id,
                "transferCode": transfer.code,
                "toUserId": transfer.to_user_id,
            }

    return _create


@pytest.fixture()
def stale_transfer(pending_transfer):
    """A PENDING transfer whose show start (and therefore expiry) is already in the past."""

    def _create(*, owner_id: int = 42, to_user_id: int = 77) -> dict:
        return pending_transfer(
            owner_id=owner_id,
            to_user_id=to_user_id,
            starts_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )

    return _create


@pytest.fixture()
def accepted_transfer(client, pending_transfer, fake_payments):
    """A transfer accepted by its recipient, so the booking owner has already moved."""

    def _create(*, owner_id: int = 42, to_user_id: int = 77) -> dict:
        transfer = pending_transfer(owner_id=owner_id, to_user_id=to_user_id)
        response = client.post(
            f"/api/transfers/{transfer['transferCode']}/accept",
            headers=_auth_headers(to_user_id),
        )
        assert response.status_code == 200
        return transfer

    return _create


@pytest.fixture()
def fake_payments(monkeypatch):
    """Records the payments calls the transfer routes make, without touching the network."""
    calls: dict[str, list[dict]] = {"settle": [], "release": []}

    def _settle(*, booking_id, from_user_id, to_user_id, amount, fee):
        calls["settle"].append(
            {
                "booking_id": booking_id,
                "from_user_id": from_user_id,
                "to_user_id": to_user_id,
                "amount": amount,
                "fee": fee,
            }
        )
        return {"transferId": 5, "status": "SUCCEEDED", "amount": amount}

    def _release(*, booking_id):
        calls["release"].append({"booking_id": booking_id})
        return {"transferId": 5, "status": "REFUNDED", "amount": 0.0}

    monkeypatch.setattr(transfer_routes, "settle_transfer", _settle)
    monkeypatch.setattr(transfer_routes, "release_transfer", _release)
    return calls


@pytest.fixture()
def failing_payments(monkeypatch):
    """Makes both payments calls fail, so the transfer routes have to leave state alone."""

    def _fail(**_kwargs):
        raise PaymentsUnavailable("payments service is down")

    monkeypatch.setattr(transfer_routes, "settle_transfer", _fail)
    monkeypatch.setattr(transfer_routes, "release_transfer", _fail)
    return _fail
