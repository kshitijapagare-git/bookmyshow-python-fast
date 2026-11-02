from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Base, SessionLocal, engine
from app.models import Cinema, City, Movie, Screen, Show
from app.timeutil import utc_from_ist

CITIES: list[dict[str, str]] = [
    {"name": "Mumbai", "state": "Maharashtra", "slug": "mumbai"},
    {"name": "Pune", "state": "Maharashtra", "slug": "pune"},
    {"name": "Bengaluru", "state": "Karnataka", "slug": "bengaluru"},
]

CINEMAS: list[dict[str, Any]] = [
    {
        "name": "PVR Icon Phoenix Mills",
        "address": "Phoenix Mills, Lower Parel",
        "city_slug": "mumbai",
        "chain_logo_url": "https://picsum.photos/seed/pvr/64/64",
        "cancellable": True,
        "amenities": ["M_TICKET", "FOOD", "PARKING"],
    },
    {
        "name": "INOX Megaplex Inorbit Mall",
        "address": "Inorbit Mall, Malad West",
        "city_slug": "mumbai",
        "chain_logo_url": "https://picsum.photos/seed/inox/64/64",
        "cancellable": True,
        "amenities": ["M_TICKET", "FOOD", "PARKING", "WHEELCHAIR"],
    },
    {
        "name": "Maison PVR Jio World Drive",
        "address": "Jio World Drive, Bandra Kurla Complex",
        "city_slug": "mumbai",
        "chain_logo_url": "https://picsum.photos/seed/maison-pvr/64/64",
        "cancellable": False,
        "amenities": ["M_TICKET", "FOOD", "WHEELCHAIR"],
    },
    {
        "name": "PVR Pavilion Mall",
        "address": "Pavilion Mall, Senapati Bapat Road",
        "city_slug": "pune",
        "chain_logo_url": "https://picsum.photos/seed/pvr/64/64",
        "cancellable": False,
        "amenities": ["M_TICKET", "PARKING"],
    },
    {
        "name": "INOX Bund Garden Road",
        "address": "Bund Garden Road, Camp",
        "city_slug": "pune",
        "chain_logo_url": "https://picsum.photos/seed/inox/64/64",
        "cancellable": True,
        "amenities": ["FOOD", "PARKING", "WHEELCHAIR"],
    },
]

SCREEN_NAMES: list[str] = ["Screen 1", "Screen 2"]

SHOW_DAYS = 7
SHOW_START_TIMES: list[tuple[int, int]] = [(8, 15), (9, 0), (13, 0), (18, 30), (23, 15)]
SHOW_FORMATS: list[str] = ["2K LASER DOLBY 7.1", "2K DOLBY 7.1", "IMAX 3D"]
SHOW_CAPACITY = 100
SEATS_SOLD_FAST_FILLING = 70
SEATS_SOLD_AVAILABLE = 69

MOVIES: list[dict[str, Any]] = [
    {
        "title": "Pehla Pyaar Doosri Baar",
        "slug": "pehla-pyaar-doosri-baar",
        "genres": "Comedy/Romantic",
        "poster_url": "https://picsum.photos/seed/pehla-pyaar/400/600",
        "rating": None,
        "votes": None,
        "likes": 10500,
        "promoted": True,
        "synopsis": (
            "Ten years after school, three best friends reunite for a wedding and discover that "
            "the feelings they left behind are still waiting for them."
        ),
        "languages": ["Hindi"],
        "formats": ["2D"],
        "duration_minutes": 142,
        "certificate": "A",
        "release_date": date.today() + timedelta(days=1),
        "backdrop_url": "https://picsum.photos/seed/pehla-pyaar/1600/900",
        "trailer_url": "https://www.youtube.com/watch?v=pehla-pyaar-trailer",
        "interested_count": 10500,
    },
    {
        "title": "Namit Malhotra's Ramayana",
        "slug": "namit-malhotras-ramayana",
        "genres": "Mythological/Drama",
        "poster_url": "https://picsum.photos/seed/ramayana/400/600",
        "rating": None,
        "votes": None,
        "likes": 359000,
        "promoted": False,
        "synopsis": (
            "A retelling of the epic Ramayana, following Prince Rama as he is exiled from his "
            "kingdom and sets out to rescue Sita from the demon king Ravana."
        ),
        "languages": ["Hindi", "Tamil", "Telugu"],
        "formats": ["2D", "3D", "IMAX"],
        "duration_minutes": 180,
        "certificate": "UA",
        "release_date": date(2026, 10, 23),
        "backdrop_url": "https://picsum.photos/seed/ramayana/1600/900",
        "trailer_url": "https://www.youtube.com/watch?v=ramayana-trailer",
        "interested_count": 359000,
    },
    {
        "title": "Avengers Endgame: Encore",
        "slug": "avengers-endgame-encore",
        "genres": "Action/Sci-Fi",
        "poster_url": "https://picsum.photos/seed/avengers/400/600",
        "rating": 9.5,
        "votes": 46000,
        "likes": None,
        "promoted": False,
        "synopsis": (
            "The remaining Avengers assemble one final time to undo the devastation caused by "
            "Thanos and restore balance to the universe."
        ),
        "languages": ["English"],
        "formats": ["2D", "3D", "IMAX"],
        "duration_minutes": 181,
        "certificate": "UA",
        "release_date": date(2025, 4, 25),
        "backdrop_url": "https://picsum.photos/seed/avengers/1600/900",
        "trailer_url": "https://www.youtube.com/watch?v=avengers-encore-trailer",
        "interested_count": 46000,
    },
    {
        "title": "Street Fighter",
        "slug": "street-fighter",
        "genres": "Action/Adventure",
        "poster_url": "https://picsum.photos/seed/street-fighter/400/600",
        "rating": 7.3,
        "votes": 3900,
        "likes": None,
        "promoted": False,
        "synopsis": (
            "A retired martial artist is pulled back into the world of underground fighting "
            "when his old rival challenges him to one last tournament."
        ),
        "languages": ["English", "Hindi"],
        "formats": ["2D"],
        "duration_minutes": 118,
        "certificate": "UA",
        "release_date": date(2025, 3, 14),
        "backdrop_url": "https://picsum.photos/seed/street-fighter/1600/900",
        "trailer_url": None,
        "interested_count": 3900,
    },
    {
        "title": "Udta Teer",
        "slug": "udta-teer",
        "genres": "Comedy/Drama",
        "poster_url": "https://picsum.photos/seed/udta-teer/400/600",
        "rating": None,
        "votes": None,
        "likes": 17800,
        "promoted": False,
        "synopsis": (
            "A small-town archery champion chases a national medal while his family argues "
            "about everything except the one thing that matters."
        ),
        "languages": ["Hindi"],
        "formats": ["2D"],
        "duration_minutes": 131,
        "certificate": "A",
        "release_date": date(2025, 8, 1),
        "backdrop_url": "https://picsum.photos/seed/udta-teer/1600/900",
        "trailer_url": "https://www.youtube.com/watch?v=udta-teer-trailer",
        "interested_count": 17800,
    },
    {
        "title": "Dune: Part Two",
        "slug": "dune-part-two",
        "genres": "Sci-Fi/Adventure",
        "poster_url": "https://picsum.photos/seed/dune/400/600",
        "rating": 8.9,
        "votes": 68000,
        "likes": None,
        "promoted": False,
        "synopsis": (
            "Paul Atreides unites with the Fremen to seek revenge against the conspirators who "
            "destroyed his family, while trying to prevent a terrible future only he can see."
        ),
        "languages": ["English", "Hindi"],
        "formats": ["2D", "IMAX", "4DX"],
        "duration_minutes": 166,
        "certificate": "UA",
        "release_date": date(2024, 3, 1),
        "backdrop_url": "https://picsum.photos/seed/dune/1600/900",
        "trailer_url": "https://www.youtube.com/watch?v=dune-part-two-trailer",
        "interested_count": 68000,
    },
    {
        "title": "Inception",
        "slug": "inception",
        "genres": "Sci-Fi/Thriller",
        "poster_url": "https://picsum.photos/seed/inception/400/600",
        "rating": 9.4,
        "votes": 150000,
        "likes": None,
        "promoted": False,
        "synopsis": (
            "A thief who steals corporate secrets through dream-sharing technology is offered "
            "a chance to have his past erased if he can plant an idea in a target's mind."
        ),
        "languages": ["English", "Hindi"],
        "formats": ["2D", "IMAX"],
        "duration_minutes": 148,
        "certificate": "UA",
        "release_date": date(2010, 7, 16),
        "backdrop_url": "https://picsum.photos/seed/inception/1600/900",
        "trailer_url": "https://www.youtube.com/watch?v=inception-trailer",
        "interested_count": 150000,
    },
    {
        "title": "Kantara Chapter 1",
        "slug": "kantara-chapter-1",
        "genres": "Action/Thriller",
        "poster_url": "https://picsum.photos/seed/kantara/400/600",
        "rating": None,
        "votes": None,
        "likes": 250000,
        "promoted": False,
        "synopsis": (
            "A folk hero takes on a powerful adversary as the traditions of his coastal village "
            "collide with the ambitions of the men who rule it."
        ),
        "languages": ["Kannada", "Hindi"],
        "formats": ["2D", "IMAX"],
        "duration_minutes": 170,
        "certificate": "UA",
        "release_date": date(2025, 10, 2),
        "backdrop_url": "https://picsum.photos/seed/kantara/1600/900",
        "trailer_url": None,
        "interested_count": 250000,
    },
]


def seed_cities(db: Session) -> None:
    if db.scalar(select(City.id).limit(1)) is not None:
        return

    db.add_all([City(**city) for city in CITIES])
    db.commit()


def seed_cinemas(db: Session) -> None:
    if db.scalar(select(Cinema.id).limit(1)) is not None:
        return

    city_ids = {city.slug: city.id for city in db.scalars(select(City))}
    db.add_all(
        [
            Cinema(
                name=cinema["name"],
                address=cinema["address"],
                city_id=city_ids[cinema["city_slug"]],
                chain_logo_url=cinema["chain_logo_url"],
                cancellable=cinema["cancellable"],
                amenities=cinema["amenities"],
            )
            for cinema in CINEMAS
        ]
    )
    db.commit()


def seed_movies(db: Session) -> None:
    if db.scalar(select(Movie.id).limit(1)) is not None:
        return

    db.add_all([Movie(**movie) for movie in MOVIES])
    db.commit()


def seed_screens(db: Session) -> None:
    if db.scalar(select(Screen.id).limit(1)) is not None:
        return

    db.add_all(
        [
            Screen(cinema_id=cinema.id, name=name)
            for cinema in db.scalars(select(Cinema).order_by(Cinema.id))
            for name in SCREEN_NAMES
        ]
    )
    db.commit()


def seed_shows(db: Session) -> None:
    if db.scalar(select(Show.id).limit(1)) is not None:
        return

    city_ids = {city.slug: city.id for city in db.scalars(select(City))}
    movies = list(db.scalars(select(Movie).order_by(Movie.id)))
    screens_by_cinema: dict[int, list[Screen]] = {}
    for screen in db.scalars(select(Screen).order_by(Screen.id)):
        screens_by_cinema.setdefault(screen.cinema_id, []).append(screen)

    shows: list[Show] = []
    for cinema in db.scalars(select(Cinema).order_by(Cinema.id)):
        cinema_screens = screens_by_cinema.get(cinema.id, [])
        if not cinema_screens:
            continue
        cinema_movies = movies if cinema.city_id == city_ids["mumbai"] else movies[:2]
        for offset in range(SHOW_DAYS):
            day = date.today() + timedelta(days=offset)
            for index, movie in enumerate(cinema_movies):
                language = movie.languages[offset % len(movie.languages)].lower()
                for slot, (hour, minute) in enumerate(SHOW_START_TIMES):
                    fast_filling = (index + offset + slot) % 2 == 0
                    shows.append(
                        Show(
                            screen_id=cinema_screens[slot % len(cinema_screens)].id,
                            movie_id=movie.id,
                            starts_at=utc_from_ist(day, hour, minute),
                            format=SHOW_FORMATS[(index + slot) % len(SHOW_FORMATS)],
                            language=language,
                            capacity=SHOW_CAPACITY,
                            seats_sold=(
                                SEATS_SOLD_FAST_FILLING
                                if fast_filling
                                else SEATS_SOLD_AVAILABLE
                            ),
                        )
                    )

    db.add_all(shows)
    db.commit()


def seed_data(db: Session) -> None:
    seed_cities(db)
    seed_cinemas(db)
    seed_movies(db)
    seed_screens(db)
    seed_shows(db)


def main() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_data(db)


if __name__ == "__main__":
    main()
