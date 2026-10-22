"""Lease and execute durable jobs with ticket-state rechecks."""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from queuepilot.jobs.models import ScheduledJob
from queuepilot.jobs.service import policy
from queuepilot.tickets.models import Ticket
from queuepilot.tickets.service import record

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class Claim:
    id: uuid.UUID
    ticket_id: uuid.UUID
    token: uuid.UUID


async def claim_batch(
    sessions: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    limit: int = 10,
) -> list[Claim]:
    timestamp = now or datetime.now(UTC)
    async with sessions() as session, session.begin():
        rows = (
            await session.scalars(
                select(ScheduledJob)
                .where(
                    ScheduledJob.state == "pending",
                    ScheduledJob.next_attempt_at <= timestamp,
                    (
                        ScheduledJob.lease_until.is_(None)
                        | (ScheduledJob.lease_until <= timestamp)
                    ),
                )
                .order_by(ScheduledJob.next_attempt_at, ScheduledJob.id)
                .limit(min(max(limit, 1), 50))
                .with_for_update(skip_locked=True)
            )
        ).all()
        claims = []
        for row in rows:
            token = uuid.uuid4()
            row.lease_token = token
            row.lease_until = timestamp + timedelta(seconds=30)
            row.attempts += 1
            claims.append(Claim(row.id, row.ticket_id, token))
        return claims


async def execute(
    sessions: async_sessionmaker[AsyncSession],
    claim: Claim,
    *,
    now: datetime | None = None,
) -> bool:
    timestamp = now or datetime.now(UTC)
    # Ticket -> policy -> job is the common lock order for ticket writers and workers.
    async with sessions() as session, session.begin():
        ticket = await session.get(Ticket, claim.ticket_id, with_for_update=True)
        if ticket is None:
            return False
        setting = await policy(session, lock=True)
        row = await session.scalar(
            select(ScheduledJob).where(ScheduledJob.id == claim.id).with_for_update()
        )
        if (
            row is None
            or row.state != "pending"
            or row.lease_token != claim.token
            or row.lease_until is None
            or row.lease_until <= timestamp
        ):
            return False
        valid = (
            row.kind == "escalation"
            and ticket.status == "open"
            and ticket.assignee_id is None
            and ticket.escalated_at is None
            and ticket.opened_at == row.source_at
        ) or (
            row.kind == "auto_close"
            and ticket.status == "resolved"
            and ticket.resolved_at == row.source_at
        )
        if not valid:
            row.state = "cancelled"
        else:
            minutes = (
                setting.escalation_minutes
                if row.kind == "escalation"
                else setting.auto_close_minutes
            )
            due = row.source_at + timedelta(minutes=minutes)
            if due > timestamp:
                row.due_at = due
                row.next_attempt_at = due
            else:
                ticket.version += 1
                ticket.updated_at = timestamp
                if row.kind == "escalation":
                    ticket.escalated_at = timestamp
                    record(session, ticket, None, "escalated", {}, timestamp)
                else:
                    ticket.status = "closed"
                    ticket.closed_at = timestamp
                    record(session, ticket, None, "auto_closed", {}, timestamp)
                row.state = "completed"
                row.completed_at = timestamp
        row.lease_token = None
        row.lease_until = None
        return row.state == "completed"


async def fail(
    sessions: async_sessionmaker[AsyncSession],
    claim: Claim,
    *,
    now: datetime | None = None,
) -> None:
    timestamp = now or datetime.now(UTC)
    async with sessions() as session, session.begin():
        row = await session.scalar(
            select(ScheduledJob)
            .where(ScheduledJob.id == claim.id, ScheduledJob.lease_token == claim.token)
            .with_for_update()
        )
        if row is None or row.state != "pending":
            return
        row.lease_token = None
        row.lease_until = None
        if row.attempts >= MAX_ATTEMPTS:
            row.state = "failed"
            row.failure_code = "execution_error"
        else:
            row.next_attempt_at = timestamp + timedelta(
                seconds=min(60, 2 ** (row.attempts - 1))
            )


async def run_once(
    sessions: async_sessionmaker[AsyncSession], *, now: datetime | None = None
) -> int:
    claims = await claim_batch(sessions, now=now)
    for claim in claims:
        try:
            await execute(sessions, claim, now=now)
        except Exception:
            logger.exception(
                "Scheduled job execution failed", extra={"job_id": str(claim.id)}
            )
            await fail(sessions, claim, now=now)
    return len(claims)
