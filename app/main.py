from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import Base, SessionLocal, engine
from .problem_details import register_problem_detail_handlers
from .routers import cities, cinemas, movies
from seed import seed_all


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_all(db)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="BookMyShow API", version="0.1.0", lifespan=lifespan)

register_problem_detail_handlers(app)

app.include_router(cities.router)
app.include_router(cinemas.router)
app.include_router(movies.router)

init_db()
