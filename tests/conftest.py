import os
import tempfile

os.environ.setdefault("DATABASE_URL", "sqlite://")
# Keep the existing controller tests on the plain path; the crypto tests turn it on
# through create_app(CryptoSettings(...)) instead of an environment variable.
os.environ.setdefault("CRYPTO_ENABLED", "false")
# The media tests assert the /media mount serves the bytes they uploaded, so the
# directory is a fresh temp one: it must be set before app.main builds the mount, and
# it keeps uploaded bytes out of the repository.
os.environ.setdefault("MEDIA_UPLOAD_DIR", tempfile.mkdtemp(prefix="bms-media-"))

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.sms import get_sms_sender
from seed import seed_data


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_data(db)

    with TestClient(app) as test_client:
        yield test_client

    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def city(client):
    response = client.post("/api/cities", json={"name": "Nagpur", "state": "Maharashtra"})
    assert response.status_code == 201
    return response.json()


class _RecordingSmsSender:
    """Captures the (phone, code) pairs the auth router hands to the sender."""

    def __init__(self, sent: list[tuple[str, str]]) -> None:
        self._sent = sent

    def send(self, *, phone: str, code: str) -> None:
        self._sent.append((phone, code))


@pytest.fixture()
def sms_outbox(client):
    sent: list[tuple[str, str]] = []
    sender = _RecordingSmsSender(sent)
    app.dependency_overrides[get_sms_sender] = lambda: sender
    try:
        yield sent
    finally:
        app.dependency_overrides.pop(get_sms_sender, None)
