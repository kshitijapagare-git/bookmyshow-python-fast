from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Base, SessionLocal, engine
from app.models import Cinema, City, Movie
from app.slug import slugify

MOVIES: list[dict[str, object]] = [
    {
        "title": "Pehla Pyaar Doosri Baar",
        "genres": "Comedy/Romantic",
        "poster_url": "https://picsum.photos/seed/pehla-pyaar/400/600",
        "rating": None,
        "votes": None,
        "likes": 10500,
        "promoted": True,
        "synopsis": (
            "Ten years after school, three best friends meet again at a reunion and discover "
            "that the crushes, rivalries and promises they left behind are still very much "
            "alive."
        ),
        "languages": ["Hindi"],
        "formats": ["2D"],
        "duration_minutes": 142,
        "certificate": "A",
        "backdrop_url": "https://picsum.photos/seed/pehla-pyaar/1600/900",
        "trailer_url": "https://www.youtube.com/watch?v=pehla-pyaar-trailer",
        "interested_count": 10500,
    },
    {
        "title": "Namit Malhotra's Ramayana",
        "genres": "Mythological/Drama",
        "poster_url": "https://picsum.photos/seed/ramayana/400/600",
        "rating": None,
        "votes": None,
        "likes": 359000,
        "promoted": False,
        "synopsis": (
            "An exiled prince gathers an army of misfits to win back his kingdom and rescue "
            "his wife from the demon king who rules the island of Lanka."
        ),
        "languages": ["Hindi", "Tamil", "Telugu"],
        "formats": ["3D", "IMAX"],
        "duration_minutes": 181,
        "certificate": "UA",
        "backdrop_url": "https://picsum.photos/seed/ramayana/1600/900",
        "trailer_url": "https://www.youtube.com/watch?v=ramayana-trailer",
        "interested_count": 359000,
    },
    {
        "title": "Avengers Endgame: Encore",
        "genres": "Action/Sci-Fi",
        "poster_url": "https://picsum.photos/seed/avengers/400/600",
        "rating": 9.5,
        "votes": 46000,
        "likes": None,
        "promoted": False,
        "synopsis": (
            "The remaining heroes travel through time to undo the snap that wiped out half of "
            "all life, knowing that only one of them may come back."
        ),
        "languages": ["English", "Hindi"],
        "formats": ["2D", "3D", "IMAX"],
        "duration_minutes": 181,
        "certificate": "UA",
        "backdrop_url": "https://picsum.photos/seed/avengers/1600/900",
        "trailer_url": "https://www.youtube.com/watch?v=avengers-encore-trailer",
        "interested_count": 125000,
    },
    {
        "title": "Street Fighter",
        "genres": "Action/Adventure",
        "poster_url": "https://picsum.photos/seed/street-fighter/400/600",
        "rating": 7.3,
        "votes": 3900,
        "likes": None,
        "promoted": False,
        "synopsis": (
            "A drifter who fights for cash in underground arenas is recruited by an "
            "international tournament and learns the fight was never about the prize money."
        ),
        "languages": ["English"],
        "formats": ["2D"],
        "duration_minutes": 118,
        "certificate": "U",
        "backdrop_url": "https://picsum.photos/seed/street-fighter/1600/900",
        "trailer_url": None,
        "interested_count": 32000,
    },
    {
        "title": "Udta Teer",
        "genres": "Comedy/Drama",
        "poster_url": "https://picsum.photos/seed/udta-teer/400/600",
        "rating": None,
        "votes": None,
        "likes": 17800,
        "promoted": False,
        "synopsis": (
            "A wedding planner in a small town has one week to pull off the season's biggest "
            "function while his own family falls apart around him."
        ),
        "languages": ["Hindi"],
        "formats": ["2D"],
        "duration_minutes": 127,
        "certificate": "UA",
        "backdrop_url": "https://picsum.photos/seed/udta-teer/1600/900",
        "trailer_url": "https://www.youtube.com/watch?v=udta-teer-trailer",
        "interested_count": 17800,
    },
    {
        "title": "Dune: Part Two",
        "genres": "Sci-Fi/Adventure",
        "poster_url": "https://picsum.photos/seed/dune/400/600",
        "rating": 8.9,
        "votes": 68000,
        "likes": None,
        "promoted": False,
        "synopsis": (
            "Paul Atreides unites with the Fremen to take revenge on the conspirators who "
            "destroyed his family, torn between love and the war that follows him."
        ),
        "languages": ["English", "Hindi"],
        "formats": ["IMAX", "4DX"],
        "duration_minutes": 166,
        "certificate": "UA",
        "backdrop_url": "https://picsum.photos/seed/dune/1600/900",
        "trailer_url": "https://www.youtube.com/watch?v=dune-part-two-trailer",
        "interested_count": 68000,
    },
    {
        "title": "Inception",
        "genres": "Sci-Fi/Thriller",
        "poster_url": "https://picsum.photos/seed/inception/400/600",
        "rating": 9.4,
        "votes": 150000,
        "likes": None,
        "promoted": False,
        "synopsis": (
            "A thief who steals secrets from dreams is offered a way home if he can plant an "
            "idea in a rival's mind instead, a job that costs him his grip on reality."
        ),
        "languages": ["English", "Hindi"],
        "formats": ["2D", "IMAX"],
        "duration_minutes": 148,
        "certificate": "UA",
        "backdrop_url": "https://picsum.photos/seed/inception/1600/900",
        "trailer_url": "https://www.youtube.com/watch?v=inception-trailer",
        "interested_count": 150000,
    },
    {
        "title": "Kantara Chapter 1",
        "genres": "Action/Thriller",
        "poster_url": "https://picsum.photos/seed/kantara/400/600",
        "rating": None,
        "votes": None,
        "likes": 250000,
        "promoted": False,
        "synopsis": (
            "A folk performer caught between a forest guardian's tradition and a land grabber's "
            "ambition takes on the men who want the village's sacred grove."
        ),
        "languages": ["Kannada", "Hindi"],
        "formats": ["2D"],
        "duration_minutes": 168,
        "certificate": "A",
        "backdrop_url": "https://picsum.photos/seed/kantara/1600/900",
        "trailer_url": "https://www.youtube.com/watch?v=kantara-chapter-one-trailer",
        "interested_count": 250000,
    },
]

CITIES: list[dict[str, str]] = [
    {"name": "Mumbai", "state": "Maharashtra"},
    {"name": "Pune", "state": "Maharashtra"},
    {"name": "Bengaluru", "state": "Karnataka"},
]

CINEMAS: list[dict[str, str]] = [
    {
        "name": "PVR Icon",
        "address": "Phoenix Palladium, Lower Parel",
        "city_slug": "mumbai",
    },
    {
        "name": "INOX Megaplex",
        "address": "R City Mall, Ghatkopar",
        "city_slug": "mumbai",
    },
    {
        "name": "Maison PVR",
        "address": "Jio World Drive, Bandra Kurla Complex",
        "city_slug": "mumbai",
    },
    {
        "name": "PVR Pavilion",
        "address": "Pavilion Mall, Senapati Bapat Road",
        "city_slug": "pune",
    },
    {
        "name": "INOX Bund Garden",
        "address": "Bund Garden Road, Sangamvadi",
        "city_slug": "pune",
    },
]

FIXED_RELEASE_DATES = (
    "2025-05-16",
    "2024-11-01",
    "2025-04-25",
    "2024-08-15",
    "2025-01-10",
    "2024-03-01",
    "2024-07-19",
)


def _movie_rows() -> list[dict[str, object]]:
    still_releasing = date.today() + timedelta(days=1)
    rows: list[dict[str, object]] = []
    for index, movie in enumerate(MOVIES):
        row: dict[str, object] = {**movie, "slug": slugify(str(movie["title"]))}
        row["release_date"] = (
            still_releasing
            if index == 0
            else date.fromisoformat(FIXED_RELEASE_DATES[index - 1])
        )
        rows.append(row)
    return rows


def seed_movies(db: Session) -> None:
    if db.scalar(select(Movie.id).limit(1)) is not None:
        return

    db.add_all([Movie(**row) for row in _movie_rows()])
    db.commit()


def seed_cities(db: Session) -> None:
    if db.scalar(select(City.id).limit(1)) is not None:
        return

    db.add_all(
        [
            City(name=city["name"], state=city["state"], slug=slugify(city["name"]))
            for city in CITIES
        ]
    )
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
            )
            for cinema in CINEMAS
        ]
    )
    db.commit()


def seed_all(db: Session) -> None:
    seed_movies(db)
    seed_cities(db)
    seed_cinemas(db)


def main() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_all(db)


if __name__ == "__main__":
    main()
