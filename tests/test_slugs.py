from app.slug import slugify


def test_slugify_lowercases_and_hyphenates():
    assert slugify("Mumbai") == "mumbai"
    assert slugify("Delhi NCR") == "delhi-ncr"


def test_city_slug_generation_and_conflict(client):
    created = client.post("/api/cities", json={"name": "Delhi NCR", "state": "Delhi"})
    assert created.status_code == 201
    city = created.json()
    assert city["slug"] == "delhi-ncr"

    renamed = client.put(
        f"/api/cities/{city['id']}",
        json={"name": "New Delhi", "state": "Delhi"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["slug"] == "new-delhi"

    duplicate = client.post("/api/cities", json={"name": "New Delhi", "state": "Delhi"})
    assert duplicate.status_code == 409
    body = duplicate.json()
    assert body["status"] == 409
    assert "slug is already taken" in body["detail"]

    unchanged = client.get(f"/api/cities/{city['id']}")
    assert unchanged.status_code == 200
    assert unchanged.json() == {
        "id": city["id"],
        "name": "New Delhi",
        "state": "Delhi",
        "slug": "new-delhi",
    }

    resaved = client.put(
        f"/api/cities/{city['id']}",
        json={"name": "New Delhi", "state": "Delhi"},
    )
    assert resaved.status_code == 200
