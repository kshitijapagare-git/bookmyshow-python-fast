import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from seed import (
    seed_cinemas,
    seed_cities,
    seed_movies,
    seed_screens,
    seed_seat_categories,
    seed_shows,
)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_movies(db)

    # A plain TestClient (not used as a context manager) so the lifespan does not
    # run: the startup seeding would otherwise fill the test database with cities
    # and cinemas. Tests that need those call the seed functions themselves.
    yield TestClient(app)

    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def city(client):
    response = client.post("/api/cities", json={"name": "Pune", "state": "Maharashtra"})
    assert response.status_code == 201
    return response.json()


@pytest.fixture()
def seeded_shows() -> Iterator[TestClient]:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_movies(db)
        seed_cities(db)
        seed_cinemas(db)
        seed_screens(db)
        seed_seat_categories(db)
        seed_shows(db)

    yield TestClient(app)

    Base.metadata.drop_all(bind=engine)
