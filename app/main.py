import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from .db import Base, SessionLocal, engine
from .errors import register_error_handlers
from .routers import auth, cities, cinemas, holds, movies, seats, shows
from .seating import HOLD_SWEEP_INTERVAL_SECONDS, release_expired_holds
from seed import seed_data


def init_db() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_data(db)


async def _sweep_expired_holds() -> None:
    """Background replacement for @Scheduled: free the seats of holds past their time."""
    while True:
        await asyncio.sleep(HOLD_SWEEP_INTERVAL_SECONDS)
        with SessionLocal() as db:
            release_expired_holds(db)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    sweeper = asyncio.create_task(_sweep_expired_holds())
    try:
        yield
    finally:
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper


app = FastAPI(title="BookMyShow API", version="0.1.0", lifespan=lifespan)
register_error_handlers(app)

app.include_router(cities.router)
app.include_router(cinemas.router)
app.include_router(movies.router)
app.include_router(shows.router)
app.include_router(seats.router)
app.include_router(auth.router)
app.include_router(auth.me_router)
app.include_router(holds.router)

init_db()
