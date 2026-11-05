"""The receptionist's guest directory: CRUD over /api/v1/guests.

Every test starts from the shared `client` fixture, which rebuilds the schema and
re-seeds the database, so the guest table is always empty at the start of a test.
"""

from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Guest

GUESTS_URL = "/api/v1/guests"
GUEST_KEYS = {
    "id",
    "firstName",
    "lastName",
    "email",
    "phone",
    "idProofType",
    "idProofNumber",
    "createdAt",
    "updatedAt",
}
PAGE_KEYS = {"content", "page", "size", "totalElements", "totalPages"}
PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}
NOT_FOUND_DETAIL = "Guest not found"


def _payload(**overrides: object) -> dict[str, object]:
    """A valid create body; an override replaces one key and None removes it."""
    body: dict[str, object] = {
        "firstName": "Mira",
        "lastName": "Kapoor",
        "email": "mira@example.com",
        "phone": "9876543210",
        "idProofType": "PASSPORT",
        "idProofNumber": "P1234567",
    }
    for key, value in overrides.items():
        if value is None:
            body.pop(key, None)
        else:
            body[key] = value
    return body


def _create_guest(client: TestClient, **overrides: object) -> dict[str, object]:
    response = client.post(GUESTS_URL, json=_payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


def test_create_guest_returns_created_guest(client: TestClient) -> None:
    response = client.post(GUESTS_URL, json=_payload())

    assert response.status_code == 201
    body = response.json()
    assert set(body) == GUEST_KEYS
    assert isinstance(body["id"], int)
    assert body["firstName"] == "Mira"
    assert body["lastName"] == "Kapoor"
    assert body["email"] == "mira@example.com"
    assert body["phone"] == "9876543210"
    assert body["idProofType"] == "PASSPORT"
    assert body["idProofNumber"] == "P1234567"
    assert datetime.fromisoformat(body["createdAt"])
    assert datetime.fromisoformat(body["updatedAt"])

    with SessionLocal() as db:
        guest = db.get(Guest, body["id"])
        assert guest is not None
        assert guest.first_name == "Mira"
        assert guest.last_name == "Kapoor"


def test_email_is_stored_lowercase(client: TestClient) -> None:
    response = client.post(GUESTS_URL, json=_payload(email="Mira@Example.COM"))

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "mira@example.com"

    read = client.get(f"{GUESTS_URL}/{body['id']}")

    assert read.status_code == 200
    assert read.json()["email"] == "mira@example.com"

    with SessionLocal() as db:
        guest = db.get(Guest, body["id"])
        assert guest is not None
        assert guest.email == "mira@example.com"


def test_duplicate_email_returns_409(client: TestClient) -> None:
    _create_guest(client)

    for duplicate in ("mira@example.com", "MIRA@EXAMPLE.COM"):
        response = client.post(GUESTS_URL, json=_payload(email=duplicate))

        assert response.status_code == 409, duplicate
        body = response.json()
        assert set(body) == PROBLEM_DETAIL_KEYS
        assert body["title"] == "Conflict"
        assert body["status"] == 409
        assert body["instance"] == GUESTS_URL


def test_id_proof_type_validation(client: TestClient) -> None:
    for bad_type in ("VOTER_ID", None):
        response = client.post(GUESTS_URL, json=_payload(idProofType=bad_type))

        assert response.status_code == 400, bad_type
        body = response.json()
        assert set(body) == PROBLEM_DETAIL_KEYS
        assert body["title"] == "Bad Request"
        assert body["status"] == 400
        assert "idProofType" in body["detail"]
        assert body["instance"] == GUESTS_URL

    with SessionLocal() as db:
        assert list(db.scalars(select(Guest))) == []


def test_id_proof_number_validation(client: TestClient) -> None:
    for bad_number in (None, "AB12", "A" * 21, "AAD-1234"):
        response = client.post(GUESTS_URL, json=_payload(idProofNumber=bad_number))

        assert response.status_code == 400, bad_number
        body = response.json()
        assert set(body) == PROBLEM_DETAIL_KEYS
        assert body["title"] == "Bad Request"
        assert body["status"] == 400
        assert "idProofNumber" in body["detail"]
        assert body["instance"] == GUESTS_URL

    with SessionLocal() as db:
        assert list(db.scalars(select(Guest))) == []


def test_field_validation_returns_422(client: TestClient) -> None:
    bad_bodies = (
        _payload(firstName=None),
        _payload(lastName="K" * 51),
        _payload(email="not-an-email"),
        _payload(phone="987654321"),
        _payload(phone="98765abcde"),
    )

    for bad_body in bad_bodies:
        response = client.post(GUESTS_URL, json=bad_body)

        assert response.status_code == 422, bad_body
        assert set(response.json()) == PROBLEM_DETAIL_KEYS

    accepted = client.post(GUESTS_URL, json=_payload(firstName="K" * 50))

    assert accepted.status_code == 201
    assert accepted.json()["firstName"] == "K" * 50


def test_get_guest_and_missing_guest(client: TestClient) -> None:
    created = _create_guest(client)

    response = client.get(f"{GUESTS_URL}/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created

    missing = client.get(f"{GUESTS_URL}/999999")

    assert missing.status_code == 404
    body = missing.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == NOT_FOUND_DETAIL
    assert body["instance"] == f"{GUESTS_URL}/999999"


def test_list_guests_paginates(client: TestClient) -> None:
    empty = client.get(GUESTS_URL)

    assert empty.status_code == 200
    empty_body = empty.json()
    assert set(empty_body) == PAGE_KEYS
    assert empty_body["content"] == []
    assert empty_body["page"] == 0
    assert empty_body["size"] == 20
    assert empty_body["totalElements"] == 0
    assert empty_body["totalPages"] == 0

    created = [
        _create_guest(
            client,
            email=f"guest{index}@example.com",
            phone=f"987654{index:04d}",
        )
        for index in range(25)
    ]

    defaults = client.get(GUESTS_URL)

    assert defaults.status_code == 200
    default_body = defaults.json()
    assert set(default_body) == PAGE_KEYS
    assert default_body["page"] == 0
    assert default_body["size"] == 20
    assert default_body["totalElements"] == 25
    assert default_body["totalPages"] == 2
    assert len(default_body["content"]) == 20

    response = client.get(f"{GUESTS_URL}?page=2&size=10")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == PAGE_KEYS
    assert body["page"] == 2
    assert body["size"] == 10
    assert body["totalElements"] == 25
    assert body["totalPages"] == 3
    assert [guest["id"] for guest in body["content"]] == [
        guest["id"] for guest in created[20:25]
    ]
    for guest in body["content"]:
        assert set(guest) == GUEST_KEYS


def test_update_guest(client: TestClient) -> None:
    created = _create_guest(client)
    other = _create_guest(client, email="other@example.com", phone="9876543211")

    response = client.put(
        f"{GUESTS_URL}/{created['id']}",
        json=_payload(
            lastName="Sharma",
            email="Mira.Sharma@Example.com",
            phone="9123456780",
            idProofType="AADHAAR",
            idProofNumber="123456789012",
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == GUEST_KEYS
    assert body["id"] == created["id"]
    assert body["firstName"] == "Mira"
    assert body["lastName"] == "Sharma"
    assert body["email"] == "mira.sharma@example.com"
    assert body["phone"] == "9123456780"
    assert body["idProofType"] == "AADHAAR"
    assert body["idProofNumber"] == "123456789012"
    assert body["updatedAt"] is not None
    assert datetime.fromisoformat(body["updatedAt"])

    read = client.get(f"{GUESTS_URL}/{created['id']}")

    assert read.status_code == 200
    assert read.json()["lastName"] == "Sharma"
    assert read.json()["email"] == "mira.sharma@example.com"

    missing = client.put(f"{GUESTS_URL}/999999", json=_payload())

    assert missing.status_code == 404
    assert missing.json()["detail"] == NOT_FOUND_DETAIL

    bad_type = client.put(
        f"{GUESTS_URL}/{created['id']}", json=_payload(idProofType="VOTER_ID")
    )

    assert bad_type.status_code == 400
    assert bad_type.json()["title"] == "Bad Request"

    taken = client.put(
        f"{GUESTS_URL}/{created['id']}",
        json=_payload(email=str(other["email"]).upper()),
    )

    assert taken.status_code == 409
    assert taken.json()["title"] == "Conflict"

    with SessionLocal() as db:
        guest = db.get(Guest, created["id"])
        assert guest is not None
        assert guest.last_name == "Sharma"
        assert guest.email == "mira.sharma@example.com"


def test_delete_guest(client: TestClient) -> None:
    created = _create_guest(client)
    url = f"{GUESTS_URL}/{created['id']}"

    deleted = client.delete(url)

    assert deleted.status_code == 204
    assert deleted.content == b""

    assert client.get(url).status_code == 404

    with SessionLocal() as db:
        assert db.get(Guest, created["id"]) is None

    missing = client.delete(f"{GUESTS_URL}/999999")

    assert missing.status_code == 404
    body = missing.json()
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == NOT_FOUND_DETAIL
    assert body["instance"] == f"{GUESTS_URL}/999999"
