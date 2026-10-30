from datetime import date, timedelta

CARD_FIELDS = {
    "id",
    "title",
    "slug",
    "genres",
    "posterUrl",
    "rating",
    "votes",
    "likes",
    "promoted",
}


def test_lists_the_eight_seeded_movies(client):
    response = client.get("/api/movies")

    assert response.status_code == 200
    movies = response.json()
    assert len(movies) == 8
    assert movies[0]["title"] == "Pehla Pyaar Doosri Baar"
    assert movies[0]["promoted"] is True
    assert movies[0]["posterUrl"] == "https://picsum.photos/seed/pehla-pyaar/400/600"
    assert movies[2]["rating"] == 9.5
    assert movies[2]["votes"] == 46000


def test_list_returns_card_fields_plus_slug(client):
    response = client.get("/api/movies")

    assert response.status_code == 200
    movies = response.json()
    assert len(movies) == 8
    for movie in movies:
        assert set(movie) == CARD_FIELDS


def test_get_movie_returns_the_full_movie(client):
    response = client.get("/api/movies/1")

    assert response.status_code == 200
    movie = response.json()
    assert movie["id"] == 1
    assert movie["title"] == "Pehla Pyaar Doosri Baar"
    assert movie["slug"] == "pehla-pyaar-doosri-baar"
    assert movie["genres"] == "Comedy/Romantic"
    assert movie["posterUrl"] == "https://picsum.photos/seed/pehla-pyaar/400/600"
    assert movie["rating"] is None
    assert movie["votes"] is None
    assert movie["likes"] == 10500
    assert movie["promoted"] is True
    assert movie["synopsis"]
    assert movie["languages"] == ["Hindi"]
    assert movie["formats"] == ["2D"]
    assert movie["durationMinutes"] == 142
    assert movie["certificate"] == "A"
    assert movie["releaseDate"] == (date.today() + timedelta(days=1)).isoformat()
    assert movie["backdropUrl"] == "https://picsum.photos/seed/pehla-pyaar/1600/900"
    assert movie["trailerUrl"] == "https://www.youtube.com/watch?v=pehla-pyaar-trailer"
    assert movie["interestedCount"] == 10500


def test_missing_movie_returns_404_problem_detail(client):
    response = client.get("/api/movies/99")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": "Movie not found: 99",
        "instance": "/api/movies/99",
    }


def test_movies_endpoint_is_read_only(client):
    response = client.post("/api/movies", json={"title": "New", "genres": "Drama"})

    assert response.status_code == 405


def test_city_filter_returns_only_movies_showing_that_city(seeded_shows):
    mumbai = seeded_shows.get("/api/movies", params={"city": "mumbai"})
    assert mumbai.status_code == 200
    mumbai_movies = mumbai.json()
    assert [movie["id"] for movie in mumbai_movies] == list(range(1, 8))
    assert "Kantara Chapter 1" not in [movie["title"] for movie in mumbai_movies]

    pune = seeded_shows.get("/api/movies", params={"city": "pune"})
    assert pune.status_code == 200
    pune_movies = pune.json()
    assert len(pune_movies) == 8
    assert "Kantara Chapter 1" in [movie["title"] for movie in pune_movies]

    without_city = seeded_shows.get("/api/movies")
    assert without_city.status_code == 200
    assert len(without_city.json()) == 8


def test_unknown_city_returns_empty_list(seeded_shows):
    response = seeded_shows.get("/api/movies", params={"city": "atlantis"})

    assert response.status_code == 200
    assert response.json() == []


def test_blank_city_returns_422(seeded_shows):
    response = seeded_shows.get("/api/movies", params={"city": ""})

    assert response.status_code == 422
