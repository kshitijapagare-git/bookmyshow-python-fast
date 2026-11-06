import base64
import hashlib
import hmac
import json
import urllib.request

from app.db import SessionLocal
from app.models import Booking, BookingTransfer
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


def _auth(user_id: int) -> dict:
    return {"Authorization": f"Bearer {_make_token(user_id)}"}


class _FakePaymentsResponse:
    status = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self) -> "_FakePaymentsResponse":
        return self

    def __exit__(self, *_exc_info) -> bool:
        return False


def test_accept_moves_ownership_and_settles(client, pending_transfer, fake_payments):
    transfer = pending_transfer()

    response = client.post(
        f"/api/transfers/{transfer['transferCode']}/accept",
        headers=_auth(77),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["transferCode"] == transfer["transferCode"]
    assert body["status"] == "ACCEPTED"

    assert fake_payments["settle"] == [
        {
            "booking_id": transfer["bookingId"],
            "from_user_id": 42,
            "to_user_id": 77,
            "amount": 400.0,
            "fee": 0.0,
        }
    ]
    assert fake_payments["release"] == []

    with SessionLocal() as db:
        row = db.get(BookingTransfer, transfer["transferId"])
        assert row.status == "ACCEPTED"
        assert row.to_user_id == 77
        assert db.get(Booking, transfer["bookingId"]).user_id == 77


def test_accept_posts_the_settlement_contract(client, pending_transfer, monkeypatch):
    monkeypatch.setenv("PAYMENTS_BASE_URL", "http://payments.test")
    monkeypatch.setenv("PAYMENTS_SERVICE_TOKEN", "test-service-token")
    sent = []

    def _urlopen(request, timeout=None):
        sent.append(
            {
                "url": request.full_url,
                "headers": {key.lower(): value for key, value in request.header_items()},
                "body": json.loads(request.data.decode()),
            }
        )
        return _FakePaymentsResponse({"transferId": 5, "status": "SUCCEEDED", "amount": 400.0})

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)

    transfer = pending_transfer()

    response = client.post(
        f"/api/transfers/{transfer['transferCode']}/accept",
        headers=_auth(77),
    )

    assert response.status_code == 200
    assert len(sent) == 1
    assert sent[0]["url"] == "http://payments.test/internal/transfers"
    assert sent[0]["headers"]["x-service-token"] == "test-service-token"
    assert sent[0]["headers"]["idempotency-key"] == f"transfer:{transfer['bookingId']}"
    assert sent[0]["body"] == {
        "bookingId": transfer["bookingId"],
        "fromUserId": 42,
        "toUserId": 77,
        "amount": 400.0,
        "fee": 0.0,
    }


def test_accept_settlement_failure_leaves_transfer_pending(
    client, pending_transfer, failing_payments
):
    transfer = pending_transfer()

    response = client.post(
        f"/api/transfers/{transfer['transferCode']}/accept",
        headers=_auth(77),
    )

    assert response.status_code == 409
    with SessionLocal() as db:
        row = db.get(BookingTransfer, transfer["transferId"])
        assert row.status == "PENDING"
        assert row.to_user_id == 77
        assert db.get(Booking, transfer["bookingId"]).user_id == 42


def test_accept_requires_the_recipient(client, pending_transfer, fake_payments):
    transfer = pending_transfer()

    response = client.post(
        f"/api/transfers/{transfer['transferCode']}/accept",
        headers=_auth(99),
    )

    assert response.status_code == 403
    assert fake_payments["settle"] == []
    with SessionLocal() as db:
        row = db.get(BookingTransfer, transfer["transferId"])
        assert row.status == "PENDING"
        assert db.get(Booking, transfer["bookingId"]).user_id == 42


def test_accept_binds_the_recipient_of_a_phone_transfer(client, pending_transfer, fake_payments):
    transfer = pending_transfer(to_user_id=None)

    response = client.post(
        f"/api/transfers/{transfer['transferCode']}/accept",
        headers=_auth(77),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ACCEPTED"
    with SessionLocal() as db:
        row = db.get(BookingTransfer, transfer["transferId"])
        assert row.to_user_id == 77
        assert db.get(Booking, transfer["bookingId"]).user_id == 77


def test_cancel_by_owner_releases_settlement(client, pending_transfer, fake_payments):
    transfer = pending_transfer()

    response = client.post(
        f"/api/transfers/{transfer['transferId']}/cancel",
        headers=_auth(42),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert fake_payments["release"] == [{"booking_id": transfer["bookingId"]}]
    assert fake_payments["settle"] == []

    with SessionLocal() as db:
        row = db.get(BookingTransfer, transfer["transferId"])
        assert row.status == "CANCELLED"
        assert db.get(Booking, transfer["bookingId"]).user_id == 42


def test_cancel_release_failure_leaves_transfer_pending(
    client, pending_transfer, failing_payments
):
    transfer = pending_transfer()

    response = client.post(
        f"/api/transfers/{transfer['transferId']}/cancel",
        headers=_auth(42),
    )

    assert response.status_code == 409
    with SessionLocal() as db:
        row = db.get(BookingTransfer, transfer["transferId"])
        assert row.status == "PENDING"
        assert db.get(Booking, transfer["bookingId"]).user_id == 42


def test_cancel_requires_the_owner(client, pending_transfer, fake_payments):
    transfer = pending_transfer()

    response = client.post(
        f"/api/transfers/{transfer['transferId']}/cancel",
        headers=_auth(77),
    )

    assert response.status_code == 403
    assert fake_payments["release"] == []
    with SessionLocal() as db:
        assert db.get(BookingTransfer, transfer["transferId"]).status == "PENDING"


def test_expired_transfer_cannot_be_accepted(client, stale_transfer, fake_payments):
    transfer = stale_transfer()

    response = client.post(
        f"/api/transfers/{transfer['transferCode']}/accept",
        headers=_auth(77),
    )

    assert response.status_code == 409
    assert fake_payments["settle"] == []
    with SessionLocal() as db:
        row = db.get(BookingTransfer, transfer["transferId"])
        assert row.status == "EXPIRED"
        assert db.get(Booking, transfer["bookingId"]).user_id == 42


def test_accepted_transfer_cannot_be_cancelled(client, accepted_transfer, fake_payments):
    transfer = accepted_transfer()

    response = client.post(
        f"/api/transfers/{transfer['transferId']}/cancel",
        headers=_auth(42),
    )

    assert response.status_code == 409
    assert fake_payments["release"] == []
    with SessionLocal() as db:
        assert db.get(BookingTransfer, transfer["transferId"]).status == "ACCEPTED"
        assert db.get(Booking, transfer["bookingId"]).user_id == 77


def test_old_owner_booking_no_longer_reports_them(client, accepted_transfer):
    transfer = accepted_transfer()

    old_owner = client.get(f"/api/bookings/{transfer['bookingId']}", headers=_auth(42))
    assert old_owner.status_code == 403

    new_owner = client.get(f"/api/bookings/{transfer['bookingId']}", headers=_auth(77))
    assert new_owner.status_code == 200
    assert new_owner.json()["userId"] == 77
