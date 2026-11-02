MOVIE_DETAIL_KEYS = {
    "id",
    "title",
    "slug",
    "genres",
    "posterUrl",
    "rating",
    "votes",
    "likes",
    "promoted",
    "synopsis",
    "languages",
    "formats",
    "durationMinutes",
    "certificate",
    "releaseDate",
    "backdropUrl",
    "trailerUrl",
    "interestedCount",
}


def test_movie_detail_found(client):
    response = client.get("/api/movies/1")

    assert response.status_code == 200
    movie = response.json()
    assert set(movie) == MOVIE_DETAIL_KEYS
    assert movie["title"] == "Pehla Pyaar Doosri Baar"
    assert movie["slug"] == "pehla-pyaar-doosri-baar"
    assert movie["languages"] == ["Hindi"]
    assert movie["formats"] == ["2D"]
    assert 30 <= movie["durationMinutes"] <= 300
    assert movie["certificate"] in {"U", "UA", "A", "S"}
    assert movie["backdropUrl"]
    assert isinstance(movie["interestedCount"], int)


def test_movie_detail_missing_returns_404(client):
    response = client.get("/api/movies/99")

    assert response.status_code == 404
    body = response.json()
    assert body["status"] == 404
    assert body["detail"] == "Movie not found: 99"
    assert body["instance"] == "/api/movies/99"
