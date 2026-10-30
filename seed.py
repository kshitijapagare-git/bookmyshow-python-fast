from sqlalchemy import select

from app.db import Base, SessionLocal, engine
from app.models import Movie

MOVIES = [
    {
        "title": "Pehla Pyaar Doosri Baar",
        "genres": "Comedy/Romantic",
        "poster_url": "https://picsum.photos/seed/pehla-pyaar/400/600",
        "rating": None,
        "votes": None,
        "likes": 10500,
        "promoted": True,
    },
    {
        "title": "Namit Malhotra's Ramayana",
        "genres": "Mythological/Drama",
        "poster_url": "https://picsum.photos/seed/ramayana/400/600",
        "rating": None,
        "votes": None,
        "likes": 359000,
        "promoted": False,
    },
    {
        "title": "Avengers Endgame: Encore",
        "genres": "Action/Sci-Fi",
        "poster_url": "https://picsum.photos/seed/avengers/400/600",
        "rating": 9.5,
        "votes": 46000,
        "likes": None,
        "promoted": False,
    },
    {
        "title": "Street Fighter",
        "genres": "Action/Adventure",
        "poster_url": "https://picsum.photos/seed/street-fighter/400/600",
        "rating": 7.3,
        "votes": 3900,
        "likes": None,
        "promoted": False,
    },
    {
        "title": "Udta Teer",
        "genres": "Comedy/Drama",
        "poster_url": "https://picsum.photos/seed/udta-teer/400/600",
        "rating": None,
        "votes": None,
        "likes": 17800,
        "promoted": False,
    },
    {
        "title": "Dune: Part Two",
        "genres": "Sci-Fi/Adventure",
        "poster_url": "https://picsum.photos/seed/dune/400/600",
        "rating": 8.9,
        "votes": 68000,
        "likes": None,
        "promoted": False,
    },
    {
        "title": "Inception",
        "genres": "Sci-Fi/Thriller",
        "poster_url": "https://picsum.photos/seed/inception/400/600",
        "rating": 9.4,
        "votes": 150000,
        "likes": None,
        "promoted": False,
    },
    {
        "title": "Kantara Chapter 1",
        "genres": "Action/Thriller",
        "poster_url": "https://picsum.photos/seed/kantara/400/600",
        "rating": None,
        "votes": None,
        "likes": 250000,
        "promoted": False,
    },
]


def seed_movies(db) -> None:
    if db.scalar(select(Movie.id).limit(1)) is not None:
        return

    db.add_all([Movie(**movie) for movie in MOVIES])
    db.commit()


def main() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_movies(db)


if __name__ == "__main__":
    main()
