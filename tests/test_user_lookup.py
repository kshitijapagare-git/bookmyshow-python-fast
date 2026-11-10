"""Resolving a recipient's phone number to the user id a transfer names.

The frontend types a phone number and the transfer endpoint takes a `toUserId`, so
this lookup is the one step between the two, behind the moviegoer's own JWT.
"""

from datetime import datetime, timezone

from app.db import SessionLocal
from app.models import User
from app.security import issue_token

LOOKUP_URL = "/api/users/lookup"
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
USER_KEYS = {"id", "phone", "name"}


def _create_user(phone: str, name: str | None = None) -> int:
    with SessionLocal() as db:
        user = User(phone=phone, name=name, created_at=datetime.now(timezone.utc))
        db.add(user)
        db.commit()
        return user.id


def _auth(user_id: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(user_id=user_id)}"}


def test_lookup_finds_the_moviegoer_by_phone(client) -> None:
    caller_id = _create_user("+919000005001")
    friend_id = _create_user("+919876543210", name="Kshitija")

    response = client.get(
        LOOKUP_URL, params={"phone": "+919876543210"}, headers=_auth(caller_id)
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == USER_KEYS
    assert body["id"] == friend_id
    assert body["phone"] == "+919876543210"
    assert body["name"] == "Kshitija"


def test_lookup_for_an_unknown_phone_is_404(client) -> None:
    caller_id = _create_user("+919000005002")

    response = client.get(
        LOOKUP_URL, params={"phone": "+910000000000"}, headers=_auth(caller_id)
    )

    assert response.status_code == 404
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == "User not found: +910000000000"
    assert body["instance"] == LOOKUP_URL


def test_lookup_requires_a_token(client) -> None:
    _create_user("+919876543211")

    response = client.get(LOOKUP_URL, params={"phone": "+919876543211"})

    assert response.status_code == 401
    body = response.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Unauthorized"
    assert body["status"] == 401
    assert body["detail"] == "Unauthorized"
