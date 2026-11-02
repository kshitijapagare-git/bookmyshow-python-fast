from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import ShowSeat
from app.timeutil import today_ist

MOVIE_KEYS = {"id", "title", "durationMinutes", "certificate", "genres"}
CINEMA_KEYS = {"id", "name", "address", "chainLogoUrl", "cancellable", "amenities", "shows"}
SHOW_KEYS = {"id", "startsAt", "format", "language", "availability", "lateNight", "earlyMorning"}


def _shows_url(
    movie_id: int = 1,
    city: str = "mumbai",
    day: str | None = None,
    language: str | None = "hindi",
) -> str:
    day = today_ist().isoformat() if day is None else day
    query = f"city={city}&date={day}"
    if language is not None:
        query += f"&language={language}"
    return f"/api/movies/{movie_id}/shows?{query}"


def test_shows_grouped_by_cinema_sorted_by_start_time(client):
    response = client.get(_shows_url())

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"movie", "dates", "cinemas", "appliedFilters", "availableFilters"}
    assert set(body["movie"]) == MOVIE_KEYS
    assert body["movie"] == {
        "id": 1,
        "title": "Pehla Pyaar Doosri Baar",
        "durationMinutes": 142,
        "certificate": "A",
        "genres": "Comedy/Romantic",
    }

    today = today_ist()
    dates = body["dates"]
    assert len(dates) == 7
    assert [entry["date"] for entry in dates] == [
        (today + timedelta(days=offset)).isoformat() for offset in range(7)
    ]
    assert all(set(entry) == {"date", "hasShows"} for entry in dates)
    assert dates[0]["hasShows"] is True

    cinemas = body["cinemas"]
    assert len(cinemas) == 3
    assert [cinema["name"] for cinema in cinemas] == sorted(cinema["name"] for cinema in cinemas)

    for cinema in cinemas:
        assert set(cinema) == CINEMA_KEYS
        assert cinema["name"] and cinema["address"]
        assert cinema["chainLogoUrl"]
        assert isinstance(cinema["cancellable"], bool)
        assert cinema["amenities"]
        assert cinema["shows"]

        starts = [datetime.fromisoformat(show["startsAt"]) for show in cinema["shows"]]
        assert starts == sorted(starts)
        for show in cinema["shows"]:
            assert set(show) == SHOW_KEYS
            assert show["startsAt"].endswith("+05:30")
            assert show["format"]
            assert show["language"] == "hindi"
            assert show["availability"] in {"AVAILABLE", "FAST_FILLING"}


def test_shows_dates_are_language_scoped(client):
    hindi = client.get(_shows_url())
    assert hindi.status_code == 200
    assert hindi.json()["cinemas"]
    assert any(entry["hasShows"] for entry in hindi.json()["dates"])

    english = client.get(_shows_url(language="english"))

    assert english.status_code == 200
    body = english.json()
    assert [entry["hasShows"] for entry in body["dates"]] == [False] * 7
    assert body["cinemas"] == []


def test_shows_availability_rule(client):
    response = client.get(_shows_url())

    assert response.status_code == 200
    shows = [show for cinema in response.json()["cinemas"] for show in cinema["shows"]]
    assert shows

    ratios = _seat_fill_ratios()

    for show in shows:
        ratio = ratios[show["id"]]
        expected = "FAST_FILLING" if ratio >= 0.5 else "AVAILABLE"
        assert show["availability"] == expected, show["id"]

    at_threshold = [show for show in shows if ratios[show["id"]] == 0.5]
    above = [show for show in shows if ratios[show["id"]] > 0.5]
    below = [show for show in shows if ratios[show["id"]] < 0.5]
    assert at_threshold and above and below
    assert all(show["availability"] == "FAST_FILLING" for show in at_threshold + above)
    assert all(show["availability"] == "AVAILABLE" for show in below)


def _seat_fill_ratios() -> dict[int, float]:
    """Share of each show's seats that are HELD or SOLD."""
    with SessionLocal() as db:
        rows = db.execute(
            select(ShowSeat.show_id, ShowSeat.status, func.count(ShowSeat.id)).group_by(
                ShowSeat.show_id, ShowSeat.status
            )
        )
        totals: dict[int, int] = defaultdict(int)
        filled: dict[int, int] = defaultdict(int)
        for show_id, seat_status, count in rows.all():
            totals[show_id] += count
            if seat_status != "AVAILABLE":
                filled[show_id] += count

    return {show_id: filled[show_id] / total for show_id, total in totals.items()}


def test_shows_date_validation_returns_400(client):
    today = today_ist()

    for valid in (today, today + timedelta(days=6)):
        assert client.get(_shows_url(day=valid.isoformat())).status_code == 200

    for invalid in (
        (today - timedelta(days=1)).isoformat(),
        (today + timedelta(days=7)).isoformat(),
        "2026-13-45",
    ):
        response = client.get(_shows_url(day=invalid))

        assert response.status_code == 400
        body = response.json()
        assert body["type"] == "about:blank"
        assert body["title"] == "Bad Request"
        assert body["status"] == 400
        assert body["detail"] == "date must be today or within the next 6 days"
        assert body["instance"] == "/api/movies/1/shows"


def test_shows_requires_language(client):
    response = client.get(_shows_url(language=None))

    assert response.status_code == 400
    body = response.json()
    assert body["title"] == "Bad Request"
    assert body["status"] == 400
    assert body["detail"] == "language is required"

    assert client.get(_shows_url(language="")).status_code == 400


def test_shows_unknown_movie_is_404_and_unknown_city_is_empty(client):
    missing = client.get(_shows_url(movie_id=999))

    assert missing.status_code == 404
    assert missing.json()["detail"] == "Movie not found: 999"

    unknown_city = client.get(_shows_url(city="narnia"))

    assert unknown_city.status_code == 200
    body = unknown_city.json()
    assert body["cinemas"] == []
    assert [entry["hasShows"] for entry in body["dates"]] == [False] * 7


def test_shows_late_night_and_early_morning_flags(client):
    response = client.get(_shows_url())

    assert response.status_code == 200
    shows = [show for cinema in response.json()["cinemas"] for show in cinema["shows"]]
    by_clock = {}
    for show in shows:
        local = datetime.fromisoformat(show["startsAt"])
        assert local.utcoffset() == timedelta(hours=5, minutes=30)
        by_clock[(local.hour, local.minute)] = show

    assert by_clock[(23, 15)]["lateNight"] is True
    assert by_clock[(23, 15)]["earlyMorning"] is False

    assert by_clock[(8, 15)]["earlyMorning"] is True
    assert by_clock[(8, 15)]["lateNight"] is False

    assert by_clock[(9, 0)]["lateNight"] is False
    assert by_clock[(9, 0)]["earlyMorning"] is False
