from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import Movie, Screen, SeatCategory, Show
from seed import CITY_TIME_ZONE, seed_all, seed_cinemas, seed_cities


def _seed_cities_and_cinemas() -> None:
    with SessionLocal() as db:
        seed_cities(db)
        seed_cinemas(db)


def _seed_everything() -> None:
    with SessionLocal() as db:
        seed_all(db)


def _local_start(starts_at: datetime) -> datetime:
    return starts_at.replace(tzinfo=timezone.utc).astimezone(CITY_TIME_ZONE)


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


def test_seeded_screens_and_shows(client):
    _seed_everything()

    with SessionLocal() as db:
        screens = db.scalars(select(Screen).order_by(Screen.id)).all()
        shows = db.scalars(select(Show)).all()

    assert len(screens) == 10
    assert len({(screen.cinema_id, screen.name) for screen in screens}) == len(screens)

    show_dates = {_local_start(show.starts_at).date() for show in shows}
    assert show_dates == {date.today() + timedelta(days=n) for n in range(7)}


def test_seeded_shows_fill_the_contract_fields(client):
    _seed_everything()

    with SessionLocal() as db:
        shows = db.scalars(select(Show)).all()
        movies = {movie.id: movie for movie in db.scalars(select(Movie))}
        languages = {
            movie_id: {language.lower() for language in movie.languages}
            for movie_id, movie in movies.items()
        }

    assert shows
    for show in shows:
        assert show.format.strip()
        assert show.language == show.language.lower()
        assert show.language in languages[show.movie_id]
        assert show.capacity > 0
        assert 0 <= show.seats_sold <= show.capacity

    local_times = {_local_start(show.starts_at).time() for show in shows}
    assert {time(8, 0), time(9, 0), time(23, 30)} <= local_times

    ratios = [show.seats_sold / show.capacity for show in shows]
    assert any(ratio >= 0.7 for ratio in ratios)
    assert any(ratio < 0.7 for ratio in ratios)


def test_a_screen_name_is_unique_per_cinema(client):
    _seed_everything()

    with SessionLocal() as db:
        screen = db.scalars(select(Screen).order_by(Screen.id)).first()
        assert screen is not None
        db.add(Screen(name=screen.name, cinema_id=screen.cinema_id))

        with pytest.raises(IntegrityError):
            db.commit()

        db.rollback()


def test_seeded_seat_categories_fill_every_screen(client):
    _seed_everything()

    expected = {
        "NORMAL": Decimal("180"),
        "EXECUTIVE": Decimal("190"),
        "PREMIUM": Decimal("210"),
        "PREMIUM_XL": Decimal("250"),
        "PREMIUM_PLUS": Decimal("230"),
    }

    with SessionLocal() as db:
        screens = db.scalars(select(Screen).order_by(Screen.id)).all()
        categories = db.scalars(select(SeatCategory)).all()

    assert screens
    prices: dict[int, dict[str, Decimal]] = {}
    for category in categories:
        prices.setdefault(category.screen_id, {})[category.name.value] = category.price

    assert sorted(prices) == [screen.id for screen in screens]
    assert all(screen_prices == expected for screen_prices in prices.values())

    # Seeding again adds no duplicate rows for the same screen and category.
    _seed_everything()

    with SessionLocal() as db:
        seeded_twice = db.scalars(select(SeatCategory)).all()

    assert len(seeded_twice) == len(screens) * len(expected)


def test_seeded_show_formats_are_declared_values(client):
    _seed_everything()

    with SessionLocal() as db:
        formats = {show.format for show in db.scalars(select(Show))}

    assert formats
    assert formats <= {"2D", "3D", "4DX", "IMAX"}
    assert len(formats) >= 2
