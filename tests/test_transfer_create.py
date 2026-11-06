import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Booking, BookingTransfer, Show
from app.security import JWT_SECRET

TRANSFER_CODE_PATTERN = re.compile(r"^TRF[A-Z0-9]{3}$")


def _b64encode(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def _make_token(user_id: int) -> str:
    header = _b64encode({"alg": "HS256", "typ": "JWT"})
    payload = _b64encode({"sub": user_id})
    signing_input = f"{header}.{payload}".encode()
    signature = hmac.new(JWT_SECRET.encode(), signing_input, hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{header}.{payload}.{encoded_signature}"


def _auth(user_id: int) -> dict:
    return {"Authorization": f"Bearer {_make_token(user_id)}"}


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def test_create_transfer_returns_pending_code_and_show_start(client, seeded_booking):
    booking = seeded_booking()

    response = client.post(
        f"/api/bookings/{booking['bookingId']}/transfer",
        json={"toUserId": 77},
        headers=_auth(booking["ownerId"]),
    )

    assert response.status_code == 201
    body = response.json()
    assert TRANSFER_CODE_PATTERN.match(body["transferCode"])
    assert body["status"] == "PENDING"
    assert _as_utc(datetime.fromisoformat(body["expiresAt"])) == _as_utc(booking["startsAt"])

    with SessionLocal() as db:
        transfer = db.scalar(
            select(BookingTransfer).where(BookingTransfer.code == body["transferCode"])
        )
        assert transfer.booking_id == booking["bookingId"]
        assert transfer.from_user_id == booking["ownerId"]
        assert transfer.to_user_id == 77
        assert transfer.status == "PENDING"
        assert float(transfer.fee) == 0.0
        assert transfer.created_at is not None
        assert _as_utc(transfer.expires_at) == _as_utc(booking["startsAt"])

    duplicate = client.post(
        f"/api/bookings/{booking['bookingId']}/transfer",
        json={"toUserId": 78},
        headers=_auth(booking["ownerId"]),
    )

    assert duplicate.status_code == 409
    with SessionLocal() as db:
        transfers = list(
            db.scalars(
                select(BookingTransfer).where(
                    BookingTransfer.booking_id == booking["bookingId"]
                )
            )
        )
        assert len(transfers) == 1


def test_create_transfer_requires_the_owner(client, seeded_booking):
    booking = seeded_booking()

    response = client.post(
        f"/api/bookings/{booking['bookingId']}/transfer",
        json={"toUserId": 77},
        headers=_auth(77),
    )

    assert response.status_code == 403
    with SessionLocal() as db:
        existing = db.scalar(
            select(BookingTransfer.id).where(BookingTransfer.booking_id == booking["bookingId"])
        )
        assert existing is None


def test_create_transfer_rejects_unconfirmed_booking_and_started_show(client, seeded_booking):
    unconfirmed = seeded_booking()
    with SessionLocal() as db:
        db.get(Booking, unconfirmed["bookingId"]).status = "CANCELLED"
        db.commit()

    rejected = client.post(
        f"/api/bookings/{unconfirmed['bookingId']}/transfer",
        json={"toUserId": 77},
        headers=_auth(unconfirmed["ownerId"]),
    )

    assert rejected.status_code == 409

    started = seeded_booking(starts_at=datetime.now(timezone.utc) - timedelta(hours=1))

    rejected = client.post(
        f"/api/bookings/{started['bookingId']}/transfer",
        json={"toUserId": 77},
        headers=_auth(started["ownerId"]),
    )

    assert rejected.status_code == 409
    with SessionLocal() as db:
        existing = db.scalar(
            select(BookingTransfer.id).where(BookingTransfer.booking_id == started["bookingId"])
        )
        assert existing is None


def test_transfer_read_back_expires_after_show_start(client, seeded_booking):
    booking = seeded_booking()

    created = client.post(
        f"/api/bookings/{booking['bookingId']}/transfer",
        json={"toUserId": 77},
        headers=_auth(booking["ownerId"]),
    )
    assert created.status_code == 201
    code = created.json()["transferCode"]

    with SessionLocal() as db:
        show = db.get(Show, booking["showId"])
        show.starts_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.commit()

    response = client.get(f"/api/transfers/{code}")

    assert response.status_code == 200
    body = response.json()
    assert body["transferCode"] == code
    assert body["status"] == "EXPIRED"
    assert body["movieTitle"] == "Inception"
    assert body["cinemaName"] == "PVR Icon"
    assert body["startsAt"]

    with SessionLocal() as db:
        transfer = db.scalar(select(BookingTransfer).where(BookingTransfer.code == code))
        assert transfer.status == "EXPIRED"

    assert client.get("/api/transfers/TRFZZZ").status_code == 404


def test_create_transfer_by_phone_leaves_to_user_id_null(client, seeded_booking):
    booking = seeded_booking()

    created = client.post(
        f"/api/bookings/{booking['bookingId']}/transfer",
        json={"toPhone": "+919876543210"},
        headers=_auth(booking["ownerId"]),
    )

    assert created.status_code == 201
    with SessionLocal() as db:
        transfer = db.scalar(
            select(BookingTransfer).where(
                BookingTransfer.code == created.json()["transferCode"]
            )
        )
        assert transfer.to_user_id is None
        assert transfer.to_phone == "+919876543210"

    both = client.post(
        f"/api/bookings/{booking['bookingId']}/transfer",
        json={"toUserId": 77, "toPhone": "+919876543210"},
        headers=_auth(booking["ownerId"]),
    )

    assert both.status_code == 422
