PROBLEM_DETAIL_KEYS = {"type", "title", "status", "detail", "instance"}


def _assert_problem_detail(body, status_code: int) -> None:
    assert set(body) == PROBLEM_DETAIL_KEYS
    assert body["type"] == "about:blank"
    assert body["status"] == status_code


def test_not_found_body_is_problem_detail(client):
    response = client.get("/api/cities/999")

    assert response.status_code == 404
    body = response.json()
    _assert_problem_detail(body, 404)
    assert body["detail"] == "City not found"
    assert body["instance"] == "/api/cities/999"


def test_conflict_body_is_problem_detail(client, city):
    cinema = client.post(
        "/api/cinemas",
        json={"name": "PVR Icon", "address": "Phoenix Mall", "cityId": city["id"]},
    )
    assert cinema.status_code == 201

    response = client.delete(f"/api/cities/{city['id']}")

    assert response.status_code == 409
    body = response.json()
    _assert_problem_detail(body, 409)
    assert body["instance"] == f"/api/cities/{city['id']}"


def test_validation_body_is_problem_detail(client):
    response = client.post("/api/cities", json={"name": "Pune"})

    assert response.status_code == 422
    _assert_problem_detail(response.json(), 422)


def test_method_not_allowed_body_is_problem_detail(client):
    response = client.post("/api/movies", json={"title": "New", "genres": "Drama"})

    assert response.status_code == 405
    _assert_problem_detail(response.json(), 405)
