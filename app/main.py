from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import Base, SessionLocal, engine
from .routers import bookings, cities, cinemas, movies, transfers
from seed import seed_movies


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_movies(db)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="BookMyShow API", version="0.1.0", lifespan=lifespan)

app.include_router(cities.router)
app.include_router(cinemas.router)
app.include_router(movies.router)
app.include_router(bookings.router)
app.include_router(transfers.router)

init_db()
