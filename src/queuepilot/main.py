"""ASGI application factory and process lifecycle."""

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import async_sessionmaker

from queuepilot.api.health import router as health_router
from queuepilot.core.config import Settings
from queuepilot.core.resources import Resources, create_redis
from queuepilot.db.session import create_engine

logger = logging.getLogger(__name__)


class RootResponse(BaseModel):
    """Public response contract for the root endpoint."""

    message: str


def create_app(settings: Settings | None = None) -> FastAPI:
    """Validate config now, acquire lazy clients at startup, close on shutdown."""
    config = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            engine = create_engine(config)
            stack.push_async_callback(engine.dispose)
            redis = create_redis(config)
            stack.push_async_callback(redis.aclose)
            app.state.resources = Resources(
                engine=engine,
                sessions=async_sessionmaker(engine, expire_on_commit=False),
                redis=redis,
                probe_timeout_seconds=config.probe_timeout_seconds,
            )
            logger.info("Starting %s", app.title)
            try:
                yield
            finally:
                del app.state.resources
                logger.info("Stopped %s", app.title)

    app = FastAPI(title=config.app_name, lifespan=lifespan)
    app.include_router(health_router)

    @app.get("/", response_model=RootResponse)
    async def root() -> RootResponse:
        return RootResponse(message=f"Welcome to {config.app_name}")

    return app
