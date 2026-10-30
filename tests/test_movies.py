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


def test_movies_endpoint_is_read_only(client):
    response = client.post("/api/movies", json={"title": "New", "genres": "Drama"})

    assert response.status_code == 405
