import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from .crypto.config import CryptoSettings
from .crypto.keys import load_key_pair
from .crypto.middleware import CryptoMiddleware
from .crypto.sessions import SessionStore
from .db import Base, SessionLocal, engine
from .delivery import DeliveryPolicy
from .errors import register_error_handlers
from .kafka_producer import KafkaSettings, build_event_publisher
from .payments_client import PaymentsSettings
from .rabbit_publisher import RabbitSettings, build_rabbit_publisher
from .reviews_client import ReviewsSettings
from .routers import (
    auth,
    bookings,
    cities,
    cinemas,
    crypto,
    events,
    groups,
    holds,
    internal_bookings,
    movies,
    reviews,
    seats,
    shows,
    transfers,
    users,
)
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
    _app.state.event_publisher = build_event_publisher(_app.state.kafka_settings)
    _app.state.rabbit_publisher = build_rabbit_publisher(_app.state.rabbit_settings)
    sweeper = asyncio.create_task(_sweep_expired_holds())
    try:
        yield
    finally:
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper
        if _app.state.event_publisher is not None:
            _app.state.event_publisher.close()
        if _app.state.rabbit_publisher is not None:
            _app.state.rabbit_publisher.close()


def create_app(settings: CryptoSettings | None = None) -> FastAPI:
    """Build the app; the crypto key pair and session store live on app.state."""
    resolved = settings if settings is not None else CryptoSettings.from_env()
    application = FastAPI(title="BookMyShow API", version="0.1.0", lifespan=lifespan)
    application.state.crypto_keys = load_key_pair(resolved)
    application.state.crypto_sessions = SessionStore()
    application.state.payments_settings = PaymentsSettings.from_env()
    application.state.reviews_settings = ReviewsSettings.from_env()
    application.state.kafka_settings = KafkaSettings.from_env()
    application.state.rabbit_settings = RabbitSettings.from_env()
    application.state.delivery_policy = DeliveryPolicy.from_env()

    if resolved.enabled:
        application.add_middleware(
            CryptoMiddleware,
            keys=application.state.crypto_keys,
            sessions=application.state.crypto_sessions,
        )

    register_error_handlers(application)

    application.include_router(cities.router)
    application.include_router(cinemas.router)
    application.include_router(movies.router)
    application.include_router(reviews.router)
    application.include_router(shows.router)
    application.include_router(seats.router)
    application.include_router(auth.router)
    application.include_router(auth.me_router)
    application.include_router(users.router)
    application.include_router(holds.router)
    application.include_router(bookings.router)
    application.include_router(internal_bookings.router)
    application.include_router(transfers.router)
    application.include_router(groups.router)
    application.include_router(crypto.router)
    application.include_router(events.router)
    return application


app = create_app()

init_db()
