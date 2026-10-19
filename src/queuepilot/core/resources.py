"""Application-owned clients; no session is shared between requests or tasks."""

from dataclasses import dataclass
from typing import cast

from fastapi import Request
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from queuepilot.core.config import Settings


@dataclass(frozen=True)
class Resources:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    redis: Redis
    probe_timeout_seconds: float


def create_redis(settings: Settings) -> Redis:
    """Use bounded connections and timeouts; credentials stay inside settings."""
    return Redis.from_url(
        settings.redis_url.get_secret_value(),
        max_connections=10,
        socket_connect_timeout=settings.probe_timeout_seconds,
        socket_timeout=settings.probe_timeout_seconds,
        retry=Retry(NoBackoff(), 0),
        decode_responses=True,
    )


def get_resources(request: Request) -> Resources:
    return cast(Resources, request.app.state.resources)
