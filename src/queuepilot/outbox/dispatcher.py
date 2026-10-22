"""Bounded PostgreSQL claims and at-least-once Redis publication."""

import asyncio
import logging
import signal
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from queuepilot.core.config import Settings
from queuepilot.core.resources import create_redis
from queuepilot.db.session import create_engine
from queuepilot.jobs.worker import run_once as run_jobs_once
from queuepilot.realtime.models import ConnectionCredential
from queuepilot.tickets.models import OutboxEvent

CHANNEL = "queuepilot.events.v1"
logger = logging.getLogger(__name__)


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: uuid.UUID
    ticket_id: uuid.UUID
    ticket_version: int = Field(ge=1)
    event_type: Literal[
        "created",
        "commented",
        "transitioned",
        "claimed",
        "reassigned",
        "escalated",
        "auto_closed",
    ]
    timestamp: AwareDatetime
    visibility: Literal["public", "internal"] | None = None


@dataclass(frozen=True)
class Claim:
    id: uuid.UUID
    token: uuid.UUID
    event: Event


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(60, 2 ** min(attempts - 1, 6)))


async def claim_batch(
    sessions: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    limit: int = 50,
) -> list[Claim]:
    timestamp = now or datetime.now(UTC)
    async with sessions() as session, session.begin():
        rows = list(
            (
                await session.scalars(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.published_at.is_(None),
                        OutboxEvent.available_at <= timestamp,
                        (
                            OutboxEvent.lease_until.is_(None)
                            | (OutboxEvent.lease_until <= timestamp)
                        ),
                    )
                    .order_by(OutboxEvent.available_at, OutboxEvent.id)
                    .limit(min(max(limit, 1), 50))
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        claims = []
        for row in rows:
            token = uuid.uuid4()
            row.lease_token = token
            row.lease_until = timestamp + timedelta(seconds=30)
            row.attempts += 1
            claims.append(
                Claim(
                    row.id,
                    token,
                    Event.model_validate(
                        {
                            "event_id": row.id,
                            "ticket_id": row.ticket_id,
                            "ticket_version": row.ticket_version,
                            "event_type": row.event_type,
                            "timestamp": row.created_at,
                            "visibility": row.payload.get("visibility"),
                        }
                    ),
                )
            )
        return claims


async def finish(
    sessions: async_sessionmaker[AsyncSession],
    claim: Claim,
    success: bool,
    *,
    now: datetime | None = None,
) -> None:
    timestamp = now or datetime.now(UTC)
    async with sessions() as session, session.begin():
        values: dict[str, object] = {"lease_token": None, "lease_until": None}
        if success:
            values["published_at"] = timestamp
        else:
            row = await session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.id == claim.id, OutboxEvent.lease_token == claim.token
                )
            )
            if row is None:
                return
            values["available_at"] = timestamp + backoff(row.attempts)
        await session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == claim.id,
                OutboxEvent.lease_token == claim.token,
                OutboxEvent.published_at.is_(None),
            )
            .values(**values)
        )


async def dispatch_once(
    sessions: async_sessionmaker[AsyncSession], redis: Redis
) -> int:
    claims = await claim_batch(sessions, limit=10)
    for claim in claims:
        try:
            await redis.publish(CHANNEL, claim.event.model_dump_json())
        except (RedisError, OSError, TimeoutError):
            await finish(sessions, claim, False)
            logger.warning("Outbox publish unavailable")
        else:
            await finish(sessions, claim, True)
    return len(claims)


async def cleanup_credentials(sessions: async_sessionmaker[AsyncSession]) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=1)
    async with sessions() as session, session.begin():
        expired = (
            select(ConnectionCredential.id)
            .where(ConnectionCredential.expires_at < cutoff)
            .order_by(ConnectionCredential.expires_at)
            .limit(500)
        )
        await session.execute(
            delete(ConnectionCredential).where(ConnectionCredential.id.in_(expired))
        )


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    engine = create_engine(settings)
    redis = create_redis(settings)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    failures = 0
    next_cleanup = time.monotonic()
    try:
        while not stop.is_set():
            try:
                if time.monotonic() >= next_cleanup:
                    await cleanup_credentials(sessions)
                    next_cleanup = time.monotonic() + 60
                count = await run_jobs_once(sessions)
                count += await dispatch_once(sessions, redis)
                failures = 0
            except Exception:
                logger.warning("Outbox dispatcher cycle failed")
                count = 0
                failures = min(failures + 1, 5)
            if count == 0:
                try:
                    await asyncio.wait_for(
                        stop.wait(),
                        timeout=max(0.2, 2 ** (failures - 1)) if failures else 0.2,
                    )
                except TimeoutError:
                    pass
    finally:
        await redis.aclose()
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    stop = asyncio.Event()

    async def serve() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run_worker(Settings(), stop)

    asyncio.run(serve())


if __name__ == "__main__":
    main()
