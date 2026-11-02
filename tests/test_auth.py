from datetime import datetime, timedelta, timezone

import bcrypt
from sqlalchemy import select

from app.db import SessionLocal
from app.models import OtpChallenge, OtpRequest, User

PHONE = "9876543210"
E164_PHONE = "+919876543210"
REQUEST_URL = "/api/auth/otp/request"
VERIFY_URL = "/api/auth/otp/verify"
WRONG_CODE_DETAIL = "That code is not right"
EXPIRED_DETAIL = "This code has expired. Request a new one."
LOCKED_DETAIL = "Too many wrong attempts. Request a new code."


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _request_otp(client, phone: str = PHONE):
    return client.post(REQUEST_URL, json={"countryCode": "+91", "phone": phone})


def _verify(client, challenge_id: str, code: str):
    return client.post(VERIFY_URL, json={"challengeId": challenge_id, "code": code})


def _new_challenge(client, sms_outbox) -> tuple[str, str]:
    response = _request_otp(client)
    assert response.status_code == 202
    phone, code = sms_outbox[-1]
    assert phone == E164_PHONE
    return response.json()["challengeId"], code


def _wrong_code(code: str) -> str:
    return "000000" if code != "000000" else "111111"


def test_request_otp_returns_202_and_stores_bcrypt_hash(client, sms_outbox):
    before = datetime.now(timezone.utc)

    response = _request_otp(client)

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"challengeId", "resendAfterSeconds", "expiresInSeconds"}
    assert body["resendAfterSeconds"] == 30
    assert body["expiresInSeconds"] == 300
    assert len(body["challengeId"]) == 36

    assert len(sms_outbox) == 1
    sent_phone, code = sms_outbox[0]
    assert sent_phone == E164_PHONE
    assert len(code) == 6
    assert code.isdigit()

    with SessionLocal() as db:
        challenge = db.get(OtpChallenge, body["challengeId"])
        assert challenge is not None
        assert challenge.phone == E164_PHONE
        assert challenge.code_hash.startswith("$2")
        assert challenge.code_hash != code
        assert bcrypt.checkpw(code.encode("utf-8"), challenge.code_hash.encode("utf-8"))
        assert challenge.attempts == 0
        expires_at = _as_utc(challenge.expires_at)
        assert timedelta(seconds=290) <= expires_at - before <= timedelta(seconds=310)

        logged = list(db.scalars(select(OtpRequest)))
        assert len(logged) == 1
        assert logged[0].phone == E164_PHONE


def test_request_otp_rejects_non_91_country_code(client, sms_outbox):
    response = client.post(REQUEST_URL, json={"countryCode": "+1", "phone": PHONE})

    assert response.status_code == 400
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Bad Request"
    assert body["status"] == 400
    assert body["detail"] == "countryCode must be +91"
    assert body["instance"] == REQUEST_URL

    assert sms_outbox == []
    with SessionLocal() as db:
        assert list(db.scalars(select(OtpChallenge))) == []
        assert list(db.scalars(select(OtpRequest))) == []


def test_rate_limit_returns_429_with_retry_after(client, sms_outbox):
    for _ in range(3):
        assert _request_otp(client).status_code == 202

    response = _request_otp(client)

    assert response.status_code == 429
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Too Many Requests"
    assert body["status"] == 429
    assert body["detail"].startswith("Too many codes requested. Try again in ")
    assert body["instance"] == REQUEST_URL

    retry_after = int(response.headers["Retry-After"])
    assert 0 < retry_after <= 600
    assert body["retryAfterSeconds"] == retry_after
    assert len(sms_outbox) == 3


def test_resend_issues_new_challenge_and_deletes_old(client, sms_outbox):
    first = _request_otp(client)
    assert first.status_code == 202
    second = _request_otp(client)
    assert second.status_code == 202

    first_id = first.json()["challengeId"]
    second_id = second.json()["challengeId"]
    assert first_id != second_id

    with SessionLocal() as db:
        assert db.get(OtpChallenge, first_id) is None
        assert db.get(OtpChallenge, second_id) is not None

    reused = _verify(client, first_id, sms_outbox[0][1])

    assert reused.status_code == 400
    assert reused.json()["detail"] == WRONG_CODE_DETAIL


def test_wrong_code_returns_400_with_attempts_left(client, sms_outbox):
    challenge_id, code = _new_challenge(client, sms_outbox)
    wrong = _wrong_code(code)

    for expected_left in (4, 3, 2, 1):
        response = _verify(client, challenge_id, wrong)

        assert response.status_code == 400
        body = response.json()
        assert body["type"] == "about:blank"
        assert body["title"] == "Bad Request"
        assert body["status"] == 400
        assert body["detail"] == WRONG_CODE_DETAIL
        assert body["attemptsLeft"] == expected_left
        assert body["instance"] == VERIFY_URL

        with SessionLocal() as db:
            challenge = db.get(OtpChallenge, challenge_id)
            assert challenge is not None
            assert challenge.attempts == 5 - expected_left


def test_fifth_wrong_attempt_locks_challenge(client, sms_outbox):
    challenge_id, code = _new_challenge(client, sms_outbox)
    wrong = _wrong_code(code)

    for _ in range(4):
        assert _verify(client, challenge_id, wrong).status_code == 400

    locked = _verify(client, challenge_id, wrong)

    assert locked.status_code == 423
    body = locked.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Locked"
    assert body["status"] == 423
    assert body["detail"] == LOCKED_DETAIL
    assert body["instance"] == VERIFY_URL

    still_locked = _verify(client, challenge_id, code)

    assert still_locked.status_code == 423
    assert still_locked.json()["detail"] == LOCKED_DETAIL

    with SessionLocal() as db:
        challenge = db.get(OtpChallenge, challenge_id)
        assert challenge is not None
        assert challenge.attempts == 5


def test_expired_challenge_returns_410(client, sms_outbox):
    challenge_id, code = _new_challenge(client, sms_outbox)

    with SessionLocal() as db:
        challenge = db.get(OtpChallenge, challenge_id)
        assert challenge is not None
        challenge.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()

    response = _verify(client, challenge_id, code)

    assert response.status_code == 410
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Gone"
    assert body["status"] == 410
    assert body["detail"] == EXPIRED_DETAIL
    assert body["instance"] == VERIFY_URL


def test_verify_creates_user_and_returns_jwt(client, sms_outbox):
    challenge_id, code = _new_challenge(client, sms_outbox)

    response = _verify(client, challenge_id, code)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"token", "expiresInSeconds", "user"}
    assert body["token"]
    assert body["expiresInSeconds"] == 604800
    assert isinstance(body["user"]["id"], int)
    assert body["user"]["phone"] == E164_PHONE
    assert body["user"]["name"] is None

    with SessionLocal() as db:
        users = list(db.scalars(select(User)))
        assert len(users) == 1
        assert users[0].id == body["user"]["id"]
        assert users[0].phone == E164_PHONE
        assert users[0].name is None
        assert db.get(OtpChallenge, challenge_id) is None

    reused = _verify(client, challenge_id, code)

    assert reused.status_code == 400
    assert reused.json()["detail"] == WRONG_CODE_DETAIL


def test_me_returns_user_with_token_and_401_without(client, sms_outbox):
    challenge_id, code = _new_challenge(client, sms_outbox)
    signed_in = _verify(client, challenge_id, code)
    assert signed_in.status_code == 200
    signed_in_body = signed_in.json()

    me = client.get(
        "/api/me",
        headers={"Authorization": f"Bearer {signed_in_body['token']}"},
    )

    assert me.status_code == 200
    assert me.json() == {
        "id": signed_in_body["user"]["id"],
        "phone": E164_PHONE,
        "name": None,
    }

    anonymous = client.get("/api/me")

    assert anonymous.status_code == 401
    problem = anonymous.json()
    assert problem["type"] == "about:blank"
    assert problem["title"] == "Unauthorized"
    assert problem["status"] == 401
    assert problem["instance"] == "/api/me"

    garbage = client.get("/api/me", headers={"Authorization": "Bearer not-a-jwt"})

    assert garbage.status_code == 401
    assert garbage.json()["title"] == "Unauthorized"


def test_public_endpoints_stay_open(client):
    for url in ("/api/cities", "/api/cinemas", "/api/movies"):
        response = client.get(url)

        assert response.status_code == 200, url
        assert response.json()
