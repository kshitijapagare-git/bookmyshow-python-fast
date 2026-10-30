def test_cinema_crud(client, city):
    created = client.post(
        "/api/cinemas",
        json={"name": "PVR Icon", "address": "Phoenix Mall", "cityId": city["id"]},
    )
    assert created.status_code == 201
    cinema = created.json()
    assert cinema == {
        "id": cinema["id"],
        "name": "PVR Icon",
        "address": "Phoenix Mall",
        "cityId": city["id"],
    }

    fetched = client.get(f"/api/cinemas/{cinema['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == cinema

    listed = client.get("/api/cinemas")
    assert listed.status_code == 200
    assert listed.json() == [cinema]

    updated = client.put(
        f"/api/cinemas/{cinema['id']}",
        json={"name": "PVR Icon 2", "address": "Seasons Mall", "city_id": city["id"]},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "PVR Icon 2"
    assert updated.json()["address"] == "Seasons Mall"

    assert client.delete(f"/api/cinemas/{cinema['id']}").status_code == 204
    assert client.get(f"/api/cinemas/{cinema['id']}").status_code == 404


def test_cinema_requires_a_valid_city(client):
    response = client.post(
        "/api/cinemas",
        json={"name": "PVR Icon", "address": "Phoenix Mall", "cityId": 999},
    )

    assert response.status_code == 404


def test_cinema_validation(client, city):
    assert (
        client.post(
            "/api/cinemas",
            json={"name": "PVR Icon", "cityId": city["id"]},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/cinemas",
            json={"name": "", "address": "Phoenix Mall", "cityId": city["id"]},
        ).status_code
        == 422
    )


def test_missing_cinema_returns_404(client):
    assert client.get("/api/cinemas/999").status_code == 404
    assert (
        client.put(
            "/api/cinemas/999",
            json={"name": "X", "address": "Y", "cityId": 1},
        ).status_code
        == 404
    )
    assert client.delete("/api/cinemas/999").status_code == 404
