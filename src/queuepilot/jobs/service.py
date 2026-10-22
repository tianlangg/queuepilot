"""Policy changes and atomic ticket scheduling; callers own transactions."""

import uuid
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from queuepilot.auth.service import Principal
from queuepilot.jobs.models import PolicyAudit, ScheduledJob, SchedulingPolicy
from queuepilot.tickets.models import Ticket


class PolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    escalation_minutes: int
    auto_close_minutes: int
    version: int


class PolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    escalation_minutes: int = Field(ge=1, le=525600)
    auto_close_minutes: int = Field(ge=1, le=525600)
    expected_version: int = Field(ge=1)


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    ticket_id: uuid.UUID
    kind: str
    state: str
    due_at: datetime
    next_attempt_at: datetime
    attempts: int
    failure_code: str | None


async def policy(session: AsyncSession, *, lock: bool = False) -> SchedulingPolicy:
    query = select(SchedulingPolicy).where(SchedulingPolicy.id == 1)
    if lock:
        query = query.with_for_update(read=True)
    result = await session.scalar(query)
    if result is None:
        raise RuntimeError("Scheduling policy missing")
    return result


async def schedule(session: AsyncSession, ticket: Ticket, now: datetime) -> None:
    """Called with the ticket row locked (or before new ticket is visible)."""
    setting = await policy(session, lock=True)
    rows = list(
        (
            await session.scalars(
                select(ScheduledJob)
                .where(
                    ScheduledJob.ticket_id == ticket.id,
                    ScheduledJob.state.in_(["pending", "failed"]),
                )
                .with_for_update()
            )
        ).all()
    )
    kind: str | None = None
    source: datetime | None = None
    minutes = 0
    if (
        ticket.status == "open"
        and ticket.assignee_id is None
        and ticket.escalated_at is None
    ):
        kind, source, minutes = (
            "escalation",
            ticket.opened_at,
            setting.escalation_minutes,
        )
    elif ticket.status == "resolved" and ticket.resolved_at is not None:
        kind, source, minutes = (
            "auto_close",
            ticket.resolved_at,
            setting.auto_close_minutes,
        )
    for row in rows:
        if row.kind != kind or row.source_at != source:
            row.state = "cancelled"
            row.lease_token = None
            row.lease_until = None
    if (
        kind is not None
        and source is not None
        and not any(row.kind == kind and row.source_at == source for row in rows)
    ):
        due = source + timedelta(minutes=minutes)
        session.add(
            ScheduledJob(
                ticket_id=ticket.id,
                kind=kind,
                effect_key=f"{kind}:{ticket.id}:{ticket.version}",
                source_at=source,
                source_version=ticket.version,
                due_at=due,
                next_attempt_at=due,
                state="pending",
                attempts=0,
            )
        )


async def get_policy(session: AsyncSession, principal: Principal) -> PolicyResponse:
    if principal.role != "admin":
        raise PermissionError
    return PolicyResponse.model_validate(await policy(session))


async def change_policy(
    session: AsyncSession,
    principal: Principal,
    body: PolicyUpdate,
    now: datetime | None = None,
) -> PolicyResponse:
    if principal.role != "admin":
        raise PermissionError
    timestamp = now or datetime.now(UTC)
    async with session.begin():
        setting = await session.scalar(
            select(SchedulingPolicy).where(SchedulingPolicy.id == 1).with_for_update()
        )
        if setting is None:
            raise RuntimeError("Scheduling policy missing")
        if setting.version != body.expected_version:
            raise ValueError("Policy version conflict")
        old_escalation, old_close = (
            setting.escalation_minutes,
            setting.auto_close_minutes,
        )
        setting.escalation_minutes = body.escalation_minutes
        setting.auto_close_minutes = body.auto_close_minutes
        setting.version += 1
        session.add(
            PolicyAudit(
                actor_id=principal.id,
                policy_version=setting.version,
                old_escalation_minutes=old_escalation,
                old_auto_close_minutes=old_close,
                escalation_minutes=body.escalation_minutes,
                auto_close_minutes=body.auto_close_minutes,
                created_at=timestamp,
            )
        )
        # Due times are derived from the original state-entry timestamps.
        for kind, minutes in (
            ("escalation", body.escalation_minutes),
            ("auto_close", body.auto_close_minutes),
        ):
            await session.execute(
                update(ScheduledJob)
                .where(ScheduledJob.state == "pending", ScheduledJob.kind == kind)
                .values(
                    due_at=ScheduledJob.source_at + timedelta(minutes=minutes),
                    next_attempt_at=ScheduledJob.source_at + timedelta(minutes=minutes),
                )
            )
        result = PolicyResponse.model_validate(setting)
    return result


async def list_jobs(
    session: AsyncSession,
    principal: Principal,
    state: str,
    limit: int,
    after: uuid.UUID | None,
) -> list[JobResponse]:
    if principal.role != "admin":
        raise PermissionError
    query = select(ScheduledJob).where(ScheduledJob.state == state)
    if after is not None:
        query = query.where(ScheduledJob.id > after)
    rows = (await session.scalars(query.order_by(ScheduledJob.id).limit(limit))).all()
    return [JobResponse.model_validate(row) for row in rows]


async def retry_job(
    session: AsyncSession,
    principal: Principal,
    job_id: uuid.UUID,
    now: datetime | None = None,
) -> JobResponse:
    if principal.role != "admin":
        raise PermissionError
    timestamp = now or datetime.now(UTC)
    async with session.begin():
        row = await session.get(ScheduledJob, job_id, with_for_update=True)
        if row is None:
            raise LookupError
        if row.state != "failed":
            raise ValueError("Only failed jobs can be retried")
        row.state = "pending"
        row.attempts = 0
        row.failure_code = None
        row.next_attempt_at = timestamp
        result = JobResponse.model_validate(row)
    return result
