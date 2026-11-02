from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import Base, SessionLocal, engine
from .errors import register_error_handlers
from .routers import auth, cities, cinemas, movies, shows
from seed import seed_data


def init_db() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_data(db)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="BookMyShow API", version="0.1.0", lifespan=lifespan)
register_error_handlers(app)

app.include_router(cities.router)
app.include_router(cinemas.router)
app.include_router(movies.router)
app.include_router(shows.router)
app.include_router(auth.router)
app.include_router(auth.me_router)

init_db()
