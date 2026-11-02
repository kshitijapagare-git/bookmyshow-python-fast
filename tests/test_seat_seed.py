from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Screen, Seat, SeatCategory, Show, ShowSeat
from seed import seed_data

FULL_LAYOUT_ROWS = ["D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P", "Q"]
EXPECTED_CATEGORIES = {"NORMAL", "EXECUTIVE", "PREMIUM", "PREMIUM_XL", "PREMIUM_PLUS"}


def _seat_counts_by_screen() -> dict[int, int]:
    with SessionLocal() as db:
        rows = db.execute(
            select(Seat.screen_id, func.count(Seat.id)).group_by(Seat.screen_id)
        )
        return {screen_id: count for screen_id, count in rows.all()}


def test_full_screen_layout_covers_rows_and_categories(client) -> None:
    with SessionLocal() as db:
        full_screen_id = db.scalar(select(Seat.screen_id).where(Seat.row == "Q").limit(1))
        assert full_screen_id is not None
        seats = list(
            db.scalars(
                select(Seat)
                .where(Seat.screen_id == full_screen_id)
                .order_by(Seat.row, Seat.number)
            )
        )
        category_names = {
            category.id: category.name
            for category in db.scalars(
                select(SeatCategory).where(SeatCategory.screen_id == full_screen_id)
            )
        }

    assert {seat.row for seat in seats} == set(FULL_LAYOUT_ROWS)
    assert {category_names[seat.category_id] for seat in seats} == EXPECTED_CATEGORIES

    seats_by_row: dict[str, list] = {}
    for seat in seats:
        seats_by_row.setdefault(seat.row, []).append(seat)

    assert set(seats_by_row) == set(FULL_LAYOUT_ROWS)
    for row, row_seats in seats_by_row.items():
        numbers = [seat.number for seat in row_seats]
        assert numbers == list(range(1, len(numbers) + 1)), row
        assert any(seat.gap_before for seat in row_seats), row

    wheelchair_seats = [seat for seat in seats if seat.kind == "WHEELCHAIR"]
    assert len(wheelchair_seats) == 1
    assert len([seat for seat in seats if seat.bestseller]) >= 3

    screen_seat_counts = _seat_counts_by_screen()
    with SessionLocal() as db:
        screen_ids = {screen.id for screen in db.scalars(select(Screen))}

    assert set(screen_seat_counts) == screen_ids
    assert all(count > 0 for count in screen_seat_counts.values())


def test_every_show_has_a_show_seat_per_seat(client) -> None:
    screen_seat_counts = _seat_counts_by_screen()

    with SessionLocal() as db:
        shows_per_screen = {
            screen_id: count
            for screen_id, count in db.execute(
                select(Show.screen_id, func.count(Show.id)).group_by(Show.screen_id)
            ).all()
        }
        show_seats_per_show = {
            show_id: count
            for show_id, count in db.execute(
                select(ShowSeat.show_id, func.count(ShowSeat.id)).group_by(
                    ShowSeat.show_id
                )
            ).all()
        }
        total_show_seats = db.scalar(select(func.count(ShowSeat.id)))
        sold_show_seats = db.scalar(
            select(func.count(ShowSeat.id)).where(ShowSeat.status == "SOLD")
        )
        shows = list(db.scalars(select(Show)))

    assert shows
    assert total_show_seats == sum(
        screen_seat_counts[screen_id] * count
        for screen_id, count in shows_per_screen.items()
    )
    assert set(show_seats_per_show) == {show.id for show in shows}
    for show in shows:
        assert show_seats_per_show[show.id] == screen_seat_counts[show.screen_id]
    assert sold_show_seats


def test_seeding_twice_does_not_duplicate_seats(client) -> None:
    with SessionLocal() as db:
        seats_before = db.scalar(select(func.count(Seat.id)))
        show_seats_before = db.scalar(select(func.count(ShowSeat.id)))
        assert seats_before and show_seats_before

        seed_data(db)

        assert db.scalar(select(func.count(Seat.id))) == seats_before
        assert db.scalar(select(func.count(ShowSeat.id))) == show_seats_before
