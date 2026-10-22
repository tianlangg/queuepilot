"""Credential issuance and authorized ticket WebSocket subscriptions."""

import asyncio
import time
import uuid
from datetime import datetime
from typing import Annotated, Literal, cast

from fastapi import (
    APIRouter,
    Depends,
    Header,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from queuepilot.auth.routes import current_user
from queuepilot.auth.security import decode_access
from queuepilot.auth.service import Principal
from queuepilot.core.resources import Resources
from queuepilot.realtime import credentials
from queuepilot.realtime.hub import Connection, Hub
from queuepilot.tickets.models import Ticket
from queuepilot.tickets.service import visible

router = APIRouter(tags=["realtime"])


class CredentialResponse(BaseModel):
    credential: str
    expires_at: datetime


class Authentication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["authenticate"]
    credential: str


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["subscribe", "unsubscribe", "pong"]
    ticket_id: uuid.UUID | None = None


@router.post("/api/v1/realtime/credentials", response_model=CredentialResponse)
async def issue_credential(
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(current_user)],
    authorization: Annotated[str, Header()],
) -> CredentialResponse:
    _, session_id = decode_access(
        authorization.split(" ", 1)[1], request.app.state.settings
    )
    token, expires = await credentials.issue(
        request.app.state.resources.sessions, principal, session_id
    )
    response.headers["Cache-Control"] = "no-store"
    return CredentialResponse(credential=token, expires_at=expires)


async def _authorized(
    resources: Resources, identity: credentials.Identity, ticket_id: uuid.UUID
) -> int | None:
    principal = Principal(identity.user_id, identity.role)
    async with resources.sessions() as session:
        return cast(
            int | None,
            await session.scalar(
                select(Ticket.version).where(Ticket.id == ticket_id, visible(principal))
            ),
        )


async def _read_message(websocket: WebSocket, seconds: float | None = None) -> str:
    if seconds is None:
        return await websocket.receive_text()
    return await asyncio.wait_for(websocket.receive_text(), seconds)


@router.websocket("/api/v1/realtime/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    settings = websocket.app.state.settings
    origin = websocket.headers.get("origin")
    if origin not in settings.websocket_origins.split(","):
        await websocket.close(code=1008)
        return
    hub: Hub = websocket.app.state.realtime_hub
    if not hub.try_open(settings.websocket_connection_limit):
        await websocket.close(code=1013)
        return
    try:
        await _serve_websocket(websocket)
    finally:
        hub.release()


async def _serve_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    resources: Resources = websocket.app.state.resources
    hub: Hub = websocket.app.state.realtime_hub
    try:
        raw = await _read_message(websocket, 5)
        if len(raw) > 512:
            raise ValueError
        message = Authentication.model_validate_json(raw)
        identity = await credentials.consume(resources.sessions, message.credential)
        if identity is None:
            raise ValueError
    except WebSocketDisconnect:
        return
    except (ValueError, TimeoutError):
        await websocket.close(code=1008)
        return
    assert identity is not None
    active_identity: credentials.Identity = identity

    connection = Connection()
    hub.connections.add(connection)
    send_lock = asyncio.Lock()
    last_pong = time.monotonic()

    async def send(payload: dict[str, object]) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def sender() -> None:
        nonlocal active_identity
        last_heartbeat = time.monotonic()
        while True:
            if not hub.ready.is_set():
                await send({"type": "resync_required"})
                await websocket.close(code=1013)
                return
            if connection.overflow:
                await send({"type": "resync_required"})
                await websocket.close(code=1013)
                return
            fresh = await credentials.revalidate(resources.sessions, active_identity)
            if fresh is None or time.monotonic() - last_pong > 30:
                await websocket.close(code=1008)
                return
            active_identity = fresh
            if time.monotonic() - last_heartbeat >= 10:
                await send({"type": "heartbeat"})
                last_heartbeat = time.monotonic()
            try:
                event = await asyncio.wait_for(connection.queue.get(), timeout=5)
            except TimeoutError:
                continue
            if event.ticket_id not in connection.tickets:
                continue
            fresh = await credentials.revalidate(resources.sessions, active_identity)
            if fresh is None:
                await websocket.close(code=1008)
                return
            active_identity = fresh
            current = await _authorized(resources, active_identity, event.ticket_id)
            if current is None:
                connection.tickets.discard(event.ticket_id)
                connection.versions.pop(event.ticket_id, None)
                continue
            if connection.seen(event.event_id):
                continue
            previous = connection.versions.get(event.ticket_id, 0)
            if event.ticket_version <= previous:
                continue
            connection.versions[event.ticket_id] = event.ticket_version
            if event.visibility == "internal" and active_identity.role == "requester":
                continue
            if event.ticket_version > previous + 1:
                await send(
                    {"type": "resync_required", "ticket_id": str(event.ticket_id)}
                )
                continue
            await send(
                {
                    "type": "ticket_changed",
                    **event.model_dump(mode="json", exclude={"visibility"}),
                }
            )

    await send({"type": "ready", "resync_required": True})
    sender_task = asyncio.create_task(sender())
    try:
        while not sender_task.done():
            try:
                raw = await _read_message(websocket, 5)
            except TimeoutError:
                continue
            if len(raw) > 1024:
                await websocket.close(code=1008)
                return
            try:
                command = Command.model_validate_json(raw)
            except ValueError:
                await send({"type": "error", "detail": "Invalid command"})
                continue
            if command.type == "pong":
                last_pong = time.monotonic()
                continue
            if command.ticket_id is None:
                await send({"type": "error", "detail": "Ticket ID required"})
                continue
            if command.type == "unsubscribe":
                connection.tickets.discard(command.ticket_id)
                connection.versions.pop(command.ticket_id, None)
                await send(
                    {"type": "unsubscribed", "ticket_id": str(command.ticket_id)}
                )
                continue
            fresh = await credentials.revalidate(resources.sessions, active_identity)
            if fresh is None:
                await websocket.close(code=1008)
                return
            active_identity = fresh
            version = await _authorized(resources, active_identity, command.ticket_id)
            if version is None:
                await send({"type": "error", "detail": "Ticket not found"})
                continue
            if (
                len(connection.tickets) >= 32
                and command.ticket_id not in connection.tickets
            ):
                await send({"type": "error", "detail": "Subscription limit reached"})
                continue
            connection.tickets.add(command.ticket_id)
            connection.versions[command.ticket_id] = 0
            await send(
                {
                    "type": "subscribed",
                    "ticket_id": str(command.ticket_id),
                    "version": version,
                    "resync_required": True,
                }
            )
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.connections.discard(connection)
        sender_task.cancel()
        try:
            await sender_task
        except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
            pass
