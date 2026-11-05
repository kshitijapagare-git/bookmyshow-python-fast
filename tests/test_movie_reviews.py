"""The reviews client and the payments lookup, exercised without a live service.

`httpx.get`/`httpx.post` are swapped for a recorder, so the outbound URL, the
`X-Service-Token` header and the body are all assertable, and a failing service is
checked to arrive as the client's own typed error rather than an httpx exception.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.bookings import PAYMENTS_UNAVAILABLE_DETAIL
from app.db import SessionLocal
from app.main import app
from app.models import Booking, Show, ShowSeat, User
from app.payments_client import (
    PaymentsClient,
    PaymentsSettings,
    PaymentsUnavailableError,
    get_payments_client,
)
from app.reviews_client import (
    DEFAULT_REVIEWS_SERVICE_TOKEN,
    DEFAULT_REVIEWS_SERVICE_URL,
    ReviewsClient,
    ReviewsSettings,
    ReviewsUnavailableError,
    get_reviews_client,
)
from app.routers.reviews import REVIEWS_UNAVAILABLE_DETAIL, REVIEW_FORBIDDEN_DETAIL
from app.schemas import ShowSeatStatus
from app.security import issue_token

REVIEWS_BASE_URL = "http://reviews.test"
PAYMENTS_BASE_URL = "http://payments.test"
SERVICE_TOKEN = "shared-token"
TOKEN_HEADER = "X-Service-Token"
REVIEW_BODY_KEYS = {"movieId", "userId", "bookingId", "rating", "comment", "verified"}
MOVIE_REVIEWS_URL = "/api/movies/1/reviews"
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
REVIEW_KEYS = {
    "id",
    "movieId",
    "userId",
    "bookingId",
    "rating",
    "comment",
    "verified",
    "createdAt",
}
BOOKINGS_URL = "/api/bookings"
PAYMENT_ID = 11
PAID_PAYMENT_STATUS = "SUCCEEDED"


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
            request=httpx.Request("GET", url),
        )


class _Unreachable:
    """Stand-in for httpx.get/httpx.post when the service cannot be reached at all."""

    def __call__(self, url, *, json=None, headers=None, timeout=None):
        raise httpx.ConnectError(
            "connection refused", request=httpx.Request("GET", url)
        )


def _reviews_client() -> ReviewsClient:
    return ReviewsClient(
        ReviewsSettings(
            base_url=REVIEWS_BASE_URL,
            service_token=SERVICE_TOKEN,
            timeout_seconds=5.0,
        )
    )


def _payments_client() -> PaymentsClient:
    return PaymentsClient(
        PaymentsSettings(
            base_url=PAYMENTS_BASE_URL,
            service_token=SERVICE_TOKEN,
            timeout_seconds=5.0,
        )
    )


def test_reviews_settings_fall_back_to_the_dev_defaults(monkeypatch) -> None:
    for name in (
        "REVIEWS_SERVICE_URL",
        "REVIEWS_SERVICE_TOKEN",
        "REVIEWS_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = ReviewsSettings.from_env()

    assert settings.base_url == DEFAULT_REVIEWS_SERVICE_URL == "http://localhost:8002"
    assert settings.service_token == DEFAULT_REVIEWS_SERVICE_TOKEN == "dev-service-token"
    assert settings.timeout_seconds == 5.0


def test_reviews_settings_read_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("REVIEWS_SERVICE_URL", "http://ratings.internal:9000/")
    monkeypatch.setenv("REVIEWS_SERVICE_TOKEN", "  s3cret  ")
    monkeypatch.setenv("REVIEWS_TIMEOUT_SECONDS", "2.5")

    settings = ReviewsSettings.from_env()

    assert settings.base_url == "http://ratings.internal:9000"
    assert settings.service_token == "s3cret"
    assert settings.timeout_seconds == 2.5


def test_list_reviews_sends_the_service_token_and_the_paging(monkeypatch) -> None:
    body = {
        "summary": {"average": 4.5, "count": 2, "distribution": {"5": 1, "4": 1}},
        "reviews": [],
        "page": 3,
        "pageSize": 5,
        "total": 2,
    }
    recorder = _Recorder(body=body)
    monkeypatch.setattr(httpx, "get", recorder)

    result = _reviews_client().list_reviews(movie_id=1, page=3, page_size=5)

    assert result == body
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["url"] == (
        f"{REVIEWS_BASE_URL}/internal/reviews?movieId=1&page=3&pageSize=5"
    )
    assert call["headers"] == {TOKEN_HEADER: SERVICE_TOKEN}


def test_create_review_reports_whether_it_created_the_review(monkeypatch) -> None:
    created_body = {
        "id": 9,
        "movieId": 1,
        "userId": 42,
        "bookingId": 501,
        "rating": 5,
        "comment": "Loved it",
        "verified": True,
        "createdAt": "2026-10-20T10:00:00+05:30",
    }
    recorder = _Recorder(status_code=201, body=created_body)
    monkeypatch.setattr(httpx, "post", recorder)

    body, created = _reviews_client().create_review(
        movie_id=1,
        user_id=42,
        booking_id=501,
        rating=5,
        comment="Loved it",
        verified=True,
    )

    assert body == created_body
    assert created is True
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["url"] == f"{REVIEWS_BASE_URL}/internal/reviews"
    assert call["headers"] == {TOKEN_HEADER: SERVICE_TOKEN}
    assert set(call["json"]) == REVIEW_BODY_KEYS
    assert call["json"] == {
        "movieId": 1,
        "userId": 42,
        "bookingId": 501,
        "rating": 5,
        "comment": "Loved it",
        "verified": True,
    }

    updated = _Recorder(status_code=200, body={**created_body, "rating": 4})
    monkeypatch.setattr(httpx, "post", updated)

    body, created = _reviews_client().create_review(
        movie_id=1,
        user_id=42,
        booking_id=None,
        rating=4,
        comment="",
        verified=True,
    )

    assert created is False
    assert body["rating"] == 4
    assert len(updated.calls) == 1
    assert updated.calls[0]["json"]["bookingId"] is None
    assert updated.calls[0]["json"]["comment"] == ""


def test_a_reviews_server_error_becomes_reviews_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "get", _Recorder(status_code=500, body={}))
    monkeypatch.setattr(httpx, "post", _Recorder(status_code=500, body={}))

    with pytest.raises(ReviewsUnavailableError):
        _reviews_client().list_reviews(movie_id=1, page=1, page_size=10)

    with pytest.raises(ReviewsUnavailableError):
        _reviews_client().create_review(
            movie_id=1,
            user_id=42,
            booking_id=501,
            rating=5,
            comment="Loved it",
            verified=True,
        )


def test_an_unreachable_reviews_service_becomes_reviews_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(httpx, "get", _Unreachable())
    monkeypatch.setattr(httpx, "post", _Unreachable())

    with pytest.raises(ReviewsUnavailableError):
        _reviews_client().list_reviews(movie_id=1, page=1, page_size=10)

    with pytest.raises(ReviewsUnavailableError):
        _reviews_client().create_review(
            movie_id=1,
            user_id=42,
            booking_id=None,
            rating=1,
            comment="",
            verified=False,
        )


def test_list_payments_asks_for_one_booking(monkeypatch) -> None:
    rows = [
        {
            "paymentId": 8,
            "bookingId": 501,
            "status": "SUCCEEDED",
            "amount": 400.0,
            "currency": "INR",
        }
    ]
    recorder = _Recorder(body=rows)
    monkeypatch.setattr(httpx, "get", recorder)

    payments = _payments_client().list_payments(booking_id=501)

    assert payments == rows
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["url"] == f"{PAYMENTS_BASE_URL}/internal/payments?bookingId=501"
    assert call["headers"] == {TOKEN_HEADER: SERVICE_TOKEN}


def test_list_payments_of_a_booking_without_payments_is_empty(monkeypatch) -> None:
    recorder = _Recorder(body=[])
    monkeypatch.setattr(httpx, "get", recorder)

    assert _payments_client().list_payments(booking_id=7) == []
    assert len(recorder.calls) == 1


def test_a_payments_server_error_becomes_payments_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "get", _Recorder(status_code=500, body={}))

    with pytest.raises(PaymentsUnavailableError):
        _payments_client().list_payments(booking_id=501)


def test_an_unreachable_payments_service_becomes_payments_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(httpx, "get", _Unreachable())

    with pytest.raises(PaymentsUnavailableError):
        _payments_client().list_payments(booking_id=501)


def test_a_non_list_payments_body_is_a_typed_error(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "get", _Recorder(body={"detail": "nope"}))

    with pytest.raises(PaymentsUnavailableError):
        _payments_client().list_payments(booking_id=501)


def _reviews_page() -> dict[str, object]:
    """One page of the ratings API's own answer, reused as the proxy's expected body."""
    return {
        "summary": {
            "average": 4.5,
            "count": 2,
            "distribution": {"1": 0, "2": 0, "3": 0, "4": 1, "5": 1},
        },
        "reviews": [
            {
                "id": 9,
                "movieId": 1,
                "userId": 42,
                "bookingId": 501,
                "rating": 5,
                "comment": "Loved it",
                "verified": True,
                "createdAt": "2026-10-20T10:00:00+05:30",
            },
            {
                "id": 8,
                "movieId": 1,
                "userId": 43,
                "bookingId": None,
                "rating": 4,
                "comment": "Good fun",
                "verified": False,
                "createdAt": "2026-10-19T10:00:00+05:30",
            },
        ],
        "page": 1,
        "pageSize": 10,
        "total": 2,
    }


class _FakeReviewsService:
    """Stand-in for ReviewsClient: answers a recorded page and records each read."""

    def __init__(
        self, body: dict[str, object] | None = None, outcome: str = "succeed"
    ) -> None:
        self.body = body if body is not None else _reviews_page()
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []
        self.submits: list[dict[str, object]] = []
        self._seen: set[tuple[int, int]] = set()

    def list_reviews(
        self, *, movie_id: int, page: int, page_size: int
    ) -> dict[str, object]:
        self.calls.append({"movie_id": movie_id, "page": page, "page_size": page_size})
        if self.outcome == "unavailable":
            raise ReviewsUnavailableError("reviews service answered 500")
        return self.body

    def create_review(
        self,
        *,
        movie_id: int,
        user_id: int,
        booking_id: int | None,
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
        if self.outcome == "unavailable":
            raise ReviewsUnavailableError("reviews service answered 500")
        created = (movie_id, user_id) not in self._seen
        self._seen.add((movie_id, user_id))
        return {
            "id": 9,
            "movieId": movie_id,
            "userId": user_id,
            "bookingId": booking_id,
            "rating": rating,
            "comment": comment,
            "verified": verified,
            "createdAt": "2026-10-20T10:00:00+05:30",
        }, created


@contextmanager
def _use_reviews(fake: _FakeReviewsService) -> Iterator[_FakeReviewsService]:
    app.dependency_overrides[get_reviews_client] = lambda: fake
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_reviews_client, None)


def test_public_review_list_proxies_to_ratings_service(client) -> None:
    fake = _FakeReviewsService()

    with _use_reviews(fake):
        response = client.get(MOVIE_REVIEWS_URL)
        unknown_movie = client.get("/api/movies/999/reviews")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"summary", "reviews", "page", "pageSize", "total"}
    assert body["summary"] == fake.body["summary"]
    assert body["summary"]["average"] == 4.5
    assert body["summary"]["count"] == 2
    assert body["summary"]["distribution"] == {
        "1": 0,
        "2": 0,
        "3": 0,
        "4": 1,
        "5": 1,
    }
    assert body["page"] == 1
    assert body["pageSize"] == 10
    assert body["total"] == 2
    assert [review["id"] for review in body["reviews"]] == [9, 8]
    assert set(body["reviews"][0]) == REVIEW_KEYS
    assert body["reviews"][0]["verified"] is True
    assert body["reviews"][1]["verified"] is False
    assert body["reviews"][1]["bookingId"] is None

    assert unknown_movie.status_code == 200
    assert fake.calls == [
        {"movie_id": 1, "page": 1, "page_size": 10},
        {"movie_id": 999, "page": 1, "page_size": 10},
    ]


def test_public_review_list_clamps_the_paging(client) -> None:
    fake = _FakeReviewsService()

    with _use_reviews(fake):
        in_range = client.get(f"{MOVIE_REVIEWS_URL}?page=3&pageSize=5")
        too_large = client.get(f"{MOVIE_REVIEWS_URL}?page=0&pageSize=999")
        too_small = client.get(f"{MOVIE_REVIEWS_URL}?page=-2&pageSize=0")

    assert in_range.status_code == 200
    assert too_large.status_code == 200
    assert too_small.status_code == 200
    assert fake.calls == [
        {"movie_id": 1, "page": 3, "page_size": 5},
        {"movie_id": 1, "page": 1, "page_size": 50},
        {"movie_id": 1, "page": 1, "page_size": 1},
    ]


def test_public_review_list_when_the_ratings_service_fails_is_503(client) -> None:
    fake = _FakeReviewsService(outcome="unavailable")

    with _use_reviews(fake):
        response = client.get(MOVIE_REVIEWS_URL)

    assert response.status_code == 503
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Service Unavailable"
    assert body["status"] == 503
    assert body["detail"] == REVIEWS_UNAVAILABLE_DETAIL
    assert body["instance"] == MOVIE_REVIEWS_URL
    assert len(fake.calls) == 1


def test_public_review_list_sends_the_service_token(client, monkeypatch) -> None:
    recorder = _Recorder(body=_reviews_page())
    monkeypatch.setattr(httpx, "get", recorder)
    monkeypatch.setattr(
        app.state,
        "reviews_settings",
        ReviewsSettings(
            base_url=REVIEWS_BASE_URL,
            service_token=SERVICE_TOKEN,
            timeout_seconds=5.0,
        ),
    )

    response = client.get(f"{MOVIE_REVIEWS_URL}?page=2&pageSize=5")

    assert response.status_code == 200
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["url"] == (
        f"{REVIEWS_BASE_URL}/internal/reviews?movieId=1&page=2&pageSize=5"
    )
    assert call["headers"] == {TOKEN_HEADER: SERVICE_TOKEN}


class _FakePaymentsClient:
    """Stand-in for PaymentsClient: settles a charge and answers the booking lookup."""

    def __init__(
        self,
        outcome: str = "succeed",
        lookup_outcome: str | None = None,
        payments: list[dict[str, object]] | None = None,
    ) -> None:
        self.outcome = outcome
        self.lookup_outcome = lookup_outcome if lookup_outcome is not None else outcome
        self.payments = payments
        self.charges: list[dict[str, object]] = []
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
        self.charges.append(
            {
                "booking_id": booking_id,
                "amount": amount,
                "idempotency_key": idempotency_key,
            }
        )
        if self.outcome == "unavailable":
            raise PaymentsUnavailableError(PAYMENTS_UNAVAILABLE_DETAIL)
        return {
            "paymentId": PAYMENT_ID,
            "bookingId": booking_id,
            "status": PAID_PAYMENT_STATUS,
            "amount": float(amount),
            "currency": currency,
            "providerRef": "mock_ref",
            "paidAt": datetime.now(timezone.utc).isoformat(),
        }

    def list_payments(self, *, booking_id: int) -> list[dict[str, object]]:
        self.lookups.append(booking_id)
        if self.lookup_outcome == "unavailable":
            raise PaymentsUnavailableError(PAYMENTS_UNAVAILABLE_DETAIL)
        if self.payments is not None:
            return self.payments
        return [
            {
                "paymentId": PAYMENT_ID,
                "bookingId": booking_id,
                "status": PAID_PAYMENT_STATUS,
                "amount": 400.0,
                "currency": "INR",
            }
        ]


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


def _create_hold(client, user_id: int, count: int = 1) -> tuple[int, str]:
    show_id, seat_ids = _available_seats(count)
    response = client.post(
        f"/api/shows/{show_id}/holds",
        json={"seatIds": seat_ids},
        headers=_auth(user_id),
    )
    assert response.status_code == 201
    return show_id, response.json()["holdId"]


def _movie_id_of(show_id: int) -> int:
    with SessionLocal() as db:
        movie_id = db.scalar(select(Show.movie_id).where(Show.id == show_id))
    assert movie_id is not None
    return movie_id


def _confirmed_booking(
    client, payments: _FakePaymentsClient, user_id: int, count: int = 1
) -> tuple[int, int]:
    """Pay for a fresh hold; returns the booking id and the movie it was booked for."""
    show_id, hold_id = _create_hold(client, user_id, count)
    with _use_payments(payments):
        response = client.post(
            BOOKINGS_URL,
            json={"holdId": hold_id},
            headers={**_auth(user_id), "Idempotency-Key": str(uuid.uuid4())},
        )
    assert response.status_code == 201
    assert response.json()["status"] == "CONFIRMED"
    return response.json()["bookingId"], _movie_id_of(show_id)


def _assert_review_forbidden(response, url: str) -> None:
    assert response.status_code == 403
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Forbidden"
    assert body["status"] == 403
    assert body["detail"] == REVIEW_FORBIDDEN_DETAIL
    assert body["instance"] == url


def test_submit_requires_confirmed_paid_booking(client) -> None:
    owner_id = _create_user("+919000000801")
    stranger_id = _create_user("+919000000802")
    payments = _FakePaymentsClient()
    reviews = _FakeReviewsService()
    booking_id, movie_id = _confirmed_booking(client, payments, owner_id, 1)
    url = f"/api/movies/{movie_id}/reviews"
    wrong_movie_url = f"/api/movies/{movie_id + 1}/reviews"
    payload = {"bookingId": booking_id, "rating": 5, "comment": "Loved it"}

    with _use_services(payments, reviews):
        stranger = client.post(url, json=payload, headers=_auth(stranger_id))
        unknown = client.post(
            url, json={**payload, "bookingId": 999999}, headers=_auth(owner_id)
        )
        wrong_movie = client.post(
            wrong_movie_url, json=payload, headers=_auth(owner_id)
        )
        created = client.post(url, json=payload, headers=_auth(owner_id))
        updated = client.post(
            url,
            json={**payload, "rating": 4, "comment": "Still good"},
            headers=_auth(owner_id),
        )

    _assert_review_forbidden(stranger, url)
    _assert_review_forbidden(unknown, url)
    _assert_review_forbidden(wrong_movie, wrong_movie_url)

    assert created.status_code == 201
    created_body = created.json()
    assert set(created_body) == REVIEW_KEYS
    assert created_body["movieId"] == movie_id
    assert created_body["userId"] == owner_id
    assert created_body["bookingId"] == booking_id
    assert created_body["rating"] == 5
    assert created_body["comment"] == "Loved it"
    assert created_body["verified"] is True

    assert updated.status_code == 200
    assert updated.json()["rating"] == 4
    assert updated.json()["comment"] == "Still good"
    assert updated.json()["verified"] is True

    assert payments.lookups == [booking_id, booking_id]
    assert reviews.submits == [
        {
            "movie_id": movie_id,
            "user_id": owner_id,
            "booking_id": booking_id,
            "rating": 5,
            "comment": "Loved it",
            "verified": True,
        },
        {
            "movie_id": movie_id,
            "user_id": owner_id,
            "booking_id": booking_id,
            "rating": 4,
            "comment": "Still good",
            "verified": True,
        },
    ]


def test_submit_requires_a_confirmed_booking(client) -> None:
    user_id = _create_user("+919000000803")
    payments = _FakePaymentsClient()
    reviews = _FakeReviewsService()
    booking_id, movie_id = _confirmed_booking(client, payments, user_id, 1)
    url = f"/api/movies/{movie_id}/reviews"
    payload = {"bookingId": booking_id, "rating": 5, "comment": "Loved it"}

    with SessionLocal() as db:
        booking = db.get(Booking, booking_id)
        assert booking is not None
        booking.status = "PENDING_PAYMENT"
        db.commit()

    with _use_services(payments, reviews):
        response = client.post(url, json=payload, headers=_auth(user_id))

    _assert_review_forbidden(response, url)
    assert payments.lookups == []
    assert reviews.submits == []


@pytest.mark.parametrize(
    "paid_rows",
    [
        [],
        [
            {"paymentId": 3, "bookingId": 501, "status": "FAILED"},
            {"paymentId": 4, "bookingId": 501, "status": "REFUNDED"},
        ],
    ],
)
def test_submit_without_a_succeeded_payment_is_403(client, paid_rows) -> None:
    user_id = _create_user("+919000000804")
    payments = _FakePaymentsClient(payments=paid_rows)
    reviews = _FakeReviewsService()
    booking_id, movie_id = _confirmed_booking(client, payments, user_id, 1)
    url = f"/api/movies/{movie_id}/reviews"
    payload = {"bookingId": booking_id, "rating": 5, "comment": "Loved it"}

    with _use_services(payments, reviews):
        response = client.post(url, json=payload, headers=_auth(user_id))

    _assert_review_forbidden(response, url)
    assert payments.lookups == [booking_id]
    assert reviews.submits == []


@pytest.mark.parametrize(
    "payload",
    [
        {"rating": 5, "comment": "Loved it"},
        {"bookingId": 0, "rating": 5, "comment": "Loved it"},
        {"bookingId": "abc", "rating": 5, "comment": "Loved it"},
        {"bookingId": 1, "rating": 0, "comment": "Loved it"},
        {"bookingId": 1, "rating": 6, "comment": "Loved it"},
        {"bookingId": 1, "rating": 5, "comment": "x" * 1001},
    ],
)
def test_submit_with_an_invalid_body_is_422(client, payload) -> None:
    user_id = _create_user("+919000000805")
    payments = _FakePaymentsClient()
    reviews = _FakeReviewsService()
    url = "/api/movies/1/reviews"

    with _use_services(payments, reviews):
        response = client.post(url, json=payload, headers=_auth(user_id))

    assert response.status_code == 422
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["status"] == 422
    assert body["instance"] == url
    assert payments.lookups == []
    assert reviews.submits == []


def test_submit_when_a_service_is_down_is_503(client) -> None:
    user_id = _create_user("+919000000806")
    payments = _FakePaymentsClient()
    reviews = _FakeReviewsService()
    booking_id, movie_id = _confirmed_booking(client, payments, user_id, 1)
    url = f"/api/movies/{movie_id}/reviews"
    payload = {"bookingId": booking_id, "rating": 5, "comment": "Loved it"}

    down_payments = _FakePaymentsClient(lookup_outcome="unavailable")
    with _use_services(down_payments, reviews):
        payments_down = client.post(url, json=payload, headers=_auth(user_id))

    assert payments_down.status_code == 503
    body = payments_down.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Service Unavailable"
    assert body["status"] == 503
    assert body["detail"] == PAYMENTS_UNAVAILABLE_DETAIL
    assert body["instance"] == url
    assert reviews.submits == []

    down_reviews = _FakeReviewsService(outcome="unavailable")
    with _use_services(payments, down_reviews):
        reviews_down = client.post(url, json=payload, headers=_auth(user_id))

    assert reviews_down.status_code == 503
    body = reviews_down.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Service Unavailable"
    assert body["status"] == 503
    assert body["detail"] == REVIEWS_UNAVAILABLE_DETAIL
    assert body["instance"] == url
    assert len(down_reviews.submits) == 1


def test_submit_requires_a_token(client) -> None:
    payments = _FakePaymentsClient()
    reviews = _FakeReviewsService()
    url = "/api/movies/1/reviews"

    with _use_services(payments, reviews):
        response = client.post(
            url, json={"bookingId": 1, "rating": 5, "comment": "Loved it"}
        )

    assert response.status_code == 401
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Unauthorized"
    assert body["status"] == 401
    assert body["detail"] == "Unauthorized"
    assert body["instance"] == url
    assert payments.charges == []
    assert payments.lookups == []
    assert reviews.submits == []

