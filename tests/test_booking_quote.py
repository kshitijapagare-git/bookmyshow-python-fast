"""Pricing a promo code against a live hold before paying for it.

The payments service's quote endpoint is faked through FastAPI's dependency override,
exactly as tests/test_bookings.py fakes the charge, so the suite never needs a live
payments service and every quote the route makes is recorded.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.bookings import (
    HOLD_EXPIRED_DETAIL,
    NOT_OWNER_DETAIL,
    PAYMENTS_UNAVAILABLE_DETAIL,
    PROMO_INVALID_DETAIL,
)
from app.db import SessionLocal
from app.main import app
from app.models import Booking, Seat, SeatCategory, ShowSeat, User
from app.payments_client import (
    PaymentsUnavailableError,
    PromoCodeInvalidError,
    get_payments_client,
)
from app.schemas import ShowSeatStatus
from app.security import issue_token

QUOTE_URL = "/api/bookings/quote"
UNAVAILABLE_DETAIL = PAYMENTS_UNAVAILABLE_DETAIL
PROMO_PERCENT = Decimal("20")
CENT = Decimal("0.01")
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
QUOTE_KEYS = {"originalAmount", "discount", "total", "promoCode"}


class _FakeQuoteClient:
    """Stand-in for PaymentsClient: prices a code at 20% off and records each quote."""

    def __init__(self, outcome: str = "succeed") -> None:
        self.outcome = outcome
        self.quotes: list[dict[str, object]] = []

    def quote_payment(
        self, *, booking_id: int, amount: Decimal, promo_code: str
    ) -> dict[str, object]:
        self.quotes.append(
            {"booking_id": booking_id, "amount": amount, "promo_code": promo_code}
        )
        if self.outcome == "invalid":
            raise PromoCodeInvalidError(PROMO_INVALID_DETAIL)
        if self.outcome == "unavailable":
            raise PaymentsUnavailableError(UNAVAILABLE_DETAIL)
        discount = (amount * PROMO_PERCENT / 100).quantize(CENT)
        return {
            "originalAmount": float(amount),
            "discount": float(discount),
            "total": float(amount - discount),
            "promoCode": promo_code.strip().upper(),
        }


@contextmanager
def _use_quote_client(fake: _FakeQuoteClient) -> Iterator[_FakeQuoteClient]:
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


def _quote(client, user_id: int, hold_id: str, promo_code: str):
    return client.post(
        QUOTE_URL,
        json={"holdId": hold_id, "promoCode": promo_code},
        headers=_auth(user_id),
    )


def _assert_no_booking() -> None:
    with SessionLocal() as db:
        assert list(db.scalars(select(Booking))) == []


def test_quote_with_valid_code_returns_the_breakdown(client) -> None:
    user_id = _create_user("+919000000701")
    _, seat_ids, hold_id = _create_hold(client, user_id, 2)
    expected_original = _expected_total(seat_ids)
    expected_discount = (expected_original * PROMO_PERCENT / 100).quantize(CENT)
    fake = _FakeQuoteClient()

    with _use_quote_client(fake):
        response = _quote(client, user_id, hold_id, " b1g1 ")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == QUOTE_KEYS
    assert body["promoCode"] == "B1G1"
    assert Decimal(str(body["originalAmount"])) == expected_original
    assert Decimal(str(body["discount"])) == expected_discount
    assert Decimal(str(body["total"])) == expected_original - expected_discount
    assert Decimal(str(body["total"])) == (
        Decimal(str(body["originalAmount"])) - Decimal(str(body["discount"]))
    )

    assert len(fake.quotes) == 1
    quote = fake.quotes[0]
    assert quote["amount"] == expected_original
    assert quote["promo_code"] == "b1g1"

    _assert_no_booking()


def test_quote_with_invalid_code_returns_400(client) -> None:
    user_id = _create_user("+919000000702")
    _, _, hold_id = _create_hold(client, user_id, 1)
    fake = _FakeQuoteClient(outcome="invalid")

    with _use_quote_client(fake):
        response = _quote(client, user_id, hold_id, "NOPE")

    assert response.status_code == 400
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Bad Request"
    assert body["status"] == 400
    assert body["detail"] == PROMO_INVALID_DETAIL
    assert body["instance"] == QUOTE_URL
    assert len(fake.quotes) == 1

    _assert_no_booking()


def test_quote_for_an_unknown_hold_returns_404(client) -> None:
    user_id = _create_user("+919000000703")
    missing_hold_id = str(uuid.uuid4())
    fake = _FakeQuoteClient()

    with _use_quote_client(fake):
        response = _quote(client, user_id, missing_hold_id, "B1G1")

    assert response.status_code == 404
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == f"Hold not found: {missing_hold_id}"
    assert body["instance"] == QUOTE_URL
    assert fake.quotes == []

    _assert_no_booking()


def test_quote_for_another_users_hold_returns_403(client) -> None:
    owner_id = _create_user("+919000000704")
    stranger_id = _create_user("+919000000705")
    _, _, hold_id = _create_hold(client, owner_id, 1)
    fake = _FakeQuoteClient()

    with _use_quote_client(fake):
        response = _quote(client, stranger_id, hold_id, "B1G1")

    assert response.status_code == 403
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Forbidden"
    assert body["status"] == 403
    assert body["detail"] == NOT_OWNER_DETAIL
    assert body["instance"] == QUOTE_URL
    assert fake.quotes == []

    _assert_no_booking()


def test_quote_for_an_expired_hold_returns_410(client) -> None:
    user_id = _create_user("+919000000706")
    _, _, hold_id = _create_hold(client, user_id, 1)

    with SessionLocal() as db:
        for row in db.scalars(select(ShowSeat).where(ShowSeat.hold_id == hold_id)):
            row.held_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()

    fake = _FakeQuoteClient()
    with _use_quote_client(fake):
        response = _quote(client, user_id, hold_id, "B1G1")

    assert response.status_code == 410
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Gone"
    assert body["status"] == 410
    assert body["detail"] == HOLD_EXPIRED_DETAIL
    assert body["instance"] == QUOTE_URL
    assert fake.quotes == []

    _assert_no_booking()


def test_quote_requires_a_token(client) -> None:
    owner_id = _create_user("+919000000707")
    _, _, hold_id = _create_hold(client, owner_id, 1)
    fake = _FakeQuoteClient()

    with _use_quote_client(fake):
        response = client.post(QUOTE_URL, json={"holdId": hold_id, "promoCode": "B1G1"})

    assert response.status_code == 401
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Unauthorized"
    assert body["status"] == 401
    assert body["detail"] == "Unauthorized"
    assert body["instance"] == QUOTE_URL
    assert fake.quotes == []

    _assert_no_booking()


def test_quote_when_payments_service_is_down_returns_503(client) -> None:
    user_id = _create_user("+919000000708")
    _, _, hold_id = _create_hold(client, user_id, 1)
    fake = _FakeQuoteClient(outcome="unavailable")

    with _use_quote_client(fake):
        response = _quote(client, user_id, hold_id, "B1G1")

    assert response.status_code == 503
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Service Unavailable"
    assert body["status"] == 503
    assert body["detail"] == PAYMENTS_UNAVAILABLE_DETAIL
    assert body["instance"] == QUOTE_URL
    assert len(fake.quotes) == 1

    _assert_no_booking()
