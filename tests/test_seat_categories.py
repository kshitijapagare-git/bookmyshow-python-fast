from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import Cinema, Screen, SeatCategory
from seed import seed_data

EXPECTED_PRICES = {
    "NORMAL": Decimal("180.00"),
    "EXECUTIVE": Decimal("190.00"),
    "PREMIUM": Decimal("210.00"),
    "PREMIUM_XL": Decimal("250.00"),
    "PREMIUM_PLUS": Decimal("230.00"),
}


def _create_extra_screen() -> int:
    """Create a screen that has no seat categories yet, so constraints are isolated."""
    with SessionLocal() as db:
        cinema_id = db.scalar(select(Cinema.id).order_by(Cinema.id))
        assert cinema_id is not None
        screen = Screen(cinema_id=cinema_id, name="Screen 3")
        db.add(screen)
        db.commit()
        return screen.id


def test_every_screen_has_the_five_seat_categories(client) -> None:
    with SessionLocal() as db:
        screens = list(db.scalars(select(Screen).order_by(Screen.id)))
        categories = list(db.scalars(select(SeatCategory).order_by(SeatCategory.id)))

        assert screens
        assert len(categories) == len(screens) * len(EXPECTED_PRICES)

        by_screen: dict[int, dict[str, Decimal]] = {}
        for category in categories:
            by_screen.setdefault(category.screen_id, {})[category.name] = category.price

        assert set(by_screen) == {screen.id for screen in screens}
        for prices in by_screen.values():
            assert prices.keys() == EXPECTED_PRICES.keys()
            assert prices == EXPECTED_PRICES
            assert all(
                f"{prices[name]:.2f}" == f"{EXPECTED_PRICES[name]:.2f}"
                for name in EXPECTED_PRICES
            )

        # Re-seeding a populated database must not duplicate any seat category.
        seed_data(db)
        reseeded = list(db.scalars(select(SeatCategory)))
        assert len(reseeded) == len(categories)


def test_seat_category_price_range_and_uniqueness(client) -> None:
    screen_id = _create_extra_screen()

    with SessionLocal() as db:
        db.add(SeatCategory(screen_id=screen_id, name="NORMAL", price=Decimal("180.00")))
        db.commit()

    with SessionLocal() as db:
        db.add(SeatCategory(screen_id=screen_id, name="NORMAL", price=Decimal("180.00")))
        with pytest.raises(IntegrityError):
            db.commit()

    for invalid_price in (Decimal("0.00"), Decimal("10001.00")):
        with SessionLocal() as db:
            db.add(
                SeatCategory(
                    screen_id=screen_id,
                    name="EXECUTIVE",
                    price=invalid_price,
                )
            )
            with pytest.raises(IntegrityError):
                db.commit()
