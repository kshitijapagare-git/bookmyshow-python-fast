from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Movie
from ..schemas import MovieOut

router = APIRouter(prefix="/api/movies", tags=["movies"])


@router.get("", response_model=list[MovieOut])
def list_movies(db: Session = Depends(get_db)):
    movies = db.scalars(select(Movie).order_by(Movie.id))
    return [
        MovieOut(
            id=movie.id,
            title=movie.title,
            genres=movie.genres,
            posterUrl=movie.poster_url,
            rating=movie.rating,
            votes=movie.votes,
            likes=movie.likes,
            promoted=movie.promoted,
        )
        for movie in movies
    ]
