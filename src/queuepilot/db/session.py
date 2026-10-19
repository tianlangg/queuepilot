"""Bounded async PostgreSQL pools and request-local sessions."""

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from queuepilot.core.config import Settings
from queuepilot.core.resources import get_resources


def create_engine(settings: Settings) -> AsyncEngine:
    """Construct a lazy pool; dependency outages do not prevent liveness."""
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=5,
        connect_args={
            "connect_timeout": 5,
            "options": "-c timezone=UTC -c statement_timeout=5000 -c lock_timeout=3000",
        },
        hide_parameters=True,
    )


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Close/roll back on exit; the calling service must own explicit commits."""
    async with get_resources(request).sessions() as session:
        yield session
