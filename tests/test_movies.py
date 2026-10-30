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
