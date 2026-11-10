"""Creating and reading a group booking: the invite code, the members and the split.

A group is opened from a live hold and priced seat by seat, so the split assertions
sum the seat-category prices of the seeded screens rather than hard-coding amounts.
The payments service is faked through FastAPI's dependency override, exactly as
tests/test_bookings.py fakes the charge, so every share and every refund is recorded
for the "one charge per member" and "one refund per paid share" assertions.
"""

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

import app.schemas as schemas_module
from app.bookings import PAYMENTS_UNAVAILABLE_DETAIL
from app.db import SessionLocal
from app.groups import (
    ALREADY_MEMBER_DETAIL,
    GROUP_FULL_DETAIL,
    INVITE_CODE_LENGTH,
    NOT_A_MEMBER_DETAIL,
    NOT_GROUP_OWNER_DETAIL,
)
from app.main import app
from app.models import Booking, Group, GroupMember, Seat, SeatCategory, ShowSeat, User
from app.payments_client import (
    PaymentsClient,
    PaymentsSettings,
    PaymentsUnavailableError,
    get_payments_client,
)
from app.schemas import BookingStatus, MemberPaymentStatus, ShowSeatStatus
from app.security import issue_token

GROUPS_URL = "/api/groups"
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
GROUP_KEYS = {
    "groupId",
    "inviteCode",
    "status",
    "showId",
    "ownerId",
    "seatIds",
    "expiresAt",
    "total",
    "members",
}
MEMBER_KEYS = {"userId", "seatIds", "seats", "shareAmount", "paymentStatus"}
CENT = Decimal("0.01")
ALLOWED_GROUP_STATUSES = {"PENDING_PAYMENT", "CONFIRMED", "CANCELLED"}
SCHEMAS_SOURCE = Path(str(schemas_module.__file__)).read_text(encoding="utf-8")


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


def _create_hold(client, user_id: int, count: int = 1) -> tuple[int, list[int], dict]:
    """Hold `count` free seats; returns the show id, the seat ids and the hold body."""
    show_id, seat_ids = _available_seats(count)
    response = client.post(
        f"/api/shows/{show_id}/holds",
        json={"seatIds": seat_ids},
        headers=_auth(user_id),
    )
    assert response.status_code == 201
    return show_id, seat_ids, response.json()


def _seat_prices(seat_ids: list[int]) -> dict[int, Decimal]:
    """Each ShowSeat's seat-category price, exactly as the group split computes it."""
    with SessionLocal() as db:
        rows = db.execute(
            select(ShowSeat.id, SeatCategory.price)
            .join(Seat, ShowSeat.seat_id == Seat.id)
            .join(SeatCategory, Seat.category_id == SeatCategory.id)
            .where(ShowSeat.id.in_(seat_ids))
        ).all()
    return {seat_id: Decimal(price).quantize(CENT) for seat_id, price in rows}


def _money(value: object) -> Decimal:
    return Decimal(str(value))


def test_create_group_returns_an_invite_code_and_pending_status(client) -> None:
    user_id = _create_user("+919000001001")
    show_id, seat_ids, hold = _create_hold(client, user_id, 3)

    response = client.post(
        GROUPS_URL, json={"holdId": hold["holdId"]}, headers=_auth(user_id)
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == GROUP_KEYS
    assert body["status"] == "PENDING_PAYMENT"
    assert body["status"] in ALLOWED_GROUP_STATUSES
    assert len(body["inviteCode"]) == INVITE_CODE_LENGTH == 6
    assert re.fullmatch(r"[A-Z0-9]{6}", body["inviteCode"])
    assert body["showId"] == show_id
    assert body["ownerId"] == user_id
    assert sorted(body["seatIds"]) == sorted(seat_ids)
    assert Decimal(str(body["total"])) == sum(_seat_prices(seat_ids).values())

    assert len(body["members"]) == 1
    member = body["members"][0]
    assert set(member) == MEMBER_KEYS
    assert member["userId"] == user_id
    assert member["seatIds"] == [body["seatIds"][0]]
    assert member["paymentStatus"] == MemberPaymentStatus.PENDING.value
    assert len(member["seats"]) == 1
    assert member["seats"][0]["id"] == member["seatIds"][0]
    assert member["seats"][0]["label"]

    with SessionLocal() as db:
        group = db.get(Group, body["groupId"])
        assert group is not None
        assert group.owner_id == user_id
        assert group.status == "PENDING_PAYMENT"
        assert group.invite_code == body["inviteCode"]
        assert sorted(group.seat_ids) == sorted(seat_ids)

        stored = db.scalar(
            select(GroupMember).where(GroupMember.group_id == body["groupId"])
        )
        assert stored is not None
        assert stored.user_id == user_id
        assert stored.payment_status == "PENDING"
        assert stored.payment_id is None

        # Opening a group leaves the hold alone: the seats stay HELD until it is paid.
        for seat_id in seat_ids:
            row = db.get(ShowSeat, seat_id)
            assert row is not None
            assert row.status == ShowSeatStatus.HELD.value
            assert row.hold_id == hold["holdId"]

        assert list(db.scalars(select(Booking))) == []


def test_group_splits_the_hold_by_seat_price(client) -> None:
    user_id = _create_user("+919000001002")
    _, seat_ids, hold = _create_hold(client, user_id, 3)
    prices = _seat_prices(seat_ids)

    response = client.post(
        GROUPS_URL, json={"holdId": hold["holdId"]}, headers=_auth(user_id)
    )

    assert response.status_code == 201
    body = response.json()
    owner_seats = body["members"][0]["seatIds"]
    owner_share = _money(body["members"][0]["shareAmount"])

    assert owner_share == sum(prices[seat_id] for seat_id in owner_seats)
    assert owner_share == owner_share.quantize(CENT)

    unassigned = [
        seat_id for seat_id in body["seatIds"] if seat_id not in owner_seats
    ]
    assert unassigned
    shares = sum(
        (_money(member["shareAmount"]) for member in body["members"]), Decimal("0")
    )
    assert shares == owner_share
    assert shares + sum(prices[seat_id] for seat_id in unassigned) == _money(
        body["total"]
    )
    assert _money(body["total"]) == sum(prices.values())


def test_group_status_vocabulary_never_carries_an_expired_value(client) -> None:
    assert {status.value for status in BookingStatus} == {
        "PENDING_PAYMENT",
        "CONFIRMED",
        "FAILED",
        "CANCELLED",
    }
    assert "EXPIRED" not in {status.name for status in BookingStatus}
    booking_status_block = SCHEMAS_SOURCE.split("class BookingStatus")[1]
    assert "EXPIRED" not in booking_status_block.split("class ")[0]

    user_id = _create_user("+919000001003")
    _, _, hold = _create_hold(client, user_id, 1)

    response = client.post(
        GROUPS_URL, json={"holdId": hold["holdId"]}, headers=_auth(user_id)
    )

    assert response.status_code == 201
    assert response.json()["status"] in ALLOWED_GROUP_STATUSES

    with SessionLocal() as db:
        stored = list(db.scalars(select(Group.status)))
    assert stored == ["PENDING_PAYMENT"]
    assert set(stored) <= ALLOWED_GROUP_STATUSES


def test_group_expires_when_its_hold_does(client) -> None:
    user_id = _create_user("+919000001004")
    _, _, hold = _create_hold(client, user_id, 2)

    response = client.post(
        GROUPS_URL, json={"holdId": hold["holdId"]}, headers=_auth(user_id)
    )

    assert response.status_code == 201
    body = response.json()
    assert datetime.fromisoformat(body["expiresAt"]) == datetime.fromisoformat(
        hold["heldUntil"]
    )
    assert datetime.fromisoformat(body["expiresAt"]).utcoffset() == timedelta(
        hours=5, minutes=30
    )

    with SessionLocal() as db:
        group = db.get(Group, body["groupId"])
        assert group is not None
        assert group.hold_id == hold["holdId"]


def test_create_group_for_an_unknown_hold_returns_404(client) -> None:
    user_id = _create_user("+919000001005")
    missing_hold_id = str(uuid.uuid4())

    response = client.post(
        GROUPS_URL, json={"holdId": missing_hold_id}, headers=_auth(user_id)
    )

    assert response.status_code == 404
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == f"Hold not found: {missing_hold_id}"
    assert body["instance"] == GROUPS_URL

    with SessionLocal() as db:
        assert list(db.scalars(select(Group))) == []


def test_create_group_for_another_users_hold_returns_403(client) -> None:
    owner_id = _create_user("+919000001006")
    stranger_id = _create_user("+919000001007")
    _, _, hold = _create_hold(client, owner_id, 1)

    response = client.post(
        GROUPS_URL, json={"holdId": hold["holdId"]}, headers=_auth(stranger_id)
    )

    assert response.status_code == 403
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Forbidden"
    assert body["status"] == 403
    assert body["detail"] == "This hold belongs to another user"
    assert body["instance"] == GROUPS_URL

    with SessionLocal() as db:
        assert list(db.scalars(select(Group))) == []


def test_create_group_on_an_expired_hold_returns_410(client) -> None:
    user_id = _create_user("+919000001008")
    _, seat_ids, hold = _create_hold(client, user_id, 1)

    with SessionLocal() as db:
        row = db.get(ShowSeat, seat_ids[0])
        assert row is not None
        row.held_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()

    response = client.post(
        GROUPS_URL, json={"holdId": hold["holdId"]}, headers=_auth(user_id)
    )

    assert response.status_code == 410
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Gone"
    assert body["status"] == 410
    assert body["detail"] == "Hold has expired"
    assert body["instance"] == GROUPS_URL

    with SessionLocal() as db:
        assert list(db.scalars(select(Group))) == []


def test_get_group_is_owner_and_member_only(client) -> None:
    owner_id = _create_user("+919000001009")
    member_id = _create_user("+919000001010")
    stranger_id = _create_user("+919000001011")
    show_id, seat_ids, hold = _create_hold(client, owner_id, 2)
    created = client.post(
        GROUPS_URL, json={"holdId": hold["holdId"]}, headers=_auth(owner_id)
    )
    assert created.status_code == 201
    group_id = created.json()["groupId"]
    url = f"{GROUPS_URL}/{group_id}"

    with SessionLocal() as db:
        db.add(
            GroupMember(
                group_id=group_id,
                user_id=member_id,
                seat_ids=[],
                share_amount=Decimal("0"),
                payment_status=MemberPaymentStatus.PENDING.value,
                payment_id=None,
            )
        )
        db.commit()

    owner_read = client.get(url, headers=_auth(owner_id))

    assert owner_read.status_code == 200
    body = owner_read.json()
    assert body["groupId"] == group_id
    assert body["showId"] == show_id
    assert sorted(body["seatIds"]) == sorted(seat_ids)
    assert body["status"] == "PENDING_PAYMENT"

    member_read = client.get(url, headers=_auth(member_id))

    assert member_read.status_code == 200
    assert [member["userId"] for member in member_read.json()["members"]] == [
        owner_id,
        member_id,
    ]

    stranger_read = client.get(url, headers=_auth(stranger_id))

    assert stranger_read.status_code == 403
    stranger_body = stranger_read.json()
    assert set(stranger_body) == PROBLEM_DETAIL_KEYS
    assert stranger_body["title"] == "Forbidden"
    assert stranger_body["status"] == 403
    assert stranger_body["detail"] == NOT_A_MEMBER_DETAIL
    assert stranger_body["instance"] == url

    unknown = client.get(f"{GROUPS_URL}/999999", headers=_auth(owner_id))

    assert unknown.status_code == 404
    unknown_body = unknown.json()
    assert unknown_body["title"] == "Not Found"
    assert unknown_body["status"] == 404
    assert unknown_body["detail"] == "Group not found: 999999"
    assert unknown_body["instance"] == f"{GROUPS_URL}/999999"


def test_group_routes_require_a_token(client) -> None:
    user_id = _create_user("+919000001012")
    _, _, hold = _create_hold(client, user_id, 1)

    created = client.post(GROUPS_URL, json={"holdId": hold["holdId"]})

    assert created.status_code == 401
    body = created.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Unauthorized"
    assert body["status"] == 401
    assert body["detail"] == "Unauthorized"
    assert body["instance"] == GROUPS_URL

    read = client.get(f"{GROUPS_URL}/1")

    assert read.status_code == 401
    assert read.json()["title"] == "Unauthorized"

    with SessionLocal() as db:
        assert list(db.scalars(select(Group))) == []


PAYMENT_FIRST_ID = 501
PAYMENTS_BASE_URL = "http://payments.test"
SERVICE_TOKEN = "shared-token"
TOKEN_HEADER = "X-Service-Token"
IDEMPOTENCY_HEADER = "Idempotency-Key"


class _FakePaymentsClient:
    """Stand-in for PaymentsClient: one payment per member and a refund recorder."""

    def __init__(
        self, outcome: str = "succeed", refund_outcome: str | None = None
    ) -> None:
        self.outcome = outcome
        self.refund_outcome = refund_outcome if refund_outcome is not None else outcome
        self.calls: list[dict[str, object]] = []
        self.refunds: list[dict[str, object]] = []
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
        self.refunds.append(
            {"payment_id": payment_id, "idempotency_key": idempotency_key}
        )
        if self.refund_outcome == "unavailable":
            raise PaymentsUnavailableError(PAYMENTS_UNAVAILABLE_DETAIL)
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


class _Unreachable:
    """Stand-in for httpx.get/httpx.post when the service cannot be reached at all."""

    def __call__(self, url, *, json=None, headers=None, timeout=None):
        raise httpx.ConnectError("connection refused", request=httpx.Request("POST", url))


def _payments_client() -> PaymentsClient:
    return PaymentsClient(
        PaymentsSettings(
            base_url=PAYMENTS_BASE_URL,
            service_token=SERVICE_TOKEN,
            timeout_seconds=5.0,
        )
    )


def _create_group(client, user_id: int, count: int) -> dict:
    """Hold `count` seats and open a group on it, returning the created group body."""
    _, seat_ids, hold = _create_hold(client, user_id, count)
    response = client.post(
        GROUPS_URL, json={"holdId": hold["holdId"]}, headers=_auth(user_id)
    )
    assert response.status_code == 201
    body = response.json()
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


def test_create_group_share_sends_the_member_idempotency_key(monkeypatch) -> None:
    payment = {
        "paymentId": 5,
        "groupId": 11,
        "userId": 42,
        "status": "SUCCEEDED",
        "amount": 250.0,
        "currency": "INR",
    }
    recorder = _Recorder(status_code=201, body=payment)
    monkeypatch.setattr(httpx, "post", recorder)

    body = _payments_client().create_group_share(
        group_id=11, user_id=42, amount=Decimal("250.00")
    )

    assert body == payment
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["url"] == f"{PAYMENTS_BASE_URL}/internal/payments"
    assert call["headers"] == {
        TOKEN_HEADER: SERVICE_TOKEN,
        IDEMPOTENCY_HEADER: "group:11:42",
    }
    assert call["json"] == {
        "groupId": 11,
        "userId": 42,
        "amount": 250.0,
        "currency": "INR",
    }


def test_list_group_payments_asks_for_one_group(monkeypatch) -> None:
    rows = [
        {
            "paymentId": 6,
            "groupId": 11,
            "userId": 42,
            "status": "SUCCEEDED",
            "amount": 250.0,
            "currency": "INR",
        }
    ]
    recorder = _Recorder(body=rows)
    monkeypatch.setattr(httpx, "get", recorder)

    payments = _payments_client().list_group_payments(group_id=11)

    assert payments == rows
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["url"] == f"{PAYMENTS_BASE_URL}/internal/payments?groupId=11"
    assert call["headers"] == {TOKEN_HEADER: SERVICE_TOKEN}


def test_list_group_payments_of_a_group_without_shares_is_empty(monkeypatch) -> None:
    recorder = _Recorder(body=[])
    monkeypatch.setattr(httpx, "get", recorder)

    assert _payments_client().list_group_payments(group_id=7) == []
    assert len(recorder.calls) == 1


def test_an_unreachable_payments_service_fails_a_group_share(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "post", _Unreachable())

    with pytest.raises(PaymentsUnavailableError):
        _payments_client().create_group_share(
            group_id=11, user_id=42, amount=Decimal("250.00")
        )


def test_join_assigns_the_next_unassigned_seats(client) -> None:
    owner_id = _create_user("+919000001101")
    joiner_id = _create_user("+919000001102")
    created = _create_group(client, owner_id, 3)
    prices = _seat_prices(created["seatIds"])
    owner_seats = created["members"][0]["seatIds"]
    expected = [
        seat_id for seat_id in created["seatIds"] if seat_id not in owner_seats
    ]
    assert expected

    response = _join(client, joiner_id, created["inviteCode"].lower())

    assert response.status_code == 200
    body = response.json()
    assert body["groupId"] == created["groupId"]
    assert body["status"] == "PENDING_PAYMENT"
    assert [member["userId"] for member in body["members"]] == [owner_id, joiner_id]

    member = _member_of(body, joiner_id)
    assert member["seatIds"] == expected
    assert _money(member["shareAmount"]) == sum(prices[seat_id] for seat_id in expected)
    assert member["paymentStatus"] == MemberPaymentStatus.PENDING.value
    assert [seat["id"] for seat in member["seats"]] == expected
    assert all(seat["label"] for seat in member["seats"])
    assert _money(_member_of(body, owner_id)["shareAmount"]) == sum(
        prices[seat_id] for seat_id in owner_seats
    )

    with SessionLocal() as db:
        stored = db.scalar(
            select(GroupMember).where(
                GroupMember.group_id == created["groupId"],
                GroupMember.user_id == joiner_id,
            )
        )
        assert stored is not None
        assert stored.payment_status == "PENDING"
        assert stored.payment_id is None
        for seat_id in created["seatIds"]:
            row = db.get(ShowSeat, seat_id)
            assert row is not None
            assert row.status == ShowSeatStatus.HELD.value


def test_join_with_an_unknown_invite_code_returns_404(client) -> None:
    joiner_id = _create_user("+919000001103")
    url = f"{GROUPS_URL}/join"

    response = _join(client, joiner_id, "ZZZZZZ")

    assert response.status_code == 404
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == "Invite code not found: ZZZZZZ"
    assert body["instance"] == url


def test_join_twice_returns_409(client) -> None:
    owner_id = _create_user("+919000001104")
    joiner_id = _create_user("+919000001105")
    created = _create_group(client, owner_id, 2)
    url = f"{GROUPS_URL}/join"

    first = _join(client, joiner_id, created["inviteCode"])
    second = _join(client, joiner_id, created["inviteCode"])

    assert first.status_code == 200
    assert second.status_code == 409
    body = second.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Conflict"
    assert body["status"] == 409
    assert body["detail"] == ALREADY_MEMBER_DETAIL
    assert body["instance"] == url

    with SessionLocal() as db:
        members = list(
            db.scalars(
                select(GroupMember.user_id)
                .where(GroupMember.group_id == created["groupId"])
                .order_by(GroupMember.id)
            )
        )
    assert members == [owner_id, joiner_id]


def test_join_a_fully_assigned_group_returns_409(client) -> None:
    owner_id = _create_user("+919000001106")
    joiner_id = _create_user("+919000001107")
    stranger_id = _create_user("+919000001108")
    created = _create_group(client, owner_id, 2)
    assert _join(client, joiner_id, created["inviteCode"]).status_code == 200

    response = _join(client, stranger_id, created["inviteCode"])

    assert response.status_code == 409
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["status"] == 409
    assert body["detail"] == GROUP_FULL_DETAIL
    assert body["instance"] == f"{GROUPS_URL}/join"


def test_pay_share_charges_the_members_own_share_once(client) -> None:
    owner_id = _create_user("+919000001109")
    joiner_id = _create_user("+919000001110")
    created = _create_group(client, owner_id, 2)
    assert _join(client, joiner_id, created["inviteCode"]).status_code == 200
    owner_share = _money(_member_of(created, owner_id)["shareAmount"])
    fake = _FakePaymentsClient()

    with _use_payments(fake):
        first = _pay(client, owner_id, created["groupId"])
        second = _pay(client, owner_id, created["groupId"])

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(fake.payments) == 1
    assert len(fake.calls) == 1
    charge = fake.calls[0]
    assert charge["group_id"] == created["groupId"]
    assert charge["user_id"] == owner_id
    assert charge["currency"] == "INR"
    assert _money(charge["amount"]) == owner_share

    body = second.json()
    assert _member_of(body, owner_id)["paymentStatus"] == "PAID"
    assert _member_of(body, joiner_id)["paymentStatus"] == "PENDING"
    assert body["status"] == "PENDING_PAYMENT"

    with SessionLocal() as db:
        stored = db.scalar(
            select(GroupMember).where(
                GroupMember.group_id == created["groupId"],
                GroupMember.user_id == owner_id,
            )
        )
        assert stored is not None
        assert stored.payment_status == "PAID"
        assert stored.payment_id == fake.payments[(created["groupId"], owner_id)][
            "paymentId"
        ]
        for seat_id in created["seatIds"]:
            row = db.get(ShowSeat, seat_id)
            assert row is not None
            assert row.status == ShowSeatStatus.HELD.value


def test_a_non_member_cannot_pay_a_share(client) -> None:
    owner_id = _create_user("+919000001111")
    stranger_id = _create_user("+919000001112")
    created = _create_group(client, owner_id, 1)
    url = f"{GROUPS_URL}/{created['groupId']}/pay"
    fake = _FakePaymentsClient()

    with _use_payments(fake):
        response = _pay(client, stranger_id, created["groupId"])

    assert response.status_code == 403
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Forbidden"
    assert body["status"] == 403
    assert body["detail"] == NOT_A_MEMBER_DETAIL
    assert body["instance"] == url
    assert fake.calls == []
    assert fake.refunds == []


def test_group_confirms_once_every_share_is_paid(client) -> None:
    owner_id = _create_user("+919000001113")
    joiner_id = _create_user("+919000001114")
    created = _create_group(client, owner_id, 2)
    assert _join(client, joiner_id, created["inviteCode"]).status_code == 200
    fake = _FakePaymentsClient()

    with _use_payments(fake):
        owner_pay = _pay(client, owner_id, created["groupId"])
        joiner_pay = _pay(client, joiner_id, created["groupId"])

    assert owner_pay.status_code == 200
    assert owner_pay.json()["status"] == "PENDING_PAYMENT"
    assert joiner_pay.status_code == 200

    body = joiner_pay.json()
    assert body["status"] == "CONFIRMED"
    assert {member["paymentStatus"] for member in body["members"]} == {"PAID"}
    assert len(fake.payments) == 2

    with SessionLocal() as db:
        group = db.get(Group, created["groupId"])
        assert group is not None
        assert group.status == "CONFIRMED"
        for seat_id in created["seatIds"]:
            row = db.get(ShowSeat, seat_id)
            assert row is not None
            assert row.status == ShowSeatStatus.SOLD.value
            assert row.held_by is None
            assert row.held_until is None
            assert row.hold_id is None

        assert list(db.scalars(select(Booking))) == []


def test_pay_when_the_payments_service_is_down_is_503(client) -> None:
    owner_id = _create_user("+919000001115")
    created = _create_group(client, owner_id, 1)
    url = f"{GROUPS_URL}/{created['groupId']}/pay"
    fake = _FakePaymentsClient(outcome="unavailable")

    with _use_payments(fake):
        response = _pay(client, owner_id, created["groupId"])

    assert response.status_code == 503
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Service Unavailable"
    assert body["status"] == 503
    assert body["detail"] == PAYMENTS_UNAVAILABLE_DETAIL
    assert body["instance"] == url
    assert len(fake.calls) == 1

    with SessionLocal() as db:
        stored = db.scalar(
            select(GroupMember).where(GroupMember.group_id == created["groupId"])
        )
        assert stored is not None
        assert stored.payment_status == "PENDING"
        assert stored.payment_id is None

        row = db.get(ShowSeat, created["seatIds"][0])
        assert row is not None
        assert row.status == ShowSeatStatus.HELD.value
        assert row.hold_id is not None


def test_leave_refunds_the_share_and_returns_the_seats(client) -> None:
    owner_id = _create_user("+919000001116")
    joiner_id = _create_user("+919000001117")
    later_id = _create_user("+919000001118")
    created = _create_group(client, owner_id, 3)
    joined = _join(client, joiner_id, created["inviteCode"])
    assert joined.status_code == 200
    joiner_seats = _member_of(joined.json(), joiner_id)["seatIds"]
    url = f"{GROUPS_URL}/{created['groupId']}/members/me"
    fake = _FakePaymentsClient()

    with _use_payments(fake):
        paid = _pay(client, joiner_id, created["groupId"])
        left = client.delete(url, headers=_auth(joiner_id))

    assert paid.status_code == 200
    assert _member_of(paid.json(), joiner_id)["paymentStatus"] == "PAID"

    assert left.status_code == 200
    body = left.json()
    assert [member["userId"] for member in body["members"]] == [owner_id]
    assert body["status"] == "PENDING_PAYMENT"

    assert len(fake.refunds) == 1
    assert fake.refunds[0]["payment_id"] == fake.payments[
        (created["groupId"], joiner_id)
    ]["paymentId"]

    with SessionLocal() as db:
        assert (
            db.scalar(
                select(GroupMember).where(
                    GroupMember.group_id == created["groupId"],
                    GroupMember.user_id == joiner_id,
                )
            )
            is None
        )
        group = db.get(Group, created["groupId"])
        assert group is not None
        assert group.status == "PENDING_PAYMENT"
        for seat_id in created["seatIds"]:
            row = db.get(ShowSeat, seat_id)
            assert row is not None
            assert row.status == ShowSeatStatus.HELD.value

    again = _join(client, later_id, created["inviteCode"])

    assert again.status_code == 200
    assert _member_of(again.json(), later_id)["seatIds"] == joiner_seats


def test_cancel_refunds_every_paid_share_and_releases_the_seats(client) -> None:
    owner_id = _create_user("+919000001119")
    joiner_id = _create_user("+919000001120")
    created = _create_group(client, owner_id, 2)
    assert _join(client, joiner_id, created["inviteCode"]).status_code == 200
    fake = _FakePaymentsClient()

    with _use_payments(fake):
        assert _pay(client, owner_id, created["groupId"]).status_code == 200
        assert _pay(client, joiner_id, created["groupId"]).status_code == 200
        cancelled = client.post(
            f"{GROUPS_URL}/{created['groupId']}/cancel", headers=_auth(owner_id)
        )

    assert cancelled.status_code == 200
    body = cancelled.json()
    assert body["status"] == "CANCELLED"
    assert {member["paymentStatus"] for member in body["members"]} == {"REFUNDED"}
    assert len(fake.refunds) == 2
    assert {refund["payment_id"] for refund in fake.refunds} == {
        payment["paymentId"] for payment in fake.payments.values()
    }

    with SessionLocal() as db:
        group = db.get(Group, created["groupId"])
        assert group is not None
        assert group.status == "CANCELLED"
        for seat_id in created["seatIds"]:
            row = db.get(ShowSeat, seat_id)
            assert row is not None
            assert row.status == ShowSeatStatus.AVAILABLE.value
            assert row.held_by is None
            assert row.held_until is None
            assert row.hold_id is None


def test_cancel_by_a_non_owner_returns_403_and_refunds_nothing(client) -> None:
    owner_id = _create_user("+919000001121")
    joiner_id = _create_user("+919000001122")
    created = _create_group(client, owner_id, 2)
    assert _join(client, joiner_id, created["inviteCode"]).status_code == 200
    url = f"{GROUPS_URL}/{created['groupId']}/cancel"
    fake = _FakePaymentsClient()

    with _use_payments(fake):
        assert _pay(client, joiner_id, created["groupId"]).status_code == 200
        response = client.post(url, headers=_auth(joiner_id))

    assert response.status_code == 403
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Forbidden"
    assert body["status"] == 403
    assert body["detail"] == NOT_GROUP_OWNER_DETAIL
    assert body["instance"] == url
    assert fake.refunds == []

    with SessionLocal() as db:
        group = db.get(Group, created["groupId"])
        assert group is not None
        assert group.status == "PENDING_PAYMENT"
        for seat_id in created["seatIds"]:
            row = db.get(ShowSeat, seat_id)
            assert row is not None
            assert row.status == ShowSeatStatus.HELD.value


def test_group_pay_leave_and_cancel_require_a_token(client) -> None:
    owner_id = _create_user("+919000001123")
    created = _create_group(client, owner_id, 1)
    group_id = created["groupId"]
    fake = _FakePaymentsClient()

    with _use_payments(fake):
        pay = client.post(f"{GROUPS_URL}/{group_id}/pay")
        leave = client.delete(f"{GROUPS_URL}/{group_id}/members/me")
        cancel = client.post(f"{GROUPS_URL}/{group_id}/cancel")

    for response, url in (
        (pay, f"{GROUPS_URL}/{group_id}/pay"),
        (leave, f"{GROUPS_URL}/{group_id}/members/me"),
        (cancel, f"{GROUPS_URL}/{group_id}/cancel"),
    ):
        assert response.status_code == 401
        body = response.json()
        assert set(body) == PROBLEM_DETAIL_KEYS
        assert body["title"] == "Unauthorized"
        assert body["status"] == 401
        assert body["detail"] == "Unauthorized"
        assert body["instance"] == url

    assert fake.calls == []
    assert fake.refunds == []
