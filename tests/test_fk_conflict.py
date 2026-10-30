def test_deleting_a_city_with_a_cinema_returns_409(client, city):
    cinema = client.post(
        "/api/cinemas",
        json={"name": "PVR Icon", "address": "Phoenix Mall", "cityId": city["id"]},
    )
    assert cinema.status_code == 201

    response = client.delete(f"/api/cities/{city['id']}")

    assert response.status_code == 409
    assert client.get(f"/api/cities/{city['id']}").status_code == 200
