"""Group reviews: the review-eligibility gate and submitting a review as a member.

A group review is gated by the caller's own paid share of a CONFIRMED group rather
than by a booking, so the payments and ratings services are both faked through
FastAPI's dependency overrides, exactly as tests/test_movie_reviews.py fakes them,
and every forwarded review is assertable without a live service.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.bookings import PAYMENTS_UNAVAILABLE_DETAIL
from app.db import SessionLocal
from app.groups import NOT_A_MEMBER_DETAIL
from app.main import app
from app.models import Booking, Group, GroupMember, Show, ShowSeat, User
from app.payments_client import PaymentsUnavailableError, get_payments_client
from app.reviews_client import ReviewsUnavailableError, get_reviews_client
from app.routers.reviews import REVIEWS_UNAVAILABLE_DETAIL, REVIEW_FORBIDDEN_DETAIL
from app.schemas import MemberPaymentStatus, ShowSeatStatus
from app.security import issue_token

GROUPS_URL = "/api/groups"
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
ELIGIBILITY_KEYS = {"eligible", "movieId", "showStartsAt"}
PAYMENT_FIRST_ID = 601


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


def _create_group(client, user_id: int, count: int = 1) -> dict:
    """Hold `count` seats and open a group on it, returning the created group body."""
    show_id, seat_ids = _available_seats(count)
    hold = client.post(
        f"/api/shows/{show_id}/holds",
        json={"seatIds": seat_ids},
        headers=_auth(user_id),
    )
    assert hold.status_code == 201
    response = client.post(
        GROUPS_URL, json={"holdId": hold.json()["holdId"]}, headers=_auth(user_id)
    )
    assert response.status_code == 201
    body = response.json()
    assert body["showId"] == show_id
    assert sorted(body["seatIds"]) == sorted(seat_ids)
    return body


def _join(client, user_id: int, invite_code: str):
    return client.post(
        f"{GROUPS_URL}/join", json={"inviteCode": invite_code}, headers=_auth(user_id)
    )


def _pay(client, user_id: int, group_id: int):
    return client.post(f"{GROUPS_URL}/{group_id}/pay", headers=_auth(user_id))


def _member_of(body: dict, user_id: int) -> dict:
    for member in body["members"]:
        if member["userId"] == user_id:
            return member
    raise AssertionError(f"user {user_id} is missing from the group")


def _movie_id_of(show_id: int) -> int:
    with SessionLocal() as db:
        movie_id = db.scalar(select(Show.movie_id).where(Show.id == show_id))
    assert movie_id is not None
    return movie_id


class _FakePaymentsClient:
    """Stand-in for PaymentsClient: settles each share and answers the booking lookup."""

    def __init__(self, outcome: str = "succeed") -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []
        self.lookups: list[int] = []
        self.payments: dict[tuple[int, int], dict[str, object]] = {}

    def create_group_share(
        self, *, group_id: int, user_id: int, amount: Decimal, currency: str = "INR"
    ) -> dict[str, object]:
        self.calls.append(
            {
                "group_id": group_id,
                "user_id": user_id,
                "amount": amount,
                "currency": currency,
            }
        )
        if self.outcome == "unavailable":
            raise PaymentsUnavailableError(PAYMENTS_UNAVAILABLE_DETAIL)
        key = (group_id, user_id)
        if key not in self.payments:
            self.payments[key] = {
                "paymentId": PAYMENT_FIRST_ID + len(self.payments),
                "groupId": group_id,
                "userId": user_id,
                "status": "SUCCEEDED",
                "amount": float(amount),
                "currency": currency,
            }
        return self.payments[key]

    def refund_payment(
        self, *, payment_id: int, idempotency_key: str
    ) -> dict[str, object]:
        return {
            "paymentId": payment_id,
            "status": "REFUNDED",
            "refundedAt": datetime.now(timezone.utc).isoformat(),
        }

    def list_payments(self, *, booking_id: int) -> list[dict[str, object]]:
        self.lookups.append(booking_id)
        if self.outcome == "unavailable":
            raise PaymentsUnavailableError(PAYMENTS_UNAVAILABLE_DETAIL)
        return [
            {
                "paymentId": 700,
                "bookingId": booking_id,
                "status": "SUCCEEDED",
                "amount": 400.0,
                "currency": "INR",
            }
        ]


class _FakeReviewsService:
    """Stand-in for ReviewsClient: records each submitted review and answers 201."""

    def __init__(self, outcome: str = "succeed") -> None:
        self.outcome = outcome
        self.submits: list[dict[str, object]] = []

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
                "group_id": group_id,
                "rating": rating,
                "comment": comment,
                "verified": verified,
            }
        )
        if self.outcome == "unavailable":
            raise ReviewsUnavailableError("reviews service answered 500")
        body: dict[str, object] = {
            "id": 9,
            "movieId": movie_id,
            "userId": user_id,
            "bookingId": booking_id,
            "rating": rating,
            "comment": comment,
            "verified": verified,
            "createdAt": "2026-10-20T10:00:00+05:30",
        }
        if group_id is not None:
            body["groupId"] = group_id
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


def _confirmed_group(client, owner_id: int, joiner_id: int) -> dict:
    """A group on two held seats, joined and fully paid by both members."""
    created = _create_group(client, owner_id, 2)
    joined = _join(client, joiner_id, created["inviteCode"])
    assert joined.status_code == 200
    fake = _FakePaymentsClient()
    with _use_payments(fake):
        assert _pay(client, owner_id, created["groupId"]).status_code == 200
        confirmed = _pay(client, joiner_id, created["groupId"])
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "CONFIRMED"
    return created


def test_review_eligibility_is_true_for_paid_members_of_a_confirmed_group(
    client,
) -> None:
    owner_id = _create_user("+919000002001")
    joiner_id = _create_user("+919000002002")
    created = _confirmed_group(client, owner_id, joiner_id)
    url = f"{GROUPS_URL}/{created['groupId']}/review-eligibility"

    response = client.get(url, headers=_auth(joiner_id))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == ELIGIBILITY_KEYS
    assert body["eligible"] is True
    assert body["movieId"] == _movie_id_of(created["showId"])
    starts_at = datetime.fromisoformat(body["showStartsAt"])
    assert starts_at.utcoffset() == timedelta(hours=5, minutes=30)

    owner_view = client.get(url, headers=_auth(owner_id))

    assert owner_view.status_code == 200
    assert owner_view.json()["eligible"] is True
    assert owner_view.json()["movieId"] == body["movieId"]


def test_review_eligibility_is_false_while_the_group_is_pending(client) -> None:
    owner_id = _create_user("+919000002003")
    created = _create_group(client, owner_id, 2)
    fake = _FakePaymentsClient()

    with _use_payments(fake):
        paid = _pay(client, owner_id, created["groupId"])

    assert paid.status_code == 200
    assert paid.json()["status"] == "PENDING_PAYMENT"
    assert _member_of(paid.json(), owner_id)["paymentStatus"] == "PAID"

    response = client.get(
        f"{GROUPS_URL}/{created['groupId']}/review-eligibility",
        headers=_auth(owner_id),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["eligible"] is False
    assert body["movieId"] == _movie_id_of(created["showId"])


def test_review_eligibility_is_false_for_a_pending_share_of_a_confirmed_group(
    client,
) -> None:
    owner_id = _create_user("+919000002004")
    joiner_id = _create_user("+919000002005")
    created = _confirmed_group(client, owner_id, joiner_id)
    url = f"{GROUPS_URL}/{created['groupId']}/review-eligibility"

    with SessionLocal() as db:
        member = db.scalar(
            select(GroupMember).where(
                GroupMember.group_id == created["groupId"],
                GroupMember.user_id == joiner_id,
            )
        )
        assert member is not None
        member.payment_status = MemberPaymentStatus.PENDING.value
        db.commit()

        group = db.get(Group, created["groupId"])
        assert group is not None
        assert group.status == "CONFIRMED"

    response = client.get(url, headers=_auth(joiner_id))

    assert response.status_code == 200
    body = response.json()
    assert body["eligible"] is False
    assert body["movieId"] == _movie_id_of(created["showId"])

    owner_view = client.get(url, headers=_auth(owner_id))

    assert owner_view.status_code == 200
    assert owner_view.json()["eligible"] is True


def test_review_eligibility_for_a_non_member_is_403(client) -> None:
    owner_id = _create_user("+919000002006")
    stranger_id = _create_user("+919000002007")
    created = _confirmed_group(client, owner_id, _create_user("+919000002008"))
    url = f"{GROUPS_URL}/{created['groupId']}/review-eligibility"

    response = client.get(url, headers=_auth(stranger_id))

    assert response.status_code == 403
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["title"] == "Forbidden"
    assert body["status"] == 403
    assert body["detail"] == NOT_A_MEMBER_DETAIL
    assert body["instance"] == url


def test_review_eligibility_for_an_unknown_group_is_404(client) -> None:
    user_id = _create_user("+919000002009")

    response = client.get(
        f"{GROUPS_URL}/999999/review-eligibility", headers=_auth(user_id)
    )

    assert response.status_code == 404
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == "Group not found: 999999"
    assert body["instance"] == f"{GROUPS_URL}/999999/review-eligibility"


def test_review_eligibility_requires_a_token(client) -> None:
    owner_id = _create_user("+919000002010")
    created = _create_group(client, owner_id, 1)

    response = client.get(f"{GROUPS_URL}/{created['groupId']}/review-eligibility")

    assert response.status_code == 401
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Unauthorized"
    assert body["status"] == 401
    assert body["detail"] == "Unauthorized"


def test_a_paid_members_group_review_is_verified_and_carries_the_group(client) -> None:
    owner_id = _create_user("+919000002011")
    joiner_id = _create_user("+919000002012")
    created = _confirmed_group(client, owner_id, joiner_id)
    movie_id = _movie_id_of(created["showId"])
    url = f"/api/movies/{movie_id}/reviews"
    payments = _FakePaymentsClient()
    reviews = _FakeReviewsService()

    with _use_services(payments, reviews):
        response = client.post(
            url,
            json={"groupId": created["groupId"], "rating": 5, "comment": "Loved it"},
            headers=_auth(joiner_id),
        )

    assert response.status_code == 201
    body = response.json()
    assert body["movieId"] == movie_id
    assert body["userId"] == joiner_id
    assert body["groupId"] == created["groupId"]
    assert body["bookingId"] is None
    assert body["rating"] == 5
    assert body["comment"] == "Loved it"
    assert body["verified"] is True

    assert reviews.submits == [
        {
            "movie_id": movie_id,
            "user_id": joiner_id,
            "booking_id": None,
            "group_id": created["groupId"],
            "rating": 5,
            "comment": "Loved it",
            "verified": True,
        }
    ]
    assert payments.lookups == []


def test_an_unpaid_members_group_review_is_403(client) -> None:
    owner_id = _create_user("+919000002013")
    joiner_id = _create_user("+919000002014")
    created = _create_group(client, owner_id, 2)
    assert _join(client, joiner_id, created["inviteCode"]).status_code == 200
    movie_id = _movie_id_of(created["showId"])
    url = f"/api/movies/{movie_id}/reviews"
    payments = _FakePaymentsClient()
    reviews = _FakeReviewsService()

    with _use_services(payments, reviews):
        response = client.post(
            url,
            json={"groupId": created["groupId"], "rating": 5, "comment": "Loved it"},
            headers=_auth(joiner_id),
        )

    assert response.status_code == 403
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Forbidden"
    assert body["status"] == 403
    assert body["detail"] == REVIEW_FORBIDDEN_DETAIL
    assert body["instance"] == url
    assert reviews.submits == []
    assert payments.lookups == []


def test_a_non_members_group_review_is_403(client) -> None:
    owner_id = _create_user("+919000002015")
    stranger_id = _create_user("+919000002016")
    created = _confirmed_group(client, owner_id, _create_user("+919000002017"))
    movie_id = _movie_id_of(created["showId"])
    url = f"/api/movies/{movie_id}/reviews"
    payments = _FakePaymentsClient()
    reviews = _FakeReviewsService()

    with _use_services(payments, reviews):
        response = client.post(
            url,
            json={"groupId": created["groupId"], "rating": 4, "comment": "Fine"},
            headers=_auth(stranger_id),
        )

    assert response.status_code == 403
    assert response.json()["detail"] == REVIEW_FORBIDDEN_DETAIL
    assert response.json()["instance"] == url
    assert reviews.submits == []


def test_a_group_review_for_another_movie_is_403(client) -> None:
    owner_id = _create_user("+919000002018")
    created = _confirmed_group(client, owner_id, _create_user("+919000002019"))
    movie_id = _movie_id_of(created["showId"])
    wrong_url = f"/api/movies/{movie_id + 1}/reviews"
    payments = _FakePaymentsClient()
    reviews = _FakeReviewsService()

    with _use_services(payments, reviews):
        response = client.post(
            wrong_url,
            json={"groupId": created["groupId"], "rating": 5, "comment": "Loved it"},
            headers=_auth(owner_id),
        )

    assert response.status_code == 403
    body = response.json()
    assert body["detail"] == REVIEW_FORBIDDEN_DETAIL
    assert body["instance"] == wrong_url
    assert reviews.submits == []


def test_a_group_review_of_a_cancelled_group_is_403(client) -> None:
    owner_id = _create_user("+919000002020")
    joiner_id = _create_user("+919000002021")
    created = _confirmed_group(client, owner_id, joiner_id)
    movie_id = _movie_id_of(created["showId"])
    payments = _FakePaymentsClient()
    reviews = _FakeReviewsService()

    with _use_services(payments, reviews):
        cancelled = client.post(
            f"{GROUPS_URL}/{created['groupId']}/cancel", headers=_auth(owner_id)
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "CANCELLED"

        response = client.post(
            f"/api/movies/{movie_id}/reviews",
            json={"groupId": created["groupId"], "rating": 5, "comment": "Loved it"},
            headers=_auth(joiner_id),
        )

    assert response.status_code == 403
    assert response.json()["detail"] == REVIEW_FORBIDDEN_DETAIL
    assert reviews.submits == []


def test_a_group_review_needs_exactly_one_source(client) -> None:
    owner_id = _create_user("+919000002022")
    created = _confirmed_group(client, owner_id, _create_user("+919000002023"))
    movie_id = _movie_id_of(created["showId"])
    url = f"/api/movies/{movie_id}/reviews"
    payments = _FakePaymentsClient()
    reviews = _FakeReviewsService()

    with _use_services(payments, reviews):
        neither = client.post(
            url, json={"rating": 5, "comment": "Loved it"}, headers=_auth(owner_id)
        )
        both = client.post(
            url,
            json={
                "bookingId": 1,
                "groupId": created["groupId"],
                "rating": 5,
                "comment": "Loved it",
            },
            headers=_auth(owner_id),
        )

    for response in (neither, both):
        assert response.status_code == 422
        body = response.json()
        assert set(body) == PROBLEM_DETAIL_KEYS
        assert body["type"] == "about:blank"
        assert body["status"] == 422
        assert body["instance"] == url

    assert reviews.submits == []
    assert payments.lookups == []


def test_a_group_review_when_the_reviews_service_is_down_is_503(client) -> None:
    owner_id = _create_user("+919000002024")
    created = _confirmed_group(client, owner_id, _create_user("+919000002025"))
    movie_id = _movie_id_of(created["showId"])
    url = f"/api/movies/{movie_id}/reviews"
    payments = _FakePaymentsClient()
    reviews = _FakeReviewsService(outcome="unavailable")

    with _use_services(payments, reviews):
        response = client.post(
            url,
            json={"groupId": created["groupId"], "rating": 5, "comment": "Loved it"},
            headers=_auth(owner_id),
        )

    assert response.status_code == 503
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Service Unavailable"
    assert body["status"] == 503
    assert body["detail"] == REVIEWS_UNAVAILABLE_DETAIL
    assert body["instance"] == url
    assert len(reviews.submits) == 1


def _confirmed_booking_row(user_id: int) -> tuple[int, int]:
    """A CONFIRMED booking row for the user, plus the movie it was booked for."""
    show_id, seat_ids = _available_seats(1)
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
        movie_id = db.scalar(select(Show.movie_id).where(Show.id == show_id))
    assert movie_id is not None
    return booking.id, movie_id


def test_the_booking_backed_review_path_is_unchanged(client) -> None:
    user_id = _create_user("+919000002026")
    booking_id, movie_id = _confirmed_booking_row(user_id)
    url = f"/api/movies/{movie_id}/reviews"
    payments = _FakePaymentsClient()
    reviews = _FakeReviewsService()

    with _use_services(payments, reviews):
        response = client.post(
            url,
            json={"bookingId": booking_id, "rating": 4, "comment": "Good fun"},
            headers=_auth(user_id),
        )

    assert response.status_code == 201
    body = response.json()
    assert body["bookingId"] == booking_id
    assert body["groupId"] is None
    assert body["verified"] is True
    assert payments.lookups == [booking_id]
    assert reviews.submits == [
        {
            "movie_id": movie_id,
            "user_id": user_id,
            "booking_id": booking_id,
            "group_id": None,
            "rating": 4,
            "comment": "Good fun",
            "verified": True,
        }
    ]
