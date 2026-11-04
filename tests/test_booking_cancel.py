"""Cancelling a confirmed booking: refund the payment and put the seats back on sale.

The payments service is faked through FastAPI's dependency override, exactly as in
tests/test_bookings.py, so a single override serves both the charge the booking flow
makes and the refund the cancel flow makes, and every refund is recorded for the
"exactly one refund" assertions.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import Booking, ShowSeat, User
from app.payments_client import PaymentsUnavailableError, get_payments_client
from app.schemas import ShowSeatStatus
from app.security import issue_token

BOOKINGS_URL = "/api/bookings"
UNAVAILABLE_DETAIL = "Payment service unavailable"
REFUND_FAILED_DETAIL = "Refund could not be processed"
NOT_OWNER_DETAIL = "This booking belongs to another user"
PAYMENT_ID = 11
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
CANCEL_KEYS = {"id", "status", "refundedAt"}


class _FakePaymentsClient:
    """Stand-in for PaymentsClient: answers the charge and records every refund."""

    def __init__(
        self, outcome: str = "succeed", refund_outcome: str | None = None
    ) -> None:
        self.outcome = outcome
        self.refund_outcome = refund_outcome if refund_outcome is not None else outcome
        self.calls: list[dict[str, object]] = []
        self.refunds: list[dict[str, object]] = []

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

    def refund_payment(
        self, *, payment_id: int, idempotency_key: str
    ) -> dict[str, object]:
        self.refunds.append(
            {"payment_id": payment_id, "idempotency_key": idempotency_key}
        )
        if self.refund_outcome == "unavailable":
            raise PaymentsUnavailableError(UNAVAILABLE_DETAIL)
        return {
            "paymentId": payment_id,
            "status": "REFUNDED",
            "refundedAt": datetime.now(timezone.utc).isoformat(),
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


def _booking_key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _confirmed_booking(
    client, fake: _FakePaymentsClient, user_id: int, count: int = 1
) -> tuple[int, list[int]]:
    """Pay for a fresh hold, returning the booking id and the seat ids it sold."""
    _, seat_ids, hold_id = _create_hold(client, user_id, count)
    with _use_payments(fake):
        response = client.post(
            BOOKINGS_URL,
            json={"holdId": hold_id},
            headers={**_auth(user_id), **_booking_key()},
        )
    assert response.status_code == 201
    assert response.json()["status"] == "CONFIRMED"
    return response.json()["bookingId"], seat_ids


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def test_cancel_confirms_refund_and_releases_seats(client) -> None:
    user_id = _create_user("+919000000601")
    fake = _FakePaymentsClient()
    booking_id, seat_ids = _confirmed_booking(client, fake, user_id, 2)

    with _use_payments(fake):
        response = client.post(
            f"{BOOKINGS_URL}/{booking_id}/cancel", headers=_auth(user_id)
        )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == CANCEL_KEYS
    assert body["id"] == booking_id
    assert body["status"] == "CANCELLED"
    assert body["refundedAt"] is not None

    assert len(fake.refunds) == 1
    refund = fake.refunds[0]
    assert refund["payment_id"] == PAYMENT_ID
    assert refund["idempotency_key"] == f"cancel-{booking_id}"

    with SessionLocal() as db:
        booking = db.get(Booking, booking_id)
        assert booking is not None
        assert booking.status == "CANCELLED"
        assert booking.refunded_at is not None

        for seat_id in seat_ids:
            row = db.get(ShowSeat, seat_id)
            assert row is not None
            assert row.status == ShowSeatStatus.AVAILABLE.value
            assert row.held_by is None
            assert row.held_until is None
            assert row.hold_id is None


def test_double_cancel_returns_409(client) -> None:
    user_id = _create_user("+919000000602")
    fake = _FakePaymentsClient()
    booking_id, _ = _confirmed_booking(client, fake, user_id, 1)
    url = f"{BOOKINGS_URL}/{booking_id}/cancel"

    with _use_payments(fake):
        first = client.post(url, headers=_auth(user_id))
        second = client.post(url, headers=_auth(user_id))

    assert first.status_code == 200
    assert second.status_code == 409
    body = second.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Conflict"
    assert body["status"] == 409
    assert "CANCELLED" in body["detail"]
    assert body["instance"] == url
    assert len(fake.refunds) == 1

    with SessionLocal() as db:
        booking = db.get(Booking, booking_id)
        assert booking is not None
        assert booking.status == "CANCELLED"


def test_cancel_is_owner_only(client) -> None:
    owner_id = _create_user("+919000000603")
    stranger_id = _create_user("+919000000604")
    fake = _FakePaymentsClient()
    booking_id, seat_ids = _confirmed_booking(client, fake, owner_id, 1)

    with _use_payments(fake):
        response = client.post(
            f"{BOOKINGS_URL}/{booking_id}/cancel", headers=_auth(stranger_id)
        )

    assert response.status_code == 403
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Forbidden"
    assert body["status"] == 403
    assert body["detail"] == NOT_OWNER_DETAIL
    assert body["instance"] == f"{BOOKINGS_URL}/{booking_id}/cancel"
    assert fake.refunds == []

    with SessionLocal() as db:
        booking = db.get(Booking, booking_id)
        assert booking is not None
        assert booking.status == "CONFIRMED"
        assert booking.refunded_at is None

        row = db.get(ShowSeat, seat_ids[0])
        assert row is not None
        assert row.status == ShowSeatStatus.SOLD.value


def test_refund_failure_returns_502_and_keeps_booking_confirmed(client) -> None:
    user_id = _create_user("+919000000605")
    fake = _FakePaymentsClient(refund_outcome="unavailable")
    booking_id, seat_ids = _confirmed_booking(client, fake, user_id, 1)

    with _use_payments(fake):
        response = client.post(
            f"{BOOKINGS_URL}/{booking_id}/cancel", headers=_auth(user_id)
        )

    assert response.status_code == 502
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Bad Gateway"
    assert body["status"] == 502
    assert body["detail"] == REFUND_FAILED_DETAIL
    assert body["instance"] == f"{BOOKINGS_URL}/{booking_id}/cancel"
    assert len(fake.refunds) == 1

    with SessionLocal() as db:
        booking = db.get(Booking, booking_id)
        assert booking is not None
        assert booking.status == "CONFIRMED"
        assert booking.refunded_at is None

        for seat_id in seat_ids:
            row = db.get(ShowSeat, seat_id)
            assert row is not None
            assert row.status == ShowSeatStatus.SOLD.value
            assert row.held_by is None
            assert row.hold_id is None


def test_get_booking_exposes_cancelled_and_refunded_at(client) -> None:
    user_id = _create_user("+919000000606")
    fake = _FakePaymentsClient()
    booking_id, _ = _confirmed_booking(client, fake, user_id, 1)

    with _use_payments(fake):
        cancelled = client.post(
            f"{BOOKINGS_URL}/{booking_id}/cancel", headers=_auth(user_id)
        )
    assert cancelled.status_code == 200

    read = client.get(f"{BOOKINGS_URL}/{booking_id}", headers=_auth(user_id))

    assert read.status_code == 200
    body = read.json()
    assert body["bookingId"] == booking_id
    assert body["status"] == "CANCELLED"
    assert body["refundedAt"] is not None
    assert _as_utc(datetime.fromisoformat(body["refundedAt"])) == _as_utc(
        datetime.fromisoformat(cancelled.json()["refundedAt"])
    )


def test_cancel_pending_booking_returns_409(client) -> None:
    user_id = _create_user("+919000000607")
    fake = _FakePaymentsClient(outcome="unavailable")
    _, _, hold_id = _create_hold(client, user_id, 1)
    key = str(uuid.uuid4())

    with _use_payments(fake):
        created = client.post(
            BOOKINGS_URL,
            json={"holdId": hold_id},
            headers={**_auth(user_id), "Idempotency-Key": key},
        )
    assert created.status_code == 503

    with SessionLocal() as db:
        booking = db.scalar(select(Booking).where(Booking.idempotency_key == key))
        assert booking is not None
        assert booking.status == "PENDING_PAYMENT"
        booking_id = booking.id

    with _use_payments(fake):
        response = client.post(
            f"{BOOKINGS_URL}/{booking_id}/cancel", headers=_auth(user_id)
        )

    assert response.status_code == 409
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Conflict"
    assert body["status"] == 409
    assert "PENDING_PAYMENT" in body["detail"]
    assert body["instance"] == f"{BOOKINGS_URL}/{booking_id}/cancel"
    assert fake.refunds == []

    with SessionLocal() as db:
        booking = db.get(Booking, booking_id)
        assert booking is not None
        assert booking.status == "PENDING_PAYMENT"
        assert booking.refunded_at is None
