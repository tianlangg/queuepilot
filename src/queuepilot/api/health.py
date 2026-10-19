"""Public probes expose only bounded, sanitized dependency status."""

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from queuepilot.core.resources import Resources, get_resources
from queuepilot.db.base import SCHEMA_REVISION

router = APIRouter(prefix="/health", tags=["health"])


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded", "not_ready"]
    database: Literal["ok", "unavailable"]
    redis: Literal["ok", "unavailable"]


async def database_ready(resources: Resources) -> bool:
    try:
        async with asyncio.timeout(resources.probe_timeout_seconds):
            async with resources.engine.connect() as connection:
                revision = await connection.scalar(
                    text("SELECT version_num FROM alembic_version")
                )
                return bool(revision == SCHEMA_REVISION)
    except (SQLAlchemyError, OSError, TimeoutError):
        return False


async def redis_ready(resources: Resources) -> bool:
    try:
        async with asyncio.timeout(resources.probe_timeout_seconds):
            return bool(await resources.redis.ping())
    except (RedisError, OSError, TimeoutError):
        return False


@router.get("/live", response_model=LivenessResponse)
async def live() -> LivenessResponse:
    return LivenessResponse()


@router.get("/ready", response_model=ReadinessResponse)
async def ready(
    response: Response,
    resources: Annotated[Resources, Depends(get_resources)],
) -> ReadinessResponse:
    database_ok, redis_ok = await asyncio.gather(
        database_ready(resources), redis_ready(resources)
    )
    response.headers["Cache-Control"] = "no-store"
    if not database_ok:
        response.status_code = 503
    return ReadinessResponse(
        status="not_ready" if not database_ok else "ready" if redis_ok else "degraded",
        database="ok" if database_ok else "unavailable",
        redis="ok" if redis_ok else "unavailable",
    )
