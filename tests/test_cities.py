def test_city_crud(client):
    created = client.post("/api/cities", json={"name": "Pune", "state": "Maharashtra"})
    assert created.status_code == 201
    city = created.json()
    assert city == {
        "id": city["id"],
        "name": "Pune",
        "state": "Maharashtra",
        "slug": "pune",
    }

    fetched = client.get(f"/api/cities/{city['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == city

    listed = client.get("/api/cities")
    assert listed.status_code == 200
    assert listed.json() == [city]

    updated = client.put(
        f"/api/cities/{city['id']}",
        json={"name": "Pimpri", "state": "Maharashtra"},
    )
    assert updated.status_code == 200
    assert updated.json() == {
        "id": city["id"],
        "name": "Pimpri",
        "state": "Maharashtra",
        "slug": "pimpri",
    }

    deleted = client.delete(f"/api/cities/{city['id']}")
    assert deleted.status_code == 204

    assert client.get(f"/api/cities/{city['id']}").status_code == 404


def test_city_slug_generation(client):
    created = client.post(
        "/api/cities",
        json={"name": "Navi Mumbai", "state": "Maharashtra", "slug": "not-this"},
    )

    assert created.status_code == 201
    assert created.json()["slug"] == "navi-mumbai"
    assert created.json()["name"] == "Navi Mumbai"


def test_duplicate_city_slug_returns_409(client):
    first = client.post("/api/cities", json={"name": "Pune", "state": "Maharashtra"})
    assert first.status_code == 201

    second = client.post("/api/cities", json={"name": "Pune", "state": "Maharashtra"})

    assert second.status_code == 409
    assert second.json()["detail"] == "City slug already exists"


def test_city_validation(client):
    assert client.post("/api/cities", json={"name": "Pune"}).status_code == 422
    assert client.post("/api/cities", json={"name": "", "state": "Maharashtra"}).status_code == 422
    assert client.post("/api/cities", json={"name": "   ", "state": "Maharashtra"}).status_code == 422


def test_missing_city_returns_404(client):
    assert client.get("/api/cities/999").status_code == 404
    assert client.put("/api/cities/999", json={"name": "X", "state": "Y"}).status_code == 404
    assert client.delete("/api/cities/999").status_code == 404
