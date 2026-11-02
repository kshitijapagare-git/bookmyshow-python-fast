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
    assert all(movie["slug"] for movie in movies)


def test_movies_endpoint_is_read_only(client):
    response = client.post("/api/movies", json={"title": "New", "genres": "Drama"})

    assert response.status_code == 405


def test_movies_city_filter(client):
    all_movies = client.get("/api/movies").json()
    assert len(all_movies) == 8

    mumbai = client.get("/api/movies?city=mumbai")

    assert mumbai.status_code == 200
    assert [movie["id"] for movie in mumbai.json()] == [movie["id"] for movie in all_movies]

    pune = client.get("/api/movies?city=pune")

    assert pune.status_code == 200
    pune_slugs = [movie["slug"] for movie in pune.json()]
    assert pune_slugs == ["pehla-pyaar-doosri-baar", "namit-malhotras-ramayana"]
    assert "avengers-endgame-encore" not in pune_slugs

    unknown_city = client.get("/api/movies?city=narnia")

    assert unknown_city.status_code == 200
    assert unknown_city.json() == []
