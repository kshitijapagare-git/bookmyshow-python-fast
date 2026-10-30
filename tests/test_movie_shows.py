from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Screen, Show
from app.schemas import Availability
from app.showtime import availability_for, is_early_morning, is_late_night
from seed import CITY_TIME_ZONE

SHOW_FIELDS = {
    "id",
    "startsAt",
    "format",
    "language",
    "availability",
    "lateNight",
    "earlyMorning",
}

CINEMA_FIELDS = {
    "id",
    "name",
    "address",
    "chainLogoUrl",
    "cancellable",
    "amenities",
    "shows",
}


def _utc_starts_at(day: date, show_time: time) -> datetime:
    local = datetime.combine(day, show_time, tzinfo=CITY_TIME_ZONE)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _shows_url(movie_id: int = 1) -> str:
    return f"/api/movies/{movie_id}/shows"


def _query(**overrides: str) -> dict[str, str]:
    params = {
        "city": "mumbai",
        "date": date.today().isoformat(),
        "language": "hindi",
    }
    params.update(overrides)
    return params


def test_shows_are_grouped_by_cinema_and_sorted(seeded_shows):
    today = date.today()

    response = seeded_shows.get(_shows_url(), params=_query())

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"movie", "dates", "cinemas"}
    assert body["movie"] == {
        "id": 1,
        "title": "Pehla Pyaar Doosri Baar",
        "durationMinutes": 142,
        "certificate": "A",
        "genres": "Comedy/Romantic",
    }

    assert len(body["dates"]) == 7
    assert body["dates"][0] == {"date": today.isoformat(), "hasShows": True}
    assert [entry["date"] for entry in body["dates"]] == [
        (today + timedelta(days=offset)).isoformat() for offset in range(7)
    ]

    cinemas = body["cinemas"]
    assert [cinema["name"] for cinema in cinemas] == [
        "PVR Icon",
        "INOX Megaplex",
        "Maison PVR",
    ]
    for cinema in cinemas:
        assert set(cinema) == CINEMA_FIELDS
        assert cinema["chainLogoUrl"]
        assert cinema["shows"]
        assert len(cinema["shows"]) >= 3

        starts = [show["startsAt"] for show in cinema["shows"]]
        assert starts == sorted(starts)

        for show in cinema["shows"]:
            assert set(show) == SHOW_FIELDS
            assert show["startsAt"].endswith("+05:30")
            assert show["language"] == "hindi"
            assert show["format"], "every show carries a screen format"


def test_availability_rule(client):
    assert availability_for(seats_sold=70, capacity=100) is Availability.FAST_FILLING
    assert availability_for(seats_sold=69, capacity=100) is Availability.AVAILABLE
    assert availability_for(seats_sold=0, capacity=0) is Availability.AVAILABLE


def test_listed_availability_follows_the_seeded_seat_counts(seeded_shows):
    response = seeded_shows.get(_shows_url(), params=_query())

    assert response.status_code == 200
    listed = {
        show["id"]: show["availability"]
        for cinema in response.json()["cinemas"]
        for show in cinema["shows"]
    }

    with SessionLocal() as db:
        shows = db.scalars(select(Show).where(Show.id.in_(listed))).all()

    assert shows
    for show in shows:
        expected = availability_for(show.seats_sold, show.capacity)
        assert listed[show.id] == expected.value
    assert set(listed.values()) == {"AVAILABLE", "FAST_FILLING"}


def test_late_night_and_early_morning_flags(client):
    today = date.today()

    assert is_late_night(_utc_starts_at(today, time(23, 30))) is True
    assert is_late_night(_utc_starts_at(today, time(23, 0))) is False
    assert is_early_morning(_utc_starts_at(today, time(8, 0))) is True
    assert is_early_morning(_utc_starts_at(today, time(9, 0))) is False


def test_listed_shows_carry_the_late_night_and_early_morning_flags(seeded_shows):
    response = seeded_shows.get(_shows_url(), params=_query())

    assert response.status_code == 200
    by_local_time = {
        datetime.fromisoformat(show["startsAt"]).astimezone(CITY_TIME_ZONE).time(): show
        for cinema in response.json()["cinemas"]
        for show in cinema["shows"]
    }

    assert by_local_time[time(23, 30)]["lateNight"] is True
    assert by_local_time[time(23, 30)]["earlyMorning"] is False
    assert by_local_time[time(8, 0)]["earlyMorning"] is True
    assert by_local_time[time(8, 0)]["lateNight"] is False
    assert by_local_time[time(9, 0)]["lateNight"] is False
    assert by_local_time[time(9, 0)]["earlyMorning"] is False


def test_date_validation(seeded_shows):
    today = date.today()

    for invalid in (today - timedelta(days=1), today + timedelta(days=7)):
        response = seeded_shows.get(
            _shows_url(), params=_query(date=invalid.isoformat())
        )

        assert response.status_code == 400
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json() == {
            "type": "about:blank",
            "title": "Bad Request",
            "status": 400,
            "detail": "date must be today or within the next 6 days",
            "instance": "/api/movies/1/shows",
        }

    last_valid_day = seeded_shows.get(
        _shows_url(), params=_query(date=(today + timedelta(days=6)).isoformat())
    )
    assert last_valid_day.status_code == 200

    three_days_ahead = seeded_shows.get(
        _shows_url(), params=_query(date=(today + timedelta(days=3)).isoformat())
    )
    assert three_days_ahead.status_code == 200
    assert three_days_ahead.json()["dates"][0]["date"] == today.isoformat()


def test_language_is_required(seeded_shows):
    response = seeded_shows.get(
        _shows_url(), params={"city": "mumbai", "date": date.today().isoformat()}
    )

    assert response.status_code == 400
    body = response.json()
    assert body["title"] == "Bad Request"
    assert body["detail"] == "language is required"
    assert body["instance"] == "/api/movies/1/shows"


def test_city_and_date_are_required(seeded_shows):
    assert (
        seeded_shows.get(
            _shows_url(), params={"date": date.today().isoformat(), "language": "hindi"}
        ).status_code
        == 422
    )
    assert (
        seeded_shows.get(
            _shows_url(), params={"city": "mumbai", "language": "hindi"}
        ).status_code
        == 422
    )


def test_missing_movie_returns_404(seeded_shows):
    response = seeded_shows.get(_shows_url(999), params=_query())

    assert response.status_code == 404
    body = response.json()
    assert body["title"] == "Not Found"
    assert body["detail"] == "Movie not found: 999"
    assert body["instance"] == "/api/movies/999/shows"


def test_unknown_city_returns_no_cinemas(seeded_shows):
    response = seeded_shows.get(_shows_url(), params=_query(city="atlantis"))

    assert response.status_code == 200
    assert response.json()["cinemas"] == []


def test_only_cinemas_of_the_requested_city_are_returned(seeded_shows):
    response = seeded_shows.get(_shows_url(), params=_query(city="pune"))

    assert response.status_code == 200
    assert [cinema["name"] for cinema in response.json()["cinemas"]] == [
        "PVR Pavilion",
        "INOX Bund Garden",
    ]


def test_has_shows_respects_the_language_filter(seeded_shows):
    today = date.today()
    covered = today + timedelta(days=2)

    with SessionLocal() as db:
        screen = db.scalars(select(Screen).order_by(Screen.id)).first()
        assert screen is not None
        db.add(
            Show(
                screen_id=screen.id,
                movie_id=1,
                starts_at=_utc_starts_at(covered, time(11, 0)),
                format="2K LASER DOLBY 7.1",
                language="tamil",
                capacity=120,
                seats_sold=0,
            )
        )
        db.commit()

    response = seeded_shows.get(
        _shows_url(), params=_query(date=covered.isoformat(), language="TAMIL")
    )

    assert response.status_code == 200
    body = response.json()
    has_shows = {entry["date"]: entry["hasShows"] for entry in body["dates"]}
    assert has_shows[covered.isoformat()] is True
    assert has_shows[(today + timedelta(days=1)).isoformat()] is False

    assert len(body["cinemas"]) == 1
    cinema = body["cinemas"][0]
    assert cinema["name"] == "PVR Icon"
    assert len(cinema["shows"]) == 1
    assert cinema["shows"][0]["language"] == "tamil"
    assert (
        datetime.fromisoformat(cinema["shows"][0]["startsAt"])
        .astimezone(CITY_TIME_ZONE)
        .time()
        == time(11, 0)
    )
