"""Ticket authorization and atomic mutation services."""

import base64
import binascii
import uuid
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import and_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from queuepilot.auth.service import Principal
from queuepilot.tickets.models import Audit, Comment, OutboxEvent, Ticket
from queuepilot.tickets.schemas import (
    CommentCreate,
    CommentPage,
    CommentResponse,
    Status,
    TicketCreate,
    TicketPage,
    TicketResponse,
)


class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass


class Conflict(Exception):
    pass


def visible(principal: Principal) -> ColumnElement[bool]:
    if principal.role == "requester":
        return Ticket.requester_id == principal.id
    return true()


def _cursor(value: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        timestamp, ticket_id = raw.split("|", 1)
        result = datetime.fromisoformat(timestamp), uuid.UUID(ticket_id)
        if result[0].tzinfo is None:
            raise ValueError
        return result
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ValueError("Invalid cursor") from exc


def _encode(created_at: datetime, row_id: uuid.UUID) -> str:
    value = f"{created_at.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


async def list_tickets(
    session: AsyncSession,
    principal: Principal,
    limit: int,
    status: Status | None,
    cursor: str | None,
) -> TicketPage:
    query = select(Ticket).where(visible(principal))
    if status is not None:
        query = query.where(Ticket.status == status)
    if cursor is not None:
        created, ticket_id = _cursor(cursor)
        query = query.where(
            or_(
                Ticket.created_at < created,
                and_(Ticket.created_at == created, Ticket.id < ticket_id),
            )
        )
    rows = list(
        (
            await session.scalars(
                query.order_by(Ticket.created_at.desc(), Ticket.id.desc()).limit(
                    limit + 1
                )
            )
        ).all()
    )
    return TicketPage(
        items=[TicketResponse.model_validate(row) for row in rows[:limit]],
        next_cursor=_encode(rows[limit - 1].created_at, rows[limit - 1].id)
        if len(rows) > limit
        else None,
    )


async def get_ticket(
    session: AsyncSession, principal: Principal, ticket_id: uuid.UUID
) -> TicketResponse:
    ticket = await session.scalar(
        select(Ticket).where(Ticket.id == ticket_id, visible(principal))
    )
    if ticket is None:
        raise NotFound
    return TicketResponse.model_validate(ticket)


def record(
    session: AsyncSession,
    ticket: Ticket,
    actor: Principal,
    action: str,
    details: dict[str, str],
    now: datetime,
) -> None:
    session.add(
        Audit(
            ticket_id=ticket.id,
            actor_id=actor.id,
            ticket_version=ticket.version,
            action=action,
            details=details,
            created_at=now,
        )
    )
    # Payload is an invalidation hint; readers refetch through authorized REST.
    session.add(
        OutboxEvent(
            ticket_id=ticket.id,
            ticket_version=ticket.version,
            event_type=action,
            payload={},
            created_at=now,
        )
    )


async def create_ticket(
    session: AsyncSession,
    principal: Principal,
    body: TicketCreate,
    now: datetime | None = None,
) -> TicketResponse:
    timestamp = now or datetime.now(UTC)
    async with session.begin():
        ticket = Ticket(
            requester_id=principal.id,
            title=body.title,
            description=body.description,
            status="open",
            version=1,
            updated_at=timestamp,
        )
        session.add(ticket)
        await session.flush()
        await session.refresh(ticket)
        record(session, ticket, principal, "created", {}, timestamp)
        result = TicketResponse.model_validate(ticket)
    return result


async def list_comments(
    session: AsyncSession,
    principal: Principal,
    ticket_id: uuid.UUID,
    limit: int,
    cursor: str | None,
) -> CommentPage:
    await get_ticket(session, principal, ticket_id)
    query = select(Comment).where(Comment.ticket_id == ticket_id)
    if principal.role == "requester":
        query = query.where(Comment.internal.is_(False))
    if cursor is not None:
        created, comment_id = _cursor(cursor)
        query = query.where(
            or_(
                Comment.created_at > created,
                and_(Comment.created_at == created, Comment.id > comment_id),
            )
        )
    rows = list(
        (
            await session.scalars(
                query.order_by(Comment.created_at, Comment.id).limit(limit + 1)
            )
        ).all()
    )
    return CommentPage(
        items=[CommentResponse.model_validate(row) for row in rows[:limit]],
        next_cursor=_encode(rows[limit - 1].created_at, rows[limit - 1].id)
        if len(rows) > limit
        else None,
    )


async def add_comment(
    session: AsyncSession,
    principal: Principal,
    ticket_id: uuid.UUID,
    body: CommentCreate,
    now: datetime | None = None,
) -> CommentResponse:
    timestamp = now or datetime.now(UTC)
    async with session.begin():
        ticket = await session.scalar(
            select(Ticket)
            .where(Ticket.id == ticket_id, visible(principal))
            .with_for_update()
        )
        if ticket is None:
            raise NotFound
        if body.internal and principal.role == "requester":
            raise Forbidden
        if ticket.version != body.expected_version:
            raise Conflict
        comment = Comment(
            ticket_id=ticket.id,
            author_id=principal.id,
            body=body.body,
            internal=body.internal,
            created_at=timestamp,
        )
        session.add(comment)
        ticket.version += 1
        ticket.updated_at = timestamp
        await session.flush()
        record(
            session,
            ticket,
            principal,
            "commented",
            {"visibility": "internal" if body.internal else "public"},
            timestamp,
        )
        result = CommentResponse.model_validate(comment)
    return result


TRANSITIONS: dict[Status, set[Status]] = {
    "open": {"in_progress"},
    "in_progress": {"resolved"},
    "resolved": {"in_progress", "closed"},
    "closed": {"open"},
}


async def transition(
    session: AsyncSession,
    principal: Principal,
    ticket_id: uuid.UUID,
    status: Status,
    expected_version: int,
    now: datetime | None = None,
) -> TicketResponse:
    timestamp = now or datetime.now(UTC)
    async with session.begin():
        ticket = await session.scalar(
            select(Ticket)
            .where(Ticket.id == ticket_id, visible(principal))
            .with_for_update()
        )
        if ticket is None:
            raise NotFound
        if (
            ticket.version != expected_version
            or status not in TRANSITIONS[cast(Status, ticket.status)]
        ):
            raise Conflict
        if principal.role == "requester":
            if status not in {"in_progress", "open"} or ticket.status not in {
                "resolved",
                "closed",
            }:
                raise Forbidden
        elif principal.role == "agent" and ticket.assignee_id != principal.id:
            raise Forbidden
        previous = ticket.status
        ticket.status = status
        ticket.version += 1
        ticket.updated_at = timestamp
        ticket.resolved_at = (
            timestamp
            if status == "resolved"
            else None
            if status in {"open", "in_progress"}
            else ticket.resolved_at
        )
        ticket.closed_at = timestamp if status == "closed" else None
        record(
            session,
            ticket,
            principal,
            "transitioned",
            {"from": previous, "to": status},
            timestamp,
        )
        await session.flush()
        result = TicketResponse.model_validate(ticket)
    return result
