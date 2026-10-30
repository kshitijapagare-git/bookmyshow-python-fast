from datetime import date, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Movie
from app.schemas import MovieDetailOut
from app.slug import slugify

DETAIL = {
    "id": 1,
    "title": "Pehla Pyaar Doosri Baar",
    "slug": "pehla-pyaar-doosri-baar",
    "genres": "Comedy/Romantic",
    "posterUrl": "https://picsum.photos/seed/pehla-pyaar/400/600",
    "rating": None,
    "votes": None,
    "likes": 10500,
    "promoted": True,
    "synopsis": "Ten years after school, three best friends meet again.",
    "languages": ["Hindi"],
    "formats": ["2D", "IMAX"],
    "durationMinutes": 142,
    "certificate": "UA",
    "releaseDate": "2026-10-23",
    "backdropUrl": "https://picsum.photos/seed/pehla-pyaar/1600/900",
    "trailerUrl": "https://www.youtube.com/watch?v=pehla-pyaar-trailer",
    "interestedCount": 10500,
}


def test_slugify_lowercases_and_replaces_spaces():
    assert slugify("Pehla Pyaar Doosri Baar") == "pehla-pyaar-doosri-baar"
    assert slugify("Mumbai") == "mumbai"
    assert slugify("Avengers Endgame: Encore") == "avengers-endgame:-encore"


def test_movie_detail_accepts_the_contract_payload():
    movie = MovieDetailOut(**DETAIL)

    assert movie.formats == ["2D", "IMAX"]
    assert movie.certificate == "UA"
    assert movie.releaseDate == date(2026, 10, 23)


@pytest.mark.parametrize("duration", [10, 301])
def test_movie_detail_rejects_a_duration_outside_the_range(duration):
    with pytest.raises(ValidationError):
        MovieDetailOut(**{**DETAIL, "durationMinutes": duration})


@pytest.mark.parametrize(
    "override",
    [{"formats": ["XD"]}, {"certificate": "PG"}, {"languages": []}],
)
def test_movie_detail_rejects_invalid_values(override):
    with pytest.raises(ValidationError):
        MovieDetailOut(**{**DETAIL, **override})


def test_movie_detail_caps_the_synopsis_at_1000_characters():
    assert MovieDetailOut(**{**DETAIL, "synopsis": "a" * 1000}).synopsis == "a" * 1000

    with pytest.raises(ValidationError):
        MovieDetailOut(**{**DETAIL, "synopsis": "a" * 1001})


def test_seeded_movies_have_every_detail_field(client):
    with SessionLocal() as db:
        movies = db.scalars(select(Movie).order_by(Movie.id)).all()

    assert len(movies) == 8
    for movie in movies:
        assert movie.slug
        assert movie.synopsis
        assert len(movie.languages) >= 1
        assert len(movie.formats) >= 1
        assert 30 <= movie.duration_minutes <= 300
        assert movie.certificate in {"U", "UA", "A", "S"}
        assert movie.release_date is not None
        assert movie.backdrop_url


def test_first_seeded_movie_releases_tomorrow(client):
    with SessionLocal() as db:
        movie = db.scalar(select(Movie).where(Movie.title == "Pehla Pyaar Doosri Baar"))

        assert movie is not None
        assert movie.release_date == date.today() + timedelta(days=1)
        assert movie.interested_count == 10500


def test_listed_movies_carry_a_slug(client):
    response = client.get("/api/movies")

    assert response.status_code == 200
    movies = response.json()
    assert len(movies) == 8
    assert movies[0]["slug"] == "pehla-pyaar-doosri-baar"
