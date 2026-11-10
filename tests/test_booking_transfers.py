"""Transferring a confirmed booking to a friend before its show.

The payments and ratings services are both faked through FastAPI's dependency
overrides, exactly as tests/test_movie_reviews.py fakes them, so every settlement,
release and review-marking the transfer flow makes is recorded without a live
service. Expiry is evaluated lazily, so the tests move a transfer's `expires_at`
(or its show's start time) into the past rather than waiting for a clock.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.bookings import PAYMENTS_UNAVAILABLE_DETAIL
from app.db import SessionLocal
from app.main import app
from app.models import Booking, BookingTransfer, Show, ShowSeat, User
from app.payments_client import (
    PaymentDeclinedError,
    PaymentsClient,
    PaymentsSettings,
    PaymentsUnavailableError,
    get_payments_client,
)
from app.reviews_client import (
    ReviewsClient,
    ReviewsSettings,
    ReviewsUnavailableError,
    get_reviews_client,
)
from app.routers.reviews import REVIEW_FORBIDDEN_DETAIL
from app.schemas import ShowSeatStatus
from app.security import issue_token
from app.timeutil import ist_local

BOOKINGS_URL = "/api/bookings"
TRANSFERS_URL = "/api/transfers"
DECLINE_DETAIL = "Payment declined"
PAYMENT_ID = 7
SETTLEMENT_ID = 5
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
TRANSFER_KEYS = {
    "transferId",
    "transferCode",
    "bookingId",
    "fromUserId",
    "toUserId",
    "status",
    "fee",
    "createdAt",
    "expiresAt",
}
DETAIL_KEYS = {
    "transfer",
    "bookingId",
    "movieId",
    "movieTitle",
    "cinemaName",
    "showStartsAt",
    "seats",
    "reviewEligible",
}
PAYMENTS_BASE_URL = "http://payments.test"
REVIEWS_BASE_URL = "http://reviews.test"
SERVICE_TOKEN = "shared-token"
TOKEN_HEADER = "X-Service-Token"
IDEMPOTENCY_HEADER = "Idempotency-Key"


class _FakePaymentsClient:
    """Stand-in for PaymentsClient: settles transfers, releases them and pays bookings."""

    def __init__(
        self, outcome: str = "succeed", release_outcome: str | None = None
    ) -> None:
        self.outcome = outcome
        self.release_outcome = (
            release_outcome if release_outcome is not None else outcome
        )
        self.calls: list[dict[str, object]] = []
        self.settlements: list[dict[str, object]] = []
        self.releases: list[int] = []
        self.lookups: list[int] = []

    def create_payment(
        self,
        *,
        booking_id: int,
        amount: Decimal,
        currency: str = "INR",
        idempotency_key: str,
        promo_code: str | None = None,
    ) -> dict[str, object]:
        self.calls.append({"booking_id": booking_id, "amount": amount})
        return {
            "paymentId": PAYMENT_ID,
            "bookingId": booking_id,
            "status": "SUCCEEDED",
            "amount": float(amount),
            "currency": currency,
            "providerRef": "mock_ref",
            "paidAt": datetime.now(timezone.utc).isoformat(),
        }

    def settle_transfer(
        self,
        *,
        booking_id: int,
        from_user_id: int,
        to_user_id: int,
        amount: Decimal,
        fee: Decimal,
    ) -> dict[str, object]:
        self.settlements.append(
            {
                "booking_id": booking_id,
                "from_user_id": from_user_id,
                "to_user_id": to_user_id,
                "amount": amount,
                "fee": fee,
            }
        )
        if self.outcome == "decline":
            raise PaymentDeclinedError(DECLINE_DETAIL)
        if self.outcome == "unavailable":
            raise PaymentsUnavailableError(PAYMENTS_UNAVAILABLE_DETAIL)
        return {
            "transferId": SETTLEMENT_ID,
            "status": "SUCCEEDED",
            "amount": float(amount),
            "fee": float(fee),
        }

    def release_transfer(self, *, transfer_id: int) -> dict[str, object]:
        self.releases.append(transfer_id)
        if self.release_outcome == "unavailable":
            raise PaymentsUnavailableError(PAYMENTS_UNAVAILABLE_DETAIL)
        return {"transferId": transfer_id, "status": "CANCELLED"}

    def list_payments(self, *, booking_id: int) -> list[dict[str, object]]:
        self.lookups.append(booking_id)
        return [
            {
                "paymentId": PAYMENT_ID,
                "bookingId": booking_id,
                "status": "SUCCEEDED",
                "amount": 400.0,
                "currency": "INR",
            }
        ]


class _FakeReviewsService:
    """Stand-in for ReviewsClient: lists a booking's reviews and marks them moved."""

    def __init__(
        self,
        outcome: str = "succeed",
        booking_reviews: list[dict[str, object]] | None = None,
    ) -> None:
        self.outcome = outcome
        self.booking_reviews = (
            booking_reviews if booking_reviews is not None else []
        )
        self.lookups: list[tuple[int, int]] = []
        self.transfers: list[int] = []
        self.submits: list[dict[str, object]] = []

    def list_booking_reviews(
        self, *, movie_id: int, booking_id: int
    ) -> list[dict[str, object]]:
        self.lookups.append((movie_id, booking_id))
        if self.outcome == "unavailable":
            raise ReviewsUnavailableError("reviews service answered 500")
        return list(self.booking_reviews)

    def transfer_review(self, *, review_id: int) -> dict[str, object]:
        self.transfers.append(review_id)
        if self.outcome == "unavailable":
            raise ReviewsUnavailableError("reviews service answered 500")
        return {"id": review_id, "status": "TRANSFERRED"}

    def create_review(
        self,
        *,
        movie_id: int,
        user_id: int,
        booking_id: int | None,
        group_id: int | None = None,
        rating: int,
        comment: str,
        verified: bool,
    ) -> tuple[dict[str, object], bool]:
        self.submits.append(
            {
                "movie_id": movie_id,
                "user_id": user_id,
                "booking_id": booking_id,
                "rating": rating,
                "comment": comment,
                "verified": verified,
            }
        )
        body: dict[str, object] = {
            "id": 9,
            "movieId": movie_id,
            "userId": user_id,
            "bookingId": booking_id,
            "rating": rating,
            "comment": comment,
            "verified": verified,
            "status": "ACTIVE",
            "createdAt": "2026-10-20T10:00:00+05:30",
        }
        return body, True


@contextmanager
def _use_payments(fake: _FakePaymentsClient) -> Iterator[_FakePaymentsClient]:
    app.dependency_overrides[get_payments_client] = lambda: fake
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_payments_client, None)


@contextmanager
def _use_services(
    payments: _FakePaymentsClient, reviews: _FakeReviewsService
) -> Iterator[tuple[_FakePaymentsClient, _FakeReviewsService]]:
    """Override both outbound services, so a refusal can prove neither was called."""
    app.dependency_overrides[get_payments_client] = lambda: payments
    app.dependency_overrides[get_reviews_client] = lambda: reviews
    try:
        yield payments, reviews
    finally:
        app.dependency_overrides.pop(get_payments_client, None)
        app.dependency_overrides.pop(get_reviews_client, None)


class _Recorder:
    """Stand-in for httpx.get/httpx.post: records each call and answers as told."""

    def __init__(self, status_code: int = 200, body: object = None) -> None:
        self.status_code = status_code
        self.body = body
        self.calls: list[dict[str, object]] = []

    def __call__(self, url, *, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return httpx.Response(
            status_code=self.status_code,
            json=self.body,
            request=httpx.Request("POST", url),
        )


def _payments_client() -> PaymentsClient:
    return PaymentsClient(
        PaymentsSettings(
            base_url=PAYMENTS_BASE_URL,
            service_token=SERVICE_TOKEN,
            timeout_seconds=5.0,
        )
    )


def _reviews_client() -> ReviewsClient:
    return ReviewsClient(
        ReviewsSettings(
            base_url=REVIEWS_BASE_URL,
            service_token=SERVICE_TOKEN,
            timeout_seconds=5.0,
        )
    )


def _create_user(phone: str) -> int:
    with SessionLocal() as db:
        user = User(phone=phone, name=None, created_at=datetime.now(timezone.utc))
        db.add(user)
        db.commit()
        return user.id


def _auth(user_id: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(user_id=user_id)}"}


def _available_seats(count: int = 1) -> tuple[int, list[int]]:
    """A seeded show that has not started yet, with at least `count` free seats."""
    soonest = datetime.now(timezone.utc) + timedelta(hours=1)
    with SessionLocal() as db:
        rows = db.execute(
            select(ShowSeat.show_id, ShowSeat.id)
            .join(Show, Show.id == ShowSeat.show_id)
            .where(
                ShowSeat.status == ShowSeatStatus.AVAILABLE.value,
                Show.starts_at > soonest,
            )
            .order_by(ShowSeat.id)
        ).all()

    by_show: dict[int, list[int]] = {}
    for show_id, seat_id in rows:
        by_show.setdefault(show_id, []).append(seat_id)
    for show_id, seat_ids in by_show.items():
        if len(seat_ids) >= count:
            return show_id, seat_ids[:count]
    raise AssertionError("no seeded show that has not started has enough free seats")


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


def _movie_id_of(show_id: int) -> int:
    with SessionLocal() as db:
        movie_id = db.scalar(select(Show.movie_id).where(Show.id == show_id))
    assert movie_id is not None
    return movie_id


def _booking_total(booking_id: int) -> Decimal:
    with SessionLocal() as db:
        booking = db.get(Booking, booking_id)
        assert booking is not None
        return Decimal(booking.total)


def _confirmed_booking(
    client, user_id: int, count: int = 1
) -> tuple[int, int, list[int]]:
    """Pay for a fresh hold on a show that has not started; the booking, show and seats."""
    show_id, seat_ids, hold_id = _create_hold(client, user_id, count)
    fake = _FakePaymentsClient()
    with _use_payments(fake):
        response = client.post(
            BOOKINGS_URL,
            json={"holdId": hold_id},
            headers={**_auth(user_id), **_booking_key()},
        )
    assert response.status_code == 201
    assert response.json()["status"] == "CONFIRMED"
    return response.json()["bookingId"], show_id, seat_ids


def _create_transfer(client, owner_id: int, booking_id: int, recipient_id: int):
    return client.post(
        f"{BOOKINGS_URL}/{booking_id}/transfer",
        json={"toUserId": recipient_id},
        headers=_auth(owner_id),
    )


def _start_the_show(show_id: int) -> None:
    with SessionLocal() as db:
        show = db.get(Show, show_id)
        assert show is not None
        show.starts_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()


def _expire_transfer(transfer_id: int) -> None:
    with SessionLocal() as db:
        transfer = db.get(BookingTransfer, transfer_id)
        assert transfer is not None
        transfer.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()


def test_create_transfer_returns_a_pending_code(client) -> None:
    owner_id = _create_user("+919000003001")
    friend_id = _create_user("+919000003002")
    booking_id, show_id, _ = _confirmed_booking(client, owner_id, 2)

    response = _create_transfer(client, owner_id, booking_id, friend_id)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == TRANSFER_KEYS
    assert body["bookingId"] == booking_id
    assert body["fromUserId"] == owner_id
    assert body["toUserId"] == friend_id
    assert body["status"] == "PENDING"
    assert body["fee"] == 0.0
    assert body["transferCode"].startswith("TRF")
    assert len(body["transferCode"]) == 6
    assert body["expiresAt"].endswith("+05:30")

    with SessionLocal() as db:
        show = db.get(Show, show_id)
        assert show is not None
        # A transfer never outlives its show: it expires exactly when the show starts.
        assert datetime.fromisoformat(body["expiresAt"]) == ist_local(show.starts_at)
        transfer = db.get(BookingTransfer, body["transferId"])
        assert transfer is not None
        assert transfer.code == body["transferCode"]
        assert transfer.status == "PENDING"
        assert transfer.expires_at is not None

        booking = db.get(Booking, booking_id)
        assert booking is not None
        assert booking.user_id == owner_id

    # A second offer for the same booking is refused while the first is pending.
    again = _create_transfer(client, owner_id, booking_id, friend_id)

    assert again.status_code == 409
    assert set(again.json()) == PROBLEM_DETAIL_KEYS
    assert again.json()["instance"] == f"{BOOKINGS_URL}/{booking_id}/transfer"


def test_create_transfer_codes_are_unique(client) -> None:
    owner_id = _create_user("+919000003003")
    friend_id = _create_user("+919000003004")
    codes: set[str] = set()

    for _ in range(2):
        booking_id, _, _ = _confirmed_booking(client, owner_id, 1)
        response = _create_transfer(client, owner_id, booking_id, friend_id)
        assert response.status_code == 201
        codes.add(response.json()["transferCode"])

    assert len(codes) == 2


def test_get_booking_exposes_a_pending_transfer(client) -> None:
    owner_id = _create_user("+919000003046")
    friend_id = _create_user("+919000003047")
    booking_id, _, _ = _confirmed_booking(client, owner_id, 1)
    created = _create_transfer(client, owner_id, booking_id, friend_id)
    assert created.status_code == 201

    response = client.get(f"{BOOKINGS_URL}/{booking_id}", headers=_auth(owner_id))

    assert response.status_code == 200
    body = response.json()
    assert body["bookingId"] == booking_id
    transfer = body["transfer"]
    assert transfer is not None
    assert set(transfer) == TRANSFER_KEYS
    assert transfer["transferId"] == created.json()["transferId"]
    assert transfer["transferCode"] == created.json()["transferCode"]
    assert transfer["status"] == "PENDING"
    assert transfer["fee"] == 0.0
    assert transfer["expiresAt"] == created.json()["expiresAt"]

    with SessionLocal() as db:
        booking = db.get(Booking, booking_id)
        assert booking is not None
        # Offering the ticket never moves the booking on its own.
        assert booking.user_id == owner_id


def test_create_transfer_by_a_non_owner_is_403(client) -> None:
    owner_id = _create_user("+919000003005")
    stranger_id = _create_user("+919000003006")
    friend_id = _create_user("+919000003007")
    booking_id, _, _ = _confirmed_booking(client, owner_id, 1)

    response = _create_transfer(client, stranger_id, booking_id, friend_id)

    assert response.status_code == 403
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["detail"] == "This booking belongs to another user"
    assert body["instance"] == f"{BOOKINGS_URL}/{booking_id}/transfer"

    with SessionLocal() as db:
        assert list(db.scalars(select(BookingTransfer))) == []


def test_create_transfer_for_a_cancelled_booking_is_409(client) -> None:
    owner_id = _create_user("+919000003008")
    friend_id = _create_user("+919000003009")
    booking_id, _, _ = _confirmed_booking(client, owner_id, 1)

    with SessionLocal() as db:
        booking = db.get(Booking, booking_id)
        assert booking is not None
        booking.status = "CANCELLED"
        db.commit()

    response = _create_transfer(client, owner_id, booking_id, friend_id)

    assert response.status_code == 409
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert "CANCELLED" in body["detail"]

    with SessionLocal() as db:
        assert list(db.scalars(select(BookingTransfer))) == []


def test_create_transfer_after_the_show_started_is_409(client) -> None:
    owner_id = _create_user("+919000003010")
    friend_id = _create_user("+919000003011")
    booking_id, show_id, _ = _confirmed_booking(client, owner_id, 1)
    _start_the_show(show_id)

    response = _create_transfer(client, owner_id, booking_id, friend_id)

    assert response.status_code == 409
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["status"] == 409

    with SessionLocal() as db:
        assert list(db.scalars(select(BookingTransfer))) == []


def test_create_transfer_for_an_unknown_booking_is_404(client) -> None:
    owner_id = _create_user("+919000003048")
    friend_id = _create_user("+919000003049")

    response = _create_transfer(client, owner_id, 999999, friend_id)

    assert response.status_code == 404
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["detail"] == "Booking not found: 999999"
    assert body["instance"] == f"{BOOKINGS_URL}/999999/transfer"

    with SessionLocal() as db:
        assert list(db.scalars(select(BookingTransfer))) == []


def test_create_transfer_for_an_unknown_recipient_is_404(client) -> None:
    owner_id = _create_user("+919000003012")
    booking_id, _, _ = _confirmed_booking(client, owner_id, 1)

    response = _create_transfer(client, owner_id, booking_id, 999999)

    assert response.status_code == 404
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["detail"] == "User not found: 999999"

    with SessionLocal() as db:
        assert list(db.scalars(select(BookingTransfer))) == []


def test_accept_by_a_stranger_is_403_and_leaves_the_owner(client) -> None:
    owner_id = _create_user("+919000003013")
    friend_id = _create_user("+919000003014")
    stranger_id = _create_user("+919000003015")
    booking_id, _, _ = _confirmed_booking(client, owner_id, 1)
    created = _create_transfer(client, owner_id, booking_id, friend_id)
    assert created.status_code == 201
    payments = _FakePaymentsClient()
    reviews = _FakeReviewsService()

    with _use_services(payments, reviews):
        response = client.post(
            f"{TRANSFERS_URL}/{created.json()['transferCode']}/accept",
            headers=_auth(stranger_id),
        )

    assert response.status_code == 403
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["status"] == 403
    assert payments.settlements == []

    with SessionLocal() as db:
        booking = db.get(Booking, booking_id)
        assert booking is not None
        assert booking.user_id == owner_id
        transfer = db.get(BookingTransfer, created.json()["transferId"])
        assert transfer is not None
        assert transfer.status == "PENDING"


def test_accept_moves_the_booking_to_the_recipient(client) -> None:
    owner_id = _create_user("+919000003016")
    friend_id = _create_user("+919000003017")
    booking_id, show_id, _ = _confirmed_booking(client, owner_id, 2)
    created = _create_transfer(client, owner_id, booking_id, friend_id)
    assert created.status_code == 201
    code = created.json()["transferCode"]
    total = _booking_total(booking_id)
    payments = _FakePaymentsClient()
    reviews = _FakeReviewsService(
        booking_reviews=[{"id": 9, "userId": owner_id, "status": "ACTIVE"}]
    )

    with _use_services(payments, reviews):
        response = client.post(
            f"{TRANSFERS_URL}/{code}/accept", headers=_auth(friend_id)
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ACCEPTED"
    assert body["bookingId"] == booking_id
    assert body["toUserId"] == friend_id

    assert payments.settlements == [
        {
            "booking_id": booking_id,
            "from_user_id": owner_id,
            "to_user_id": friend_id,
            "amount": total,
            "fee": Decimal("0.00"),
        }
    ]
    assert reviews.lookups == [(_movie_id_of(show_id), booking_id)]
    assert reviews.transfers == [9]

    recipient_read = client.get(f"{BOOKINGS_URL}/{booking_id}", headers=_auth(friend_id))

    assert recipient_read.status_code == 200
    read_body = recipient_read.json()
    assert read_body["status"] == "CONFIRMED"
    assert read_body["transfer"]["transferCode"] == code
    assert read_body["transfer"]["status"] == "ACCEPTED"

    old_owner_read = client.get(f"{BOOKINGS_URL}/{booking_id}", headers=_auth(owner_id))

    assert old_owner_read.status_code == 403
    assert old_owner_read.json()["detail"] == "This booking belongs to another user"

    with SessionLocal() as db:
        booking = db.get(Booking, booking_id)
        assert booking is not None
        assert booking.user_id == friend_id
        transfer = db.get(BookingTransfer, body["transferId"])
        assert transfer is not None
        assert transfer.status == "ACCEPTED"
        assert transfer.accepted_at is not None
        assert transfer.payments_transfer_id == SETTLEMENT_ID


def test_accept_when_the_settlement_is_declined_keeps_it_pending(client) -> None:
    owner_id = _create_user("+919000003018")
    friend_id = _create_user("+919000003019")
    booking_id, _, _ = _confirmed_booking(client, owner_id, 1)
    created = _create_transfer(client, owner_id, booking_id, friend_id)
    assert created.status_code == 201
    payments = _FakePaymentsClient(outcome="decline")
    reviews = _FakeReviewsService()

    with _use_services(payments, reviews):
        response = client.post(
            f"{TRANSFERS_URL}/{created.json()['transferCode']}/accept",
            headers=_auth(friend_id),
        )

    assert response.status_code == 402
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Payment Required"
    assert body["detail"] == DECLINE_DETAIL

    with SessionLocal() as db:
        booking = db.get(Booking, booking_id)
        assert booking is not None
        assert booking.user_id == owner_id
        transfer = db.get(BookingTransfer, created.json()["transferId"])
        assert transfer is not None
        assert transfer.status == "PENDING"
        assert transfer.payments_transfer_id is None


def test_accept_when_payments_is_unavailable_is_503(client) -> None:
    owner_id = _create_user("+919000003020")
    friend_id = _create_user("+919000003021")
    booking_id, _, _ = _confirmed_booking(client, owner_id, 1)
    created = _create_transfer(client, owner_id, booking_id, friend_id)
    assert created.status_code == 201
    payments = _FakePaymentsClient(outcome="unavailable")
    reviews = _FakeReviewsService()

    with _use_services(payments, reviews):
        response = client.post(
            f"{TRANSFERS_URL}/{created.json()['transferCode']}/accept",
            headers=_auth(friend_id),
        )

    assert response.status_code == 503
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Service Unavailable"
    assert body["detail"] == PAYMENTS_UNAVAILABLE_DETAIL

    with SessionLocal() as db:
        booking = db.get(Booking, booking_id)
        assert booking is not None
        assert booking.user_id == owner_id
        transfer = db.get(BookingTransfer, created.json()["transferId"])
        assert transfer is not None
        assert transfer.status == "PENDING"


def test_accept_succeeds_when_the_ratings_service_is_down(client) -> None:
    owner_id = _create_user("+919000003022")
    friend_id = _create_user("+919000003023")
    booking_id, _, _ = _confirmed_booking(client, owner_id, 1)
    created = _create_transfer(client, owner_id, booking_id, friend_id)
    assert created.status_code == 201
    payments = _FakePaymentsClient()
    reviews = _FakeReviewsService(outcome="unavailable")

    with _use_services(payments, reviews):
        response = client.post(
            f"{TRANSFERS_URL}/{created.json()['transferCode']}/accept",
            headers=_auth(friend_id),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ACCEPTED"
    assert reviews.transfers == []

    with SessionLocal() as db:
        booking = db.get(Booking, booking_id)
        assert booking is not None
        assert booking.user_id == friend_id


def test_accept_an_expired_transfer_is_409_and_expires_it(client) -> None:
    owner_id = _create_user("+919000003024")
    friend_id = _create_user("+919000003025")
    booking_id, _, _ = _confirmed_booking(client, owner_id, 1)
    created = _create_transfer(client, owner_id, booking_id, friend_id)
    assert created.status_code == 201
    transfer_id = created.json()["transferId"]
    _expire_transfer(transfer_id)
    payments = _FakePaymentsClient()
    reviews = _FakeReviewsService()

    with _use_services(payments, reviews):
        response = client.post(
            f"{TRANSFERS_URL}/{created.json()['transferCode']}/accept",
            headers=_auth(friend_id),
        )

    assert response.status_code == 409
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert "EXPIRED" in body["detail"]
    assert payments.settlements == []

    with SessionLocal() as db:
        transfer = db.get(BookingTransfer, transfer_id)
        assert transfer is not None
        assert transfer.status == "EXPIRED"
        booking = db.get(Booking, booking_id)
        assert booking is not None
        assert booking.user_id == owner_id


def test_cancel_by_the_owner_cancels_the_transfer(client) -> None:
    owner_id = _create_user("+919000003026")
    friend_id = _create_user("+919000003027")
    booking_id, _, _ = _confirmed_booking(client, owner_id, 1)
    created = _create_transfer(client, owner_id, booking_id, friend_id)
    assert created.status_code == 201
    payments = _FakePaymentsClient()

    with _use_payments(payments):
        response = client.post(
            f"{TRANSFERS_URL}/{created.json()['transferId']}/cancel",
            headers=_auth(owner_id),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "CANCELLED"
    assert payments.releases == []

    with SessionLocal() as db:
        transfer = db.get(BookingTransfer, body["transferId"])
        assert transfer is not None
        assert transfer.status == "CANCELLED"
        assert transfer.cancelled_at is not None


def test_cancel_by_a_non_owner_is_403(client) -> None:
    owner_id = _create_user("+919000003028")
    friend_id = _create_user("+919000003029")
    booking_id, _, _ = _confirmed_booking(client, owner_id, 1)
    created = _create_transfer(client, owner_id, booking_id, friend_id)
    assert created.status_code == 201
    payments = _FakePaymentsClient()

    with _use_payments(payments):
        response = client.post(
            f"{TRANSFERS_URL}/{created.json()['transferId']}/cancel",
            headers=_auth(friend_id),
        )

    assert response.status_code == 403
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["instance"] == f"{TRANSFERS_URL}/{created.json()['transferId']}/cancel"

    with SessionLocal() as db:
        transfer = db.get(BookingTransfer, created.json()["transferId"])
        assert transfer is not None
        assert transfer.status == "PENDING"


def test_cancel_an_accepted_transfer_is_409(client) -> None:
    owner_id = _create_user("+919000003030")
    friend_id = _create_user("+919000003031")
    booking_id, _, _ = _confirmed_booking(client, owner_id, 1)
    created = _create_transfer(client, owner_id, booking_id, friend_id)
    assert created.status_code == 201
    payments = _FakePaymentsClient()
    reviews = _FakeReviewsService()

    with _use_services(payments, reviews):
        accepted = client.post(
            f"{TRANSFERS_URL}/{created.json()['transferCode']}/accept",
            headers=_auth(friend_id),
        )
    assert accepted.status_code == 200

    with _use_payments(payments):
        response = client.post(
            f"{TRANSFERS_URL}/{created.json()['transferId']}/cancel",
            headers=_auth(owner_id),
        )

    assert response.status_code == 409
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert "ACCEPTED" in body["detail"]
    assert payments.releases == []


def test_cancel_an_expired_transfer_is_409_and_expires_it(client) -> None:
    owner_id = _create_user("+919000003032")
    friend_id = _create_user("+919000003033")
    booking_id, _, _ = _confirmed_booking(client, owner_id, 1)
    created = _create_transfer(client, owner_id, booking_id, friend_id)
    assert created.status_code == 201
    transfer_id = created.json()["transferId"]
    _expire_transfer(transfer_id)
    payments = _FakePaymentsClient()

    with _use_payments(payments):
        response = client.post(
            f"{TRANSFERS_URL}/{transfer_id}/cancel", headers=_auth(owner_id)
        )

    assert response.status_code == 409
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert "EXPIRED" in body["detail"]

    with SessionLocal() as db:
        transfer = db.get(BookingTransfer, transfer_id)
        assert transfer is not None
        assert transfer.status == "EXPIRED"
        assert transfer.cancelled_at is None


def test_cancel_releases_a_settlement_that_exists(client) -> None:
    owner_id = _create_user("+919000003034")
    friend_id = _create_user("+919000003035")
    booking_id, _, _ = _confirmed_booking(client, owner_id, 1)
    created = _create_transfer(client, owner_id, booking_id, friend_id)
    assert created.status_code == 201
    transfer_id = created.json()["transferId"]

    with SessionLocal() as db:
        transfer = db.get(BookingTransfer, transfer_id)
        assert transfer is not None
        transfer.payments_transfer_id = SETTLEMENT_ID
        db.commit()

    payments = _FakePaymentsClient()
    with _use_payments(payments):
        response = client.post(
            f"{TRANSFERS_URL}/{transfer_id}/cancel", headers=_auth(owner_id)
        )

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert payments.releases == [SETTLEMENT_ID]


def test_cancel_when_the_release_fails_is_503_and_keeps_it_pending(client) -> None:
    owner_id = _create_user("+919000003036")
    friend_id = _create_user("+919000003037")
    booking_id, _, _ = _confirmed_booking(client, owner_id, 1)
    created = _create_transfer(client, owner_id, booking_id, friend_id)
    assert created.status_code == 201
    transfer_id = created.json()["transferId"]

    with SessionLocal() as db:
        transfer = db.get(BookingTransfer, transfer_id)
        assert transfer is not None
        transfer.payments_transfer_id = SETTLEMENT_ID
        db.commit()

    payments = _FakePaymentsClient(release_outcome="unavailable")
    with _use_payments(payments):
        response = client.post(
            f"{TRANSFERS_URL}/{transfer_id}/cancel", headers=_auth(owner_id)
        )

    assert response.status_code == 503
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["detail"] == PAYMENTS_UNAVAILABLE_DETAIL
    assert payments.releases == [SETTLEMENT_ID]

    with SessionLocal() as db:
        transfer = db.get(BookingTransfer, transfer_id)
        assert transfer is not None
        assert transfer.status == "PENDING"
        assert transfer.cancelled_at is None


def test_review_ownership_follows_the_recipient(client) -> None:
    owner_id = _create_user("+919000003038")
    friend_id = _create_user("+919000003039")
    booking_id, show_id, _ = _confirmed_booking(client, owner_id, 1)
    movie_id = _movie_id_of(show_id)
    created = _create_transfer(client, owner_id, booking_id, friend_id)
    assert created.status_code == 201
    payments = _FakePaymentsClient()
    reviews = _FakeReviewsService()
    url = f"/api/movies/{movie_id}/reviews"

    with _use_services(payments, reviews):
        accepted = client.post(
            f"{TRANSFERS_URL}/{created.json()['transferCode']}/accept",
            headers=_auth(friend_id),
        )
        assert accepted.status_code == 200

        old_owner = client.post(
            url,
            json={"bookingId": booking_id, "rating": 5, "comment": "Loved it"},
            headers=_auth(owner_id),
        )
        recipient = client.post(
            url,
            json={"bookingId": booking_id, "rating": 4, "comment": "Good fun"},
            headers=_auth(friend_id),
        )

    assert old_owner.status_code == 403
    assert old_owner.json()["detail"] == REVIEW_FORBIDDEN_DETAIL
    assert recipient.status_code == 201
    assert recipient.json()["userId"] == friend_id
    assert recipient.json()["bookingId"] == booking_id
    assert reviews.submits == [
        {
            "movie_id": movie_id,
            "user_id": friend_id,
            "booking_id": booking_id,
            "rating": 4,
            "comment": "Good fun",
            "verified": True,
        }
    ]


def test_transfer_detail_is_readable_by_both_parties(client) -> None:
    owner_id = _create_user("+919000003040")
    friend_id = _create_user("+919000003041")
    stranger_id = _create_user("+919000003042")
    booking_id, show_id, seat_ids = _confirmed_booking(client, owner_id, 2)
    created = _create_transfer(client, owner_id, booking_id, friend_id)
    assert created.status_code == 201
    code = created.json()["transferCode"]
    url = f"{TRANSFERS_URL}/{code}"

    sender_view = client.get(url, headers=_auth(owner_id))

    assert sender_view.status_code == 200
    body = sender_view.json()
    assert set(body) == DETAIL_KEYS
    assert body["bookingId"] == booking_id
    assert body["movieId"] == _movie_id_of(show_id)
    assert body["movieTitle"]
    assert body["cinemaName"]
    assert body["showStartsAt"].endswith("+05:30")
    assert [seat["id"] for seat in body["seats"]] == seat_ids
    assert all(seat["label"] for seat in body["seats"])
    assert body["reviewEligible"] is False
    assert body["transfer"]["transferCode"] == code
    assert body["transfer"]["status"] == "PENDING"

    recipient_view = client.get(url, headers=_auth(friend_id))

    assert recipient_view.status_code == 200
    assert recipient_view.json()["movieTitle"] == body["movieTitle"]

    stranger_view = client.get(url, headers=_auth(stranger_id))

    assert stranger_view.status_code == 403
    assert set(stranger_view.json()) == PROBLEM_DETAIL_KEYS


def test_transfer_detail_of_an_unknown_code_is_404(client) -> None:
    user_id = _create_user("+919000003043")

    response = client.get(f"{TRANSFERS_URL}/TRFZZZ", headers=_auth(user_id))

    assert response.status_code == 404
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["detail"] == "Transfer not found: TRFZZZ"


def test_transfer_routes_require_a_token(client) -> None:
    owner_id = _create_user("+919000003044")
    friend_id = _create_user("+919000003045")
    booking_id, _, _ = _confirmed_booking(client, owner_id, 1)

    created = client.post(
        f"{BOOKINGS_URL}/{booking_id}/transfer", json={"toUserId": friend_id}
    )

    assert created.status_code == 401
    body = created.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Unauthorized"
    assert body["instance"] == f"{BOOKINGS_URL}/{booking_id}/transfer"

    with SessionLocal() as db:
        assert list(db.scalars(select(BookingTransfer))) == []


def test_settle_transfer_sends_the_ticket_idempotency_key(monkeypatch) -> None:
    settlement = {"transferId": 5, "status": "SUCCEEDED", "amount": 400.0, "fee": 0.0}
    recorder = _Recorder(body=settlement)
    monkeypatch.setattr(httpx, "post", recorder)

    body = _payments_client().settle_transfer(
        booking_id=42,
        from_user_id=42,
        to_user_id=77,
        amount=Decimal("400.00"),
        fee=Decimal("0.00"),
    )

    assert body == settlement
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["url"] == f"{PAYMENTS_BASE_URL}/internal/transfers"
    assert call["headers"] == {
        TOKEN_HEADER: SERVICE_TOKEN,
        IDEMPOTENCY_HEADER: "transfer:42",
    }
    assert call["json"] == {
        "bookingId": 42,
        "fromUserId": 42,
        "toUserId": 77,
        "amount": 400.0,
        "fee": 0.0,
    }


def test_settle_transfer_decline_raises_payment_declined(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "post", _Recorder(status_code=402, body={}))

    with pytest.raises(PaymentDeclinedError):
        _payments_client().settle_transfer(
            booking_id=42,
            from_user_id=42,
            to_user_id=77,
            amount=Decimal("400.00"),
            fee=Decimal("0.00"),
        )


def test_release_transfer_posts_to_the_transfer_cancel_route(monkeypatch) -> None:
    released = {"transferId": 5, "status": "CANCELLED"}
    recorder = _Recorder(body=released)
    monkeypatch.setattr(httpx, "post", recorder)

    body = _payments_client().release_transfer(transfer_id=5)

    assert body == released
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["url"] == f"{PAYMENTS_BASE_URL}/internal/transfers/5/cancel"
    assert call["headers"] == {TOKEN_HEADER: SERVICE_TOKEN}


def test_release_transfer_failure_raises_payments_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "post", _Recorder(status_code=500, body={}))

    with pytest.raises(PaymentsUnavailableError):
        _payments_client().release_transfer(transfer_id=5)


def test_list_booking_reviews_asks_for_one_booking(monkeypatch) -> None:
    rows = [{"id": 9, "userId": 42, "bookingId": 501, "status": "ACTIVE"}]
    recorder = _Recorder(body={"reviews": rows, "page": 1, "pageSize": 10, "total": 1})
    monkeypatch.setattr(httpx, "get", recorder)

    reviews = _reviews_client().list_booking_reviews(movie_id=1, booking_id=501)

    assert reviews == rows
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["url"] == (
        f"{REVIEWS_BASE_URL}/internal/reviews?movieId=1&bookingId=501"
    )
    assert call["headers"] == {TOKEN_HEADER: SERVICE_TOKEN}


def test_transfer_review_posts_and_tolerates_an_unknown_review(monkeypatch) -> None:
    moved = {"id": 9, "status": "TRANSFERRED"}
    recorder = _Recorder(body=moved)
    monkeypatch.setattr(httpx, "post", recorder)

    body = _reviews_client().transfer_review(review_id=9)

    assert body == moved
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["url"] == f"{REVIEWS_BASE_URL}/internal/reviews/9/transfer"
    assert call["headers"] == {TOKEN_HEADER: SERVICE_TOKEN}

    missing = _Recorder(status_code=404, body={"detail": "Not found"})
    monkeypatch.setattr(httpx, "post", missing)

    assert _reviews_client().transfer_review(review_id=10) == {}


def test_transfer_detail_unlocks_the_review_for_the_new_owner(client) -> None:
    owner_id = _create_user("+919000003050")
    friend_id = _create_user("+919000003051")
    booking_id, show_id, _ = _confirmed_booking(client, owner_id, 1)
    created = _create_transfer(client, owner_id, booking_id, friend_id)
    assert created.status_code == 201
    code = created.json()["transferCode"]
    payments = _FakePaymentsClient()
    reviews = _FakeReviewsService()

    with _use_services(payments, reviews):
        accepted = client.post(
            f"{TRANSFERS_URL}/{code}/accept", headers=_auth(friend_id)
        )
    assert accepted.status_code == 200

    # Before the show the reward is still locked for the moviegoer who now holds it.
    before_the_show = client.get(f"{TRANSFERS_URL}/{code}", headers=_auth(friend_id))

    assert before_the_show.status_code == 200
    assert before_the_show.json()["reviewEligible"] is False

    _start_the_show(show_id)

    recipient_view = client.get(f"{TRANSFERS_URL}/{code}", headers=_auth(friend_id))

    assert recipient_view.status_code == 200
    assert recipient_view.json()["reviewEligible"] is True

    # The moviegoer who gave the ticket away no longer owns its review.
    sender_view = client.get(f"{TRANSFERS_URL}/{code}", headers=_auth(owner_id))

    assert sender_view.status_code == 200
    assert sender_view.json()["reviewEligible"] is False
