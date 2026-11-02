from decimal import Decimal
from urllib.parse import quote

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Cinema, Screen, SeatCategory, Show
from app.timeutil import today_ist, utc_from_ist


def _url(**params) -> str:
    query: dict[str, object] = {
        "city": "mumbai",
        "date": today_ist().isoformat(),
        "language": "hindi",
    }
    query.update(params)

    pairs: list[str] = []
    for key, value in query.items():
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            pairs.append(f"{key}={quote(str(item))}")
    return "/api/movies/1/shows?" + "&".join(pairs)


def _show_ids(body) -> set[int]:
    return {show["id"] for cinema in body["cinemas"] for show in cinema["shows"]}


def _add_screen_with_show(
    name: str, prices: dict[str, Decimal], hour: int, minute: int = 0
) -> tuple[int, int]:
    with SessionLocal() as db:
        cinema_id = db.scalar(select(Cinema.id).order_by(Cinema.id))
        assert cinema_id is not None
        screen = Screen(cinema_id=cinema_id, name=name)
        db.add(screen)
        db.flush()
        db.add_all(
            [
                SeatCategory(screen_id=screen.id, name=category, price=price)
                for category, price in prices.items()
            ]
        )
        show = Show(
            screen_id=screen.id,
            movie_id=1,
            starts_at=utc_from_ist(today_ist(), hour, minute),
            format="2K DOLBY 7.1",
            language="hindi",
        )
        db.add(show)
        db.commit()
        return screen.id, show.id


def test_price_filter_matches_any_seat_category(client) -> None:
    _, expensive_show_id = _add_screen_with_show(
        "Screen Price A", {"PREMIUM": Decimal("900.00")}, 10
    )
    _, mixed_show_id = _add_screen_with_show(
        "Screen Price B",
        {"PREMIUM": Decimal("900.00"), "NORMAL": Decimal("150.00")},
        11,
    )

    capped = client.get(_url(priceMax=200))
    assert capped.status_code == 200
    capped_ids = _show_ids(capped.json())
    assert expensive_show_id not in capped_ids
    assert mixed_show_id in capped_ids
    assert capped_ids

    floored = client.get(_url(priceMin=500))
    assert floored.status_code == 200
    assert _show_ids(floored.json()) == {expensive_show_id, mixed_show_id}

    inclusive = client.get(_url(priceMin=180, priceMax=180))
    assert inclusive.status_code == 200
    seeded_ids = _show_ids(client.get(_url()).json()) - {expensive_show_id, mixed_show_id}
    assert _show_ids(inclusive.json()) == seeded_ids


def test_price_max_below_price_min_is_400(client) -> None:
    response = client.get(_url(priceMin=300, priceMax=200))

    assert response.status_code == 400
    assert response.json() == {
        "type": "about:blank",
        "title": "Bad Request",
        "status": 400,
        "detail": "priceMax must be greater than or equal to priceMin",
        "instance": "/api/movies/1/shows",
    }


def test_unknown_enum_and_bad_price_are_400(client) -> None:
    cases = [
        ({"timeOfDay": "DAWN"}, "timeOfDay"),
        ({"sort": "NEWEST"}, "sort"),
        ({"priceMin": "abc"}, "priceMin"),
        ({"priceMax": "abc"}, "priceMax"),
        ({"cancellable": "maybe"}, "cancellable"),
        ({"wheelchair": "sometimes"}, "wheelchair"),
    ]

    for params, offending in cases:
        response = client.get(_url(**params))

        assert response.status_code == 400, params
        body = response.json()
        assert body["type"] == "about:blank"
        assert body["title"] == "Bad Request"
        assert body["status"] == 400
        assert offending in body["detail"]
        assert body["instance"] == "/api/movies/1/shows"


def test_time_of_day_bucket_boundaries(client) -> None:
    markers: dict[tuple[int, int], int] = {}
    for hour, minute in ((11, 59), (12, 0), (15, 59), (16, 0), (19, 59), (20, 0)):
        _, show_id = _add_screen_with_show(
            f"Screen {hour:02d}{minute:02d}",
            {"NORMAL": Decimal("180.00")},
            hour,
            minute,
        )
        markers[(hour, minute)] = show_id

    morning = _show_ids(client.get(_url(timeOfDay="MORNING")).json())
    assert markers[(11, 59)] in morning
    assert markers[(12, 0)] not in morning

    afternoon = _show_ids(client.get(_url(timeOfDay="AFTERNOON")).json())
    assert markers[(12, 0)] in afternoon
    assert markers[(15, 59)] in afternoon
    assert markers[(11, 59)] not in afternoon
    assert markers[(16, 0)] not in afternoon

    evening = _show_ids(client.get(_url(timeOfDay="EVENING")).json())
    assert markers[(16, 0)] in evening
    assert markers[(19, 59)] in evening
    assert markers[(15, 59)] not in evening
    assert markers[(20, 0)] not in evening

    night = _show_ids(client.get(_url(timeOfDay="NIGHT")).json())
    assert markers[(20, 0)] in night
    assert markers[(19, 59)] not in night

    repeated = _show_ids(client.get(_url(timeOfDay=["EVENING", "NIGHT"])).json())
    for key in ((16, 0), (19, 59), (20, 0)):
        assert markers[key] in repeated
    for key in ((11, 59), (12, 0), (15, 59)):
        assert markers[key] not in repeated


def test_filters_drop_fully_filtered_cinemas(client) -> None:
    unfiltered = client.get(_url())
    assert {cinema["id"] for cinema in unfiltered.json()["cinemas"]} == {1, 2, 3}

    cancellable = client.get(_url(cancellable="true"))
    assert cancellable.status_code == 200
    assert [cinema["id"] for cinema in cancellable.json()["cinemas"]] == [2, 1]

    wheelchair = client.get(_url(wheelchair="true"))
    assert wheelchair.status_code == 200
    wheelchair_ids = [cinema["id"] for cinema in wheelchair.json()["cinemas"]]
    assert wheelchair_ids == [2, 3]
    assert 1 not in wheelchair_ids

    formats = client.get(_url(format=["2K LASER DOLBY 7.1", "IMAX 3D"]))
    assert formats.status_code == 200
    shown_formats = {
        show["format"] for cinema in formats.json()["cinemas"] for show in cinema["shows"]
    }
    assert shown_formats == {"2K LASER DOLBY 7.1", "IMAX 3D"}


def test_available_filters_are_computed_unfiltered(client) -> None:
    unfiltered = client.get(_url())

    assert unfiltered.status_code == 200
    body = unfiltered.json()
    assert body["availableFilters"] == {
        "priceBands": [{"min": 0, "max": 200}, {"min": 201, "max": 250}],
        "formats": ["2K DOLBY 7.1", "2K LASER DOLBY 7.1", "IMAX 3D"],
    }
    assert body["appliedFilters"] == {
        "priceMin": None,
        "priceMax": None,
        "timeOfDay": [],
        "format": [],
        "cancellable": None,
        "wheelchair": None,
        "sort": "NAME",
    }

    filtered = client.get(_url(priceMax=180, timeOfDay="MORNING", format="IMAX 3D"))
    assert filtered.status_code == 200
    assert filtered.json()["availableFilters"] == body["availableFilters"]


def test_sort_and_cinema_flag_filters(client) -> None:
    with SessionLocal() as db:
        cinema_two_screens = list(db.scalars(select(Screen.id).where(Screen.cinema_id == 2)))
        early_shows = list(
            db.scalars(
                select(Show).where(
                    Show.screen_id.in_(cinema_two_screens),
                    Show.starts_at < utc_from_ist(today_ist(), 12, 0),
                )
            )
        )
        assert early_shows
        for show in early_shows:
            db.delete(show)
        db.commit()

    by_name = client.get(_url())
    assert by_name.status_code == 200
    assert [cinema["id"] for cinema in by_name.json()["cinemas"]] == [2, 3, 1]

    explicit_name = client.get(_url(sort="name"))
    assert [cinema["id"] for cinema in explicit_name.json()["cinemas"]] == [2, 3, 1]

    by_earliest = client.get(_url(sort="EARLIEST"))
    assert by_earliest.status_code == 200
    assert [cinema["id"] for cinema in by_earliest.json()["cinemas"]] == [1, 3, 2]

    cancellable = client.get(_url(cancellable="true")).json()["cinemas"]
    assert [cinema["id"] for cinema in cancellable] == [2, 1]

    wheelchair = client.get(_url(wheelchair="true")).json()["cinemas"]
    assert [cinema["id"] for cinema in wheelchair] == [2, 3]

    both = client.get(_url(cancellable="true", wheelchair="true")).json()["cinemas"]
    assert [cinema["id"] for cinema in both] == [2]

    echoed = client.get(
        _url(
            priceMin=0,
            priceMax=200,
            timeOfDay=["EVENING", "NIGHT"],
            format=["2K LASER DOLBY 7.1"],
            cancellable="true",
            wheelchair="true",
            sort="EARLIEST",
        )
    )
    assert echoed.status_code == 200
    assert echoed.json()["appliedFilters"] == {
        "priceMin": 0,
        "priceMax": 200,
        "timeOfDay": ["EVENING", "NIGHT"],
        "format": ["2K LASER DOLBY 7.1"],
        "cancellable": True,
        "wheelchair": True,
        "sort": "EARLIEST",
    }
