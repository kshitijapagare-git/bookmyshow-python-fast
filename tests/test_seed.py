from datetime import date, timedelta

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
