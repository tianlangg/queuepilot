"""Administrator scheduling controls."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from queuepilot.auth.routes import current_user
from queuepilot.auth.service import Principal
from queuepilot.db.session import get_session
from queuepilot.jobs import service
from queuepilot.jobs.service import JobResponse, PolicyResponse, PolicyUpdate

router = APIRouter(prefix="/api/v1/scheduling", tags=["scheduling"])
User = Annotated[Principal, Depends(current_user)]
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/policy", response_model=PolicyResponse)
async def get_policy(principal: User, session: Session) -> PolicyResponse:
    try:
        return await service.get_policy(session, principal)
    except PermissionError as exc:
        raise HTTPException(403, "Forbidden") from exc


@router.patch("/policy", response_model=PolicyResponse)
async def update_policy(
    body: PolicyUpdate, principal: User, session: Session
) -> PolicyResponse:
    try:
        return await service.change_policy(session, principal, body)
    except PermissionError as exc:
        raise HTTPException(403, "Forbidden") from exc
    except ValueError as exc:
        raise HTTPException(409, "Policy version conflict") from exc


@router.get("/jobs", response_model=list[JobResponse])
async def jobs(
    principal: User,
    session: Session,
    state: Literal["pending", "completed", "cancelled", "failed"] = "failed",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    after: uuid.UUID | None = None,
) -> list[JobResponse]:
    try:
        return await service.list_jobs(session, principal, state, limit, after)
    except PermissionError as exc:
        raise HTTPException(403, "Forbidden") from exc


@router.post("/jobs/{job_id}/retry", response_model=JobResponse)
async def retry(job_id: uuid.UUID, principal: User, session: Session) -> JobResponse:
    try:
        return await service.retry_job(session, principal, job_id)
    except PermissionError as exc:
        raise HTTPException(403, "Forbidden") from exc
    except LookupError as exc:
        raise HTTPException(404, "Job not found") from exc
    except ValueError as exc:
        raise HTTPException(409, "Only failed jobs can be retried") from exc
