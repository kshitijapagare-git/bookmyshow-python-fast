def test_not_found_is_a_problem_detail(client):
    response = client.get("/api/cities/999")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": "City not found",
        "instance": "/api/cities/999",
    }


def test_validation_error_is_a_problem_detail(client):
    response = client.post("/api/cities", json={"name": "Pune"})

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert set(body) == {"type", "title", "status", "detail", "instance"}
    assert body["type"] == "about:blank"
    assert body["title"] == "Unprocessable Entity"
    assert body["status"] == 422
    assert body["instance"] == "/api/cities"


def test_conflict_is_a_problem_detail(client, city):
    cinema = client.post(
        "/api/cinemas",
        json={"name": "PVR Icon", "address": "Phoenix Mall", "cityId": city["id"]},
    )
    assert cinema.status_code == 201

    response = client.delete(f"/api/cities/{city['id']}")

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Conflict"
    assert body["status"] == 409
    assert body["detail"] == "City is referenced by a cinema"
    assert body["instance"] == f"/api/cities/{city['id']}"
