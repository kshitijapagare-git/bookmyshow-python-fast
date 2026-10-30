from app.db import SessionLocal
from seed import seed_all, seed_cinemas, seed_cities


def _seed_cities_and_cinemas() -> None:
    with SessionLocal() as db:
        seed_cities(db)
        seed_cinemas(db)


def test_seeded_cities_and_cinemas(client):
    _seed_cities_and_cinemas()

    cities = client.get("/api/cities")
    assert cities.status_code == 200
    city_rows = cities.json()
    assert [(row["name"], row["state"], row["slug"]) for row in city_rows] == [
        ("Mumbai", "Maharashtra", "mumbai"),
        ("Pune", "Maharashtra", "pune"),
        ("Bengaluru", "Karnataka", "bengaluru"),
    ]

    city_ids = {row["slug"]: row["id"] for row in city_rows}

    cinemas = client.get("/api/cinemas")
    assert cinemas.status_code == 200
    cinema_rows = cinemas.json()
    assert len(cinema_rows) == 5
    assert sum(1 for row in cinema_rows if row["cityId"] == city_ids["mumbai"]) == 3
    assert sum(1 for row in cinema_rows if row["cityId"] == city_ids["pune"]) == 2


def test_seeding_is_idempotent(client):
    with SessionLocal() as db:
        seed_all(db)
        seed_all(db)

    assert len(client.get("/api/cities").json()) == 3
    assert len(client.get("/api/cinemas").json()) == 5
    assert len(client.get("/api/movies").json()) == 8
