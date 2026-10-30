from datetime import date, datetime, time, timezone

from httpx import Response
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Cinema, Screen, SeatCategory, Show
from seed import CITY_TIME_ZONE


def _shows_url(movie_id: int = 1) -> str:
    return f"/api/movies/{movie_id}/shows"


def _query(*time_of_day: str, **overrides: str) -> list[tuple[str, str]]:
    params = [
        ("city", "mumbai"),
        ("date", date.today().isoformat()),
        ("language", "hindi"),
    ]
    params.extend(("timeOfDay", value) for value in time_of_day)
    params.extend(overrides.items())
    return params


def _cinema_names(response: Response) -> list[str]:
    return [cinema["name"] for cinema in response.json()["cinemas"]]


def _listed_local_times(response: Response) -> list[time]:
    return sorted(
        datetime.fromisoformat(show["startsAt"]).astimezone(CITY_TIME_ZONE).time()
        for cinema in response.json()["cinemas"]
        for show in cinema["shows"]
    )


def _add_show(
    cinema_name: str,
    day: date,
    show_time: time,
    movie_id: int = 1,
    show_format: str = "2D",
) -> None:
    with SessionLocal() as db:
        cinema = db.scalar(select(Cinema).where(Cinema.name == cinema_name))
        assert cinema is not None
        screen = db.scalars(
            select(Screen).where(Screen.cinema_id == cinema.id).order_by(Screen.id)
        ).first()
        assert screen is not None
        local = datetime.combine(day, show_time, tzinfo=CITY_TIME_ZONE)
        db.add(
            Show(
                screen_id=screen.id,
                movie_id=movie_id,
                starts_at=local.astimezone(timezone.utc).replace(tzinfo=None),
                format=show_format,
                language="hindi",
                capacity=120,
                seats_sold=0,
            )
        )
        db.commit()


def test_price_range_keeps_only_matching_screens(seeded_shows):
    baseline = seeded_shows.get(_shows_url(), params=_query())

    assert baseline.status_code == 200
    assert _cinema_names(baseline) == ["INOX Megaplex", "Maison PVR", "PVR Icon"]

    # Every seeded screen carries a 180 and a 190 category, so the 0-200 range
    # keeps every show. Drop INOX's cheapest categories so only it loses out.
    with SessionLocal() as db:
        inox = db.scalar(select(Cinema).where(Cinema.name == "INOX Megaplex"))
        assert inox is not None
        inox_id = inox.id
        screen_ids = list(
            db.scalars(select(Screen.id).where(Screen.cinema_id == inox_id))
        )
        cheap = list(
            db.scalars(
                select(SeatCategory).where(
                    SeatCategory.screen_id.in_(screen_ids),
                    SeatCategory.price <= 200,
                )
            )
        )
        assert cheap
        for category in cheap:
            db.delete(category)
        db.commit()

    response = seeded_shows.get(
        _shows_url(), params=_query(priceMin="0", priceMax="200")
    )

    assert response.status_code == 200
    assert _cinema_names(response) == ["Maison PVR", "PVR Icon"]

    show_ids = [
        show["id"]
        for cinema in response.json()["cinemas"]
        for show in cinema["shows"]
    ]
    assert show_ids
    with SessionLocal() as db:
        listed = db.scalars(select(Show).where(Show.id.in_(show_ids))).all()
        listed_cinema_ids = {show.screen.cinema_id for show in listed}

    assert listed_cinema_ids
    assert inox_id not in listed_cinema_ids

    # A range no seat category falls into drops every cinema.
    outside = seeded_shows.get(
        _shows_url(), params=_query(priceMin="300", priceMax="400")
    )

    assert outside.status_code == 200
    assert outside.json()["cinemas"] == []


def test_time_of_day_buckets(seeded_shows):
    today = date.today()
    _add_show("PVR Icon", today, time(20, 0))

    expected = {
        "MORNING": {time(8, 0), time(9, 0)},
        "AFTERNOON": {time(13, 0)},
        "EVENING": {time(18, 30)},
        "NIGHT": {time(20, 0), time(23, 30)},
    }
    for bucket, times in expected.items():
        response = seeded_shows.get(_shows_url(), params=_query(timeOfDay=bucket))

        assert response.status_code == 200
        assert set(_listed_local_times(response)) == times

    # 20:00 belongs to the night bucket and not to the evening one.
    evening = seeded_shows.get(_shows_url(), params=_query(timeOfDay="EVENING"))
    night = seeded_shows.get(_shows_url(), params=_query(timeOfDay="NIGHT"))

    assert time(20, 0) not in _listed_local_times(evening)
    assert time(20, 0) in _listed_local_times(night)

    # Repeated timeOfDay values are OR-ed together.
    repeated = seeded_shows.get(_shows_url(), params=_query("EVENING", "NIGHT"))

    assert repeated.status_code == 200
    assert set(_listed_local_times(repeated)) == {
        time(18, 30),
        time(20, 0),
        time(23, 30),
    }
    assert repeated.json()["appliedFilters"]["timeOfDay"] == ["EVENING", "NIGHT"]


def test_format_cancellable_and_wheelchair_filters(seeded_shows):
    imax = seeded_shows.get(_shows_url(), params=_query(format="IMAX"))

    assert imax.status_code == 200
    imax_shows = [
        show for cinema in imax.json()["cinemas"] for show in cinema["shows"]
    ]
    assert imax_shows
    assert {show["format"] for show in imax_shows} == {"IMAX"}

    four_dx = seeded_shows.get(_shows_url(), params=_query(format="4DX"))

    assert four_dx.status_code == 200
    assert set(_listed_local_times(four_dx)) == {time(13, 0)}

    cancellable = seeded_shows.get(
        _shows_url(), params=_query(cancellable="true")
    )

    assert cancellable.status_code == 200
    assert _cinema_names(cancellable) == ["Maison PVR", "PVR Icon"]

    wheelchair = seeded_shows.get(_shows_url(), params=_query(wheelchair="true"))

    assert wheelchair.status_code == 200
    assert _cinema_names(wheelchair) == ["Maison PVR"]

    without_wheelchair = seeded_shows.get(
        _shows_url(), params=_query(wheelchair="false")
    )

    assert without_wheelchair.status_code == 200
    assert _cinema_names(without_wheelchair) == ["INOX Megaplex", "PVR Icon"]


def test_sort_by_name_and_earliest(seeded_shows):
    by_name = seeded_shows.get(_shows_url(), params=_query(sort="NAME"))

    assert by_name.status_code == 200
    assert _cinema_names(by_name) == ["INOX Megaplex", "Maison PVR", "PVR Icon"]
    assert by_name.json()["appliedFilters"]["sort"] == "NAME"

    # An omitted sort behaves like NAME.
    default_sort = seeded_shows.get(_shows_url(), params=_query())

    assert default_sort.status_code == 200
    assert _cinema_names(default_sort) == _cinema_names(by_name)
    assert default_sort.json()["appliedFilters"]["sort"] == "NAME"

    # Give Maison PVR an earlier show than any other cinema's earliest show.
    _add_show("Maison PVR", date.today(), time(6, 30))

    by_earliest = seeded_shows.get(_shows_url(), params=_query(sort="EARLIEST"))

    assert by_earliest.status_code == 200
    cinemas = by_earliest.json()["cinemas"]
    assert len(cinemas) == 3
    earliest_starts = [
        min(datetime.fromisoformat(show["startsAt"]) for show in cinema["shows"])
        for cinema in cinemas
    ]
    assert earliest_starts == sorted(earliest_starts)
    assert cinemas[0]["name"] == "Maison PVR"
    assert by_earliest.json()["appliedFilters"]["sort"] == "EARLIEST"


def test_invalid_price_range_and_unknown_enums_return_400(seeded_shows):
    response = seeded_shows.get(
        _shows_url(), params=_query(priceMin="200", priceMax="199")
    )

    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Bad Request",
        "status": 400,
        "detail": "priceMax must be greater than or equal to priceMin",
        "instance": "/api/movies/1/shows",
    }

    for parameter, value in (
        ("timeOfDay", "DAWN"),
        ("format", "DOLBY"),
        ("sort", "LATEST"),
    ):
        invalid = seeded_shows.get(
            _shows_url(), params=_query(**{parameter: value})
        )

        assert invalid.status_code == 400
        assert invalid.headers["content-type"] == "application/problem+json"
        body = invalid.json()
        assert body["type"] == "about:blank"
        assert body["title"] == "Bad Request"
        assert body["status"] == 400
        assert body["instance"] == "/api/movies/1/shows"
        assert parameter in body["detail"]


def test_applied_and_available_filters_are_echoed(seeded_shows):
    unfiltered = seeded_shows.get(_shows_url(), params=_query())

    assert unfiltered.status_code == 200
    body = unfiltered.json()
    assert body["appliedFilters"] == {
        "priceMin": None,
        "priceMax": None,
        "timeOfDay": [],
        "format": None,
        "cancellable": None,
        "wheelchair": None,
        "sort": "NAME",
    }
    assert body["availableFilters"] == {
        "priceBands": [{"min": 0, "max": 200}, {"min": 201, "max": 250}],
        "formats": ["2D", "3D", "4DX", "IMAX"],
    }

    filtered = seeded_shows.get(
        _shows_url(),
        params=_query(
            "EVENING",
            "NIGHT",
            priceMin="0",
            priceMax="200",
            format="2D",
            cancellable="true",
            wheelchair="false",
            sort="EARLIEST",
        ),
    )

    assert filtered.status_code == 200
    filtered_body = filtered.json()
    assert filtered_body["appliedFilters"] == {
        "priceMin": 0,
        "priceMax": 200,
        "timeOfDay": ["EVENING", "NIGHT"],
        "format": "2D",
        "cancellable": True,
        "wheelchair": False,
        "sort": "EARLIEST",
    }
    # Only PVR Icon is cancellable, has no wheelchair amenity and plays a 2D
    # show after 4 PM.
    assert _cinema_names(filtered) == ["PVR Icon"]

    # The available filters describe the unfiltered day, so they do not move.
    assert filtered_body["availableFilters"] == body["availableFilters"]
