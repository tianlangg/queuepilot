"""Ticket HTTP endpoints."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from queuepilot.auth.routes import current_user
from queuepilot.auth.service import Principal
from queuepilot.db.session import get_session
from queuepilot.tickets import service
from queuepilot.tickets.schemas import (
    CommentCreate,
    CommentPage,
    CommentResponse,
    Status,
    TicketCreate,
    TicketPage,
    TicketResponse,
    TransitionRequest,
)

router = APIRouter(prefix="/api/v1/tickets", tags=["tickets"])
User = Annotated[Principal, Depends(current_user)]
Session = Annotated[AsyncSession, Depends(get_session)]


def error(exc: Exception) -> HTTPException:
    if isinstance(exc, service.NotFound):
        return HTTPException(404, "Ticket not found")
    if isinstance(exc, service.Forbidden):
        return HTTPException(403, "Forbidden")
    if isinstance(exc, service.Conflict):
        return HTTPException(409, "Ticket version or transition conflict")
    return HTTPException(422, "Invalid cursor")


@router.post("", response_model=TicketResponse, status_code=201)
async def create(
    body: TicketCreate, principal: User, session: Session
) -> TicketResponse:
    return await service.create_ticket(session, principal, body)


@router.get("", response_model=TicketPage)
async def list_all(
    principal: User,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    status: Status | None = None,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> TicketPage:
    try:
        return await service.list_tickets(session, principal, limit, status, cursor)
    except ValueError as exc:
        raise error(exc) from exc


@router.get("/{ticket_id}", response_model=TicketResponse)
async def detail(
    ticket_id: uuid.UUID, principal: User, session: Session
) -> TicketResponse:
    try:
        return await service.get_ticket(session, principal, ticket_id)
    except service.NotFound as exc:
        raise error(exc) from exc


@router.get("/{ticket_id}/comments", response_model=CommentPage)
async def comments(
    ticket_id: uuid.UUID,
    principal: User,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> CommentPage:
    try:
        return await service.list_comments(session, principal, ticket_id, limit, cursor)
    except (service.NotFound, ValueError) as exc:
        raise error(exc) from exc


@router.post("/{ticket_id}/comments", response_model=CommentResponse, status_code=201)
async def comment(
    ticket_id: uuid.UUID, body: CommentCreate, principal: User, session: Session
) -> CommentResponse:
    try:
        return await service.add_comment(session, principal, ticket_id, body)
    except (service.NotFound, service.Forbidden, service.Conflict) as exc:
        raise error(exc) from exc


@router.post("/{ticket_id}/transitions", response_model=TicketResponse)
async def transition(
    ticket_id: uuid.UUID, body: TransitionRequest, principal: User, session: Session
) -> TicketResponse:
    try:
        return await service.transition(
            session, principal, ticket_id, body.status, body.expected_version
        )
    except (service.NotFound, service.Forbidden, service.Conflict) as exc:
        raise error(exc) from exc
