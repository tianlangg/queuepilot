"""Administrator-only queue and connection health counters."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from queuepilot.auth.routes import current_user
from queuepilot.auth.service import Principal
from queuepilot.db.session import get_session
from queuepilot.jobs.models import ScheduledJob
from queuepilot.tickets.models import OutboxEvent

router = APIRouter(prefix="/api/v1/operations", tags=["operations"])


class QueueMetrics(BaseModel):
    pending_outbox: int = Field(ge=0)
    oldest_outbox_seconds: float = Field(ge=0)
    pending_jobs: int = Field(ge=0)
    due_jobs: int = Field(ge=0)
    oldest_due_job_seconds: float = Field(ge=0)
    failed_jobs: int = Field(ge=0)
    websocket_connections: int = Field(ge=0)


def age(now: datetime, then: datetime | None) -> float:
    return max(0.0, (now - then).total_seconds()) if then is not None else 0.0


@router.get("/metrics", response_model=QueueMetrics)
async def metrics(
    request: Request,
    principal: Annotated[Principal, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> QueueMetrics:
    if principal.role != "admin":
        raise HTTPException(403, "Forbidden")
    now = datetime.now(UTC)
    outbox = await session.execute(
        select(func.count(), func.min(OutboxEvent.created_at)).where(
            OutboxEvent.published_at.is_(None)
        )
    )
    outbox_count, oldest_outbox = outbox.one()
    jobs = await session.execute(
        select(func.count(), func.min(ScheduledJob.due_at)).where(
            ScheduledJob.state == "pending", ScheduledJob.due_at <= now
        )
    )
    due_count, oldest_due = jobs.one()
    pending_count = await session.scalar(
        select(func.count())
        .select_from(ScheduledJob)
        .where(ScheduledJob.state == "pending")
    )
    failed_count = await session.scalar(
        select(func.count())
        .select_from(ScheduledJob)
        .where(ScheduledJob.state == "failed")
    )
    return QueueMetrics(
        pending_outbox=outbox_count,
        oldest_outbox_seconds=age(now, oldest_outbox),
        pending_jobs=pending_count or 0,
        due_jobs=due_count,
        oldest_due_job_seconds=age(now, oldest_due),
        failed_jobs=failed_count or 0,
        websocket_connections=len(request.app.state.realtime_hub.connections),
    )
