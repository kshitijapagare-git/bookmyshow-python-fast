"""Booking flow tests: paying a live hold, declines, replays and payment outages.

The payments service is faked through FastAPI's dependency override, exactly as the
auth tests fake the SMS sender, so the suite never needs a live payments service and
every charge the booking flow makes is recorded for the idempotency assertions.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import Booking, Seat, SeatCategory, ShowSeat, User
from app.payments_client import (
    PaymentDeclinedError,
    PaymentsUnavailableError,
    get_payments_client,
)
from app.schemas import ShowSeatStatus
from app.security import issue_token

BOOKINGS_URL = "/api/bookings"
DECLINE_DETAIL = "Payment declined"
UNAVAILABLE_DETAIL = "Payment service unavailable"
NOT_OWNER_DETAIL = "This hold belongs to another user"
EXPIRED_DETAIL = "Hold has expired"
PAYMENT_ID = 7
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
CENT = Decimal("0.01")


class _FakePaymentsClient:
    """Stand-in for PaymentsClient: records each charge and answers as told."""

    def __init__(self, outcome: str = "succeed") -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    def create_payment(
        self,
        *,
        booking_id: int,
        amount: Decimal,
        currency: str = "INR",
        idempotency_key: str,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "booking_id": booking_id,
                "amount": amount,
                "currency": currency,
                "idempotency_key": idempotency_key,
            }
        )
        if self.outcome == "decline":
            raise PaymentDeclinedError(DECLINE_DETAIL)
        if self.outcome == "unavailable":
            raise PaymentsUnavailableError(UNAVAILABLE_DETAIL)
        return {
            "paymentId": PAYMENT_ID,
            "bookingId": booking_id,
            "status": "SUCCEEDED",
            "amount": float(amount),
            "currency": currency,
            "providerRef": "mock_ref",
            "paidAt": datetime.now(timezone.utc).isoformat(),
        }


@contextmanager
def _use_payments(fake: _FakePaymentsClient) -> Iterator[_FakePaymentsClient]:
    app.dependency_overrides[get_payments_client] = lambda: fake
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_payments_client, None)


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


def _create_hold(client, user_id: int, count: int = 1) -> tuple[int, list[int], str]:
    show_id, seat_ids = _available_seats(count)
    response = client.post(
        f"/api/shows/{show_id}/holds",
        json={"seatIds": seat_ids},
        headers=_auth(user_id),
    )
    assert response.status_code == 201
    return show_id, seat_ids, response.json()["holdId"]


def _expected_total(seat_ids: list[int]) -> Decimal:
    """The hold's price, summed from the seat categories the seats belong to."""
    with SessionLocal() as db:
        prices = db.scalars(
            select(SeatCategory.price)
            .join(Seat, Seat.category_id == SeatCategory.id)
            .join(ShowSeat, ShowSeat.seat_id == Seat.id)
            .where(ShowSeat.id.in_(seat_ids))
        )
        return sum((Decimal(price) for price in prices), Decimal("0")).quantize(CENT)


def _booking_key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def test_booking_success_confirms_and_sells_seats(client) -> None:
    user_id = _create_user("+919000000501")
    show_id, seat_ids, hold_id = _create_hold(client, user_id, 2)
    expected_total = _expected_total(seat_ids)
    fake = _FakePaymentsClient()

    with _use_payments(fake):
        response = client.post(
            BOOKINGS_URL,
            json={"holdId": hold_id},
            headers={**_auth(user_id), **_booking_key()},
        )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "bookingId",
        "showId",
        "holdId",
        "status",
        "seats",
        "total",
        "paymentId",
        "createdAt",
        "refundedAt",
    }
    assert body["status"] == "CONFIRMED"
    assert body["showId"] == show_id
    assert body["holdId"] == hold_id
    assert body["paymentId"] == PAYMENT_ID
    assert body["total"] == float(expected_total)
    assert [seat["id"] for seat in body["seats"]] == seat_ids
    for seat in body["seats"]:
        assert set(seat) == {"id", "label"}
        assert seat["label"]

    assert len(fake.calls) == 1
    charge = fake.calls[0]
    assert charge["booking_id"] == body["bookingId"]
    assert charge["idempotency_key"] == str(body["bookingId"])
    assert charge["currency"] == "INR"
    assert Decimal(str(charge["amount"])) == expected_total

    with SessionLocal() as db:
        for seat_id in seat_ids:
            row = db.get(ShowSeat, seat_id)
            assert row is not None
            assert row.status == ShowSeatStatus.SOLD.value
            assert row.held_by is None
            assert row.held_until is None
            assert row.hold_id is None

        booking = db.get(Booking, body["bookingId"])
        assert booking is not None
        assert booking.status == "CONFIRMED"
        assert booking.payment_id == PAYMENT_ID
        assert booking.user_id == user_id
        assert booking.show_id == show_id
        assert booking.hold_id == hold_id
        assert list(booking.seat_ids) == seat_ids
        assert Decimal(booking.total) == expected_total


def test_booking_decline_returns_402_and_releases_hold(client) -> None:
    user_id = _create_user("+919000000502")
    _, seat_ids, hold_id = _create_hold(client, user_id, 1)
    key = str(uuid.uuid4())
    fake = _FakePaymentsClient(outcome="decline")

    with _use_payments(fake):
        response = client.post(
            BOOKINGS_URL,
            json={"holdId": hold_id},
            headers={**_auth(user_id), "Idempotency-Key": key},
        )

    assert response.status_code == 402
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Payment Required"
    assert body["status"] == 402
    assert body["detail"] == DECLINE_DETAIL
    assert body["instance"] == BOOKINGS_URL
    assert len(fake.calls) == 1

    with SessionLocal() as db:
        for seat_id in seat_ids:
            row = db.get(ShowSeat, seat_id)
            assert row is not None
            assert row.status == ShowSeatStatus.AVAILABLE.value
            assert row.held_by is None
            assert row.held_until is None
            assert row.hold_id is None

        booking = db.scalar(select(Booking).where(Booking.idempotency_key == key))
        assert booking is not None
        assert booking.status == "FAILED"
        assert booking.payment_id is None


def test_repeated_idempotency_key_reuses_the_booking(client) -> None:
    user_id = _create_user("+919000000503")
    _, seat_ids, hold_id = _create_hold(client, user_id, 1)
    key = str(uuid.uuid4())
    headers = {**_auth(user_id), "Idempotency-Key": key}
    fake = _FakePaymentsClient()

    with _use_payments(fake):
        first = client.post(BOOKINGS_URL, json={"holdId": hold_id}, headers=headers)
        second = client.post(BOOKINGS_URL, json={"holdId": hold_id}, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["bookingId"] == first.json()["bookingId"]
    assert second.json()["status"] == "CONFIRMED"
    assert [seat["id"] for seat in second.json()["seats"]] == seat_ids
    assert len(fake.calls) == 1

    with SessionLocal() as db:
        assert list(db.scalars(select(Booking.id))) == [first.json()["bookingId"]]


def test_expired_hold_returns_410(client) -> None:
    user_id = _create_user("+919000000504")
    _, seat_ids, hold_id = _create_hold(client, user_id, 1)

    with SessionLocal() as db:
        row = db.get(ShowSeat, seat_ids[0])
        assert row is not None
        row.held_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()

    fake = _FakePaymentsClient()
    with _use_payments(fake):
        response = client.post(
            BOOKINGS_URL,
            json={"holdId": hold_id},
            headers={**_auth(user_id), **_booking_key()},
        )

    assert response.status_code == 410
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Gone"
    assert body["status"] == 410
    assert body["detail"] == EXPIRED_DETAIL
    assert body["instance"] == BOOKINGS_URL
    assert fake.calls == []

    with SessionLocal() as db:
        assert list(db.scalars(select(Booking))) == []


def test_another_users_hold_returns_403(client) -> None:
    owner_id = _create_user("+919000000505")
    stranger_id = _create_user("+919000000506")
    _, seat_ids, hold_id = _create_hold(client, owner_id, 1)
    fake = _FakePaymentsClient()

    with _use_payments(fake):
        response = client.post(
            BOOKINGS_URL,
            json={"holdId": hold_id},
            headers={**_auth(stranger_id), **_booking_key()},
        )

    assert response.status_code == 403
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Forbidden"
    assert body["status"] == 403
    assert body["detail"] == NOT_OWNER_DETAIL
    assert body["instance"] == BOOKINGS_URL
    assert fake.calls == []

    with SessionLocal() as db:
        assert list(db.scalars(select(Booking))) == []
        row = db.get(ShowSeat, seat_ids[0])
        assert row is not None
        assert row.status == ShowSeatStatus.HELD.value
        assert row.held_by == owner_id
        assert row.hold_id == hold_id


def test_payments_service_down_returns_503_and_keeps_booking_pending(client) -> None:
    user_id = _create_user("+919000000507")
    _, seat_ids, hold_id = _create_hold(client, user_id, 1)
    key = str(uuid.uuid4())
    fake = _FakePaymentsClient(outcome="unavailable")

    with _use_payments(fake):
        response = client.post(
            BOOKINGS_URL,
            json={"holdId": hold_id},
            headers={**_auth(user_id), "Idempotency-Key": key},
        )

    assert response.status_code == 503
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Service Unavailable"
    assert body["status"] == 503
    assert body["detail"] == UNAVAILABLE_DETAIL
    assert body["instance"] == BOOKINGS_URL
    assert len(fake.calls) == 1

    with SessionLocal() as db:
        booking = db.scalar(select(Booking).where(Booking.idempotency_key == key))
        assert booking is not None
        assert booking.status == "PENDING_PAYMENT"
        assert booking.payment_id is None

        row = db.get(ShowSeat, seat_ids[0])
        assert row is not None
        assert row.status == ShowSeatStatus.HELD.value
        assert row.held_by == user_id
        assert row.hold_id == hold_id


def test_get_booking_is_owner_only(client) -> None:
    owner_id = _create_user("+919000000508")
    stranger_id = _create_user("+919000000509")
    show_id, seat_ids, hold_id = _create_hold(client, owner_id, 1)
    fake = _FakePaymentsClient()

    with _use_payments(fake):
        created = client.post(
            BOOKINGS_URL,
            json={"holdId": hold_id},
            headers={**_auth(owner_id), **_booking_key()},
        )
    assert created.status_code == 201
    booking_id = created.json()["bookingId"]

    owner_read = client.get(f"/api/bookings/{booking_id}", headers=_auth(owner_id))

    assert owner_read.status_code == 200
    body = owner_read.json()
    assert body["bookingId"] == booking_id
    assert body["status"] == "CONFIRMED"
    assert body["showId"] == show_id
    assert body["holdId"] == hold_id
    assert body["total"] == created.json()["total"]
    assert [seat["id"] for seat in body["seats"]] == seat_ids

    stranger_read = client.get(
        f"/api/bookings/{booking_id}", headers=_auth(stranger_id)
    )

    assert stranger_read.status_code == 403
    stranger_body = stranger_read.json()
    assert stranger_body["title"] == "Forbidden"
    assert stranger_body["status"] == 403
    assert stranger_body["detail"] == "This booking belongs to another user"

    unknown = client.get("/api/bookings/999999", headers=_auth(owner_id))

    assert unknown.status_code == 404
    unknown_body = unknown.json()
    assert unknown_body["title"] == "Not Found"
    assert unknown_body["status"] == 404
    assert unknown_body["detail"] == "Booking not found: 999999"
