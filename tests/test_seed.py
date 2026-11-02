from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Cinema, Movie, Screen, Show
from app.timeutil import ist_local, utc_from_ist

SEEDED_CITIES = {
    "Mumbai": "mumbai",
    "Pune": "pune",
    "Bengaluru": "bengaluru",
}

DETAIL_FIELDS = (
    "slug",
    "synopsis",
    "languages",
    "formats",
    "durationMinutes",
    "certificate",
    "releaseDate",
    "backdropUrl",
    "interestedCount",
)


def test_seeded_cities(client):
    response = client.get("/api/cities")

    assert response.status_code == 200
    cities = response.json()
    assert len(cities) == 3
    assert {city["name"]: city["slug"] for city in cities} == SEEDED_CITIES
    assert all(city["state"] for city in cities)


def test_seeded_cinemas_per_city(client):
    city_ids = {city["slug"]: city["id"] for city in client.get("/api/cities").json()}

    response = client.get("/api/cinemas")

    assert response.status_code == 200
    cinemas = response.json()
    assert len(cinemas) == 5
    mumbai = [cinema for cinema in cinemas if cinema["cityId"] == city_ids["mumbai"]]
    pune = [cinema for cinema in cinemas if cinema["cityId"] == city_ids["pune"]]
    assert len(mumbai) == 3
    assert len(pune) == 2
    assert all(cinema["name"] and cinema["address"] for cinema in cinemas)


def test_seeded_movie_detail_fields(client):
    response = client.get("/api/movies")

    assert response.status_code == 200
    movies = response.json()
    assert len(movies) == 8

    for movie in movies:
        detail = client.get(f"/api/movies/{movie['id']}")
        assert detail.status_code == 200
        body = detail.json()
        for field in DETAIL_FIELDS:
            assert body[field] is not None
        assert 30 <= body["durationMinutes"] <= 300
        assert body["certificate"] in {"U", "UA", "A", "S"}
        assert body["languages"]
        assert set(body["formats"]) <= {"2D", "3D", "IMAX", "4DX"}

    pehla_pyaar = movies[0]
    assert pehla_pyaar["title"] == "Pehla Pyaar Doosri Baar"
    detail = client.get(f"/api/movies/{pehla_pyaar['id']}").json()
    assert detail["releaseDate"] == (date.today() + timedelta(days=1)).isoformat()


SEEDED_SHOW_START_TIMES = {(8, 15), (9, 0), (13, 0), (18, 30), (23, 15)}
SEEDED_AMENITIES = {"M_TICKET", "FOOD", "PARKING", "WHEELCHAIR"}


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def test_seeded_screens_per_cinema(client):
    with SessionLocal() as db:
        cinemas = list(db.scalars(select(Cinema).order_by(Cinema.id)))
        screens = list(db.scalars(select(Screen).order_by(Screen.id)))

    assert len(cinemas) == 5
    assert len(screens) == 10

    screens_per_cinema = {cinema.id: 0 for cinema in cinemas}
    for screen in screens:
        screens_per_cinema[screen.cinema_id] += 1

    assert set(screens_per_cinema.values()) == {2}


def test_seeded_shows_cover_the_next_seven_days(client):
    with SessionLocal() as db:
        cinemas = list(db.scalars(select(Cinema).order_by(Cinema.id)))
        screens = list(db.scalars(select(Screen)))
        shows = list(db.scalars(select(Show)))
        movie_languages = {
            movie.id: {language.lower() for language in movie.languages}
            for movie in db.scalars(select(Movie))
        }

    assert shows

    days = [date.today() + timedelta(days=offset) for offset in range(7)]
    cinema_by_screen = {screen.id: screen.cinema_id for screen in screens}

    covered = set()
    for show in shows:
        local = ist_local(show.starts_at)
        expected = utc_from_ist(local.date(), local.hour, local.minute)
        assert (local.hour, local.minute) in SEEDED_SHOW_START_TIMES
        assert _as_utc(show.starts_at) == expected
        assert show.language == show.language.lower()
        assert show.language in movie_languages[show.movie_id]
        covered.add((cinema_by_screen[show.screen_id], local.date()))

    assert {(cinema.id, day) for cinema in cinemas for day in days} <= covered

    fill_ratios = [show.seats_sold / show.capacity for show in shows]
    assert any(ratio >= 0.7 for ratio in fill_ratios)
    assert any(ratio < 0.7 for ratio in fill_ratios)


def test_seeded_cinema_chain_fields(client):
    with SessionLocal() as db:
        cinemas = list(db.scalars(select(Cinema).order_by(Cinema.id)))

    assert len(cinemas) == 5
    for cinema in cinemas:
        assert cinema.chain_logo_url
        assert isinstance(cinema.cancellable, bool)
        assert cinema.amenities
        assert set(cinema.amenities) <= SEEDED_AMENITIES

    assert {cinema.cancellable for cinema in cinemas} == {True, False}
