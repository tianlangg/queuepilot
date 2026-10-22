"""Durable publication and authorized fan-out through real PostgreSQL and Redis."""

import asyncio
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from queuepilot.auth.bootstrap import bootstrap
from queuepilot.auth.models import User
from queuepilot.core.config import Settings
from queuepilot.core.resources import create_redis
from queuepilot.db.session import create_engine
from queuepilot.main import create_app
from queuepilot.outbox.dispatcher import (
    CHANNEL,
    Event,
    claim_batch,
    dispatch_once,
    finish,
)
from queuepilot.realtime.models import ConnectionCredential
from queuepilot.tickets.models import OutboxEvent

from .conftest import ROOT, migrate

pytestmark = pytest.mark.integration
ORIGIN = {"origin": "http://127.0.0.1:8000"}


def _headers(client: TestClient, email: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _credential(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/api/v1/realtime/credentials", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return str(response.json()["credential"])


def _receive(ws: WebSocketTestSession) -> dict[str, object]:
    result: queue.Queue[dict[str, object] | WebSocketDisconnect] = queue.Queue(
        maxsize=1
    )

    def read() -> None:
        try:
            result.put(ws.receive_json())
        except WebSocketDisconnect as exc:
            result.put(exc)

    threading.Thread(target=read, daemon=True).start()
    value = result.get(timeout=5)
    if isinstance(value, WebSocketDisconnect):
        raise value
    return value


def _wait_ready(client: TestClient) -> None:
    deadline = time.monotonic() + 5
    while not cast(FastAPI, client.app).state.realtime_hub.ready.is_set():
        if time.monotonic() >= deadline:
            pytest.fail("Redis listener did not subscribe")
        time.sleep(0.01)


async def _publish(settings: Settings) -> int:
    engine = create_engine(settings)
    redis = create_redis(settings)
    try:
        return await dispatch_once(
            async_sessionmaker(engine, expire_on_commit=False), redis
        )
    finally:
        await redis.aclose()
        await engine.dispose()


async def _repeat_event(settings: Settings, ticket_id: uuid.UUID, version: int) -> None:
    engine = create_engine(settings)
    redis = create_redis(settings)
    try:
        async with async_sessionmaker(engine)() as session:
            row = await session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.ticket_id == ticket_id,
                    OutboxEvent.ticket_version == version,
                )
            )
            assert row is not None
            event = Event.model_validate(
                {
                    "event_id": row.id,
                    "ticket_id": row.ticket_id,
                    "ticket_version": row.ticket_version,
                    "event_type": row.event_type,
                    "timestamp": row.created_at,
                    "visibility": row.payload.get("visibility"),
                }
            )
        await redis.publish(CHANNEL, event.model_dump_json())
    finally:
        await redis.aclose()
        await engine.dispose()


async def _demote_agent(settings: Settings) -> None:
    engine = create_engine(settings)
    try:
        async with async_sessionmaker(engine)() as session, session.begin():
            await session.execute(
                update(User)
                .where(User.email == "agent@example.com")
                .values(role="requester")
            )
    finally:
        await engine.dispose()


def test_cross_replica_visibility_replay_and_revocation(
    migrated_database: Settings,
) -> None:
    assert asyncio.run(
        bootstrap("admin@example.com", "correct-horse-battery", migrated_database)
    )
    with (
        TestClient(create_app(migrated_database)) as first,
        TestClient(create_app(migrated_database)) as second,
    ):
        _wait_ready(first)
        _wait_ready(second)
        admin = _headers(first, "admin@example.com")
        for email, role in (
            ("owner@example.com", "requester"),
            ("other@example.com", "requester"),
            ("agent@example.com", "agent"),
        ):
            response = first.post(
                "/api/v1/users",
                headers=admin,
                json={
                    "email": email,
                    "password": "correct-horse-battery",
                    "role": role,
                },
            )
            assert response.status_code == 201
        owner = _headers(second, "owner@example.com")
        other = _headers(second, "other@example.com")
        agent = _headers(first, "agent@example.com")
        created = second.post(
            "/api/v1/tickets",
            headers=owner,
            json={"title": "Live", "description": "Private text"},
        )
        assert created.status_code == 201
        ticket_id = created.json()["id"]
        path = f"/api/v1/tickets/{ticket_id}"
        owner_token = _credential(second, owner)
        agent_token = _credential(first, agent)
        with (
            second.websocket_connect("/api/v1/realtime/ws", headers=ORIGIN) as owner_ws,
            first.websocket_connect("/api/v1/realtime/ws", headers=ORIGIN) as agent_ws,
        ):
            owner_ws.send_json({"type": "authenticate", "credential": owner_token})
            agent_ws.send_json({"type": "authenticate", "credential": agent_token})
            assert _receive(owner_ws)["type"] == "ready"
            assert _receive(agent_ws)["type"] == "ready"
            with pytest.raises(WebSocketDisconnect) as replay_while_active:
                with second.websocket_connect(
                    "/api/v1/realtime/ws", headers=ORIGIN
                ) as duplicate_ws:
                    duplicate_ws.send_json(
                        {"type": "authenticate", "credential": owner_token}
                    )
                    duplicate_ws.receive_json()
            assert replay_while_active.value.code == 1008
            for ws in (owner_ws, agent_ws):
                ws.send_json({"type": "subscribe", "ticket_id": ticket_id})
                assert _receive(ws)["type"] == "subscribed"
            assert asyncio.run(_publish(migrated_database)) == 1
            assert _receive(owner_ws)["ticket_version"] == 1
            assert _receive(agent_ws)["ticket_version"] == 1

            internal = first.post(
                path + "/comments",
                headers=agent,
                json={"body": "Staff secret", "internal": True, "expected_version": 1},
            )
            assert internal.status_code == 201
            assert asyncio.run(_publish(migrated_database)) == 1
            assert _receive(agent_ws)["ticket_version"] == 2
            public = second.post(
                path + "/comments",
                headers=owner,
                json={"body": "Update?", "expected_version": 2},
            )
            assert public.status_code == 201
            assert asyncio.run(_publish(migrated_database)) == 1
            assert _receive(owner_ws)["ticket_version"] == 3
            assert _receive(agent_ws)["ticket_version"] == 3
            assert (
                len(second.get(path + "/comments", headers=owner).json()["items"]) == 1
            )

            asyncio.run(_repeat_event(migrated_database, uuid.UUID(ticket_id), 3))
            claimed = first.post(
                path + "/claim", headers=agent, json={"expected_version": 3}
            )
            assert claimed.status_code == 200
            assert asyncio.run(_publish(migrated_database)) == 1
            assert _receive(owner_ws)["ticket_version"] == 4
            assert _receive(agent_ws)["ticket_version"] == 4

            other_token = _credential(second, other)
            with second.websocket_connect(
                "/api/v1/realtime/ws", headers=ORIGIN
            ) as other_ws:
                other_ws.send_json({"type": "authenticate", "credential": other_token})
                assert _receive(other_ws)["type"] == "ready"
                other_ws.send_json({"type": "subscribe", "ticket_id": ticket_id})
                assert _receive(other_ws) == {
                    "type": "error",
                    "detail": "Ticket not found",
                }

            asyncio.run(_demote_agent(migrated_database))
            agent_ws.send_json({"type": "subscribe", "ticket_id": ticket_id})
            assert _receive(agent_ws) == {"type": "error", "detail": "Ticket not found"}

            logout = second.post("/api/v1/auth/logout", headers=owner)
            assert logout.status_code == 200
            with pytest.raises(WebSocketDisconnect):
                _receive(owner_ws)

        with pytest.raises(WebSocketDisconnect) as replay:
            with second.websocket_connect("/api/v1/realtime/ws", headers=ORIGIN) as ws:
                ws.send_json({"type": "authenticate", "credential": owner_token})
                ws.receive_json()
        assert replay.value.code == 1008
        with pytest.raises(WebSocketDisconnect):
            with first.websocket_connect(
                "/api/v1/realtime/ws", headers={"origin": "http://evil.example"}
            ):
                pass


def test_outbox_retry_and_lease_recovery(migrated_database: Settings) -> None:
    assert asyncio.run(
        bootstrap("admin@example.com", "correct-horse-battery", migrated_database)
    )
    with TestClient(create_app(migrated_database)) as client:
        admin = _headers(client, "admin@example.com")
        created = client.post(
            "/api/v1/tickets",
            headers=admin,
            json={"title": "Retry", "description": "Durable"},
        )
        assert created.status_code == 201
        ticket_id = uuid.UUID(created.json()["id"])
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        outage = migrated_database.model_copy(
            update={
                "redis_url": SecretStr(
                    f"redis://127.0.0.1:{reserved.getsockname()[1]}/0"
                )
            }
        )
        assert asyncio.run(_publish(outage)) == 1

    async def inspect() -> None:
        engine = create_engine(migrated_database)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as session:
                row = await session.scalar(
                    select(OutboxEvent).where(OutboxEvent.ticket_id == ticket_id)
                )
                assert (
                    row is not None and row.published_at is None and row.attempts == 1
                )
                assert row.available_at > datetime.now(UTC)
            async with sessions() as session, session.begin():
                await session.execute(
                    update(OutboxEvent)
                    .where(OutboxEvent.ticket_id == ticket_id)
                    .values(available_at=datetime.now(UTC))
                )
            first = (await claim_batch(sessions))[0]
            replacement = (
                await claim_batch(
                    sessions, now=datetime.now(UTC) + timedelta(seconds=31)
                )
            )[0]
            assert first.token != replacement.token
            await finish(sessions, first, True)
            async with sessions() as session:
                row = await session.scalar(
                    select(OutboxEvent).where(OutboxEvent.ticket_id == ticket_id)
                )
                assert row is not None and row.published_at is None
            await finish(
                sessions,
                replacement,
                False,
                now=datetime.now(UTC) - timedelta(seconds=60),
            )
        finally:
            await engine.dispose()

    asyncio.run(inspect())
    assert asyncio.run(_publish(migrated_database)) == 1


def test_upgrade_from_assignment_schema(empty_database: Settings) -> None:
    migrate(empty_database, "0004_assignment")
    migrate(empty_database)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("QUEUEPILOT_")
    }
    env.update(
        QUEUEPILOT_ENVIRONMENT="test",
        QUEUEPILOT_DATABASE_URL=empty_database.database_url.get_secret_value(),
        QUEUEPILOT_REDIS_URL=empty_database.redis_url.get_secret_value(),
        QUEUEPILOT_TOKEN_SECRET=empty_database.token_secret.get_secret_value(),
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), "check"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_expired_credential_and_slow_client_disconnect(
    migrated_database: Settings,
) -> None:
    assert asyncio.run(
        bootstrap("admin@example.com", "correct-horse-battery", migrated_database)
    )
    with TestClient(create_app(migrated_database)) as client:
        _wait_ready(client)
        admin = _headers(client, "admin@example.com")
        created = client.post(
            "/api/v1/tickets",
            headers=admin,
            json={"title": "Slow", "description": "Queue"},
        )
        ticket_id = uuid.UUID(created.json()["id"])
        expired = _credential(client, admin)

        async def expire() -> None:
            engine = create_engine(migrated_database)
            try:
                async with async_sessionmaker(engine)() as session, session.begin():
                    await session.execute(
                        update(ConnectionCredential)
                        .where(
                            ConnectionCredential.id
                            == uuid.UUID(expired.split(".", 1)[0])
                        )
                        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                    )
            finally:
                await engine.dispose()

        asyncio.run(expire())
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect("/api/v1/realtime/ws", headers=ORIGIN) as ws:
                ws.send_json({"type": "authenticate", "credential": expired})
                ws.receive_json()
        assert denied.value.code == 1008

        token = _credential(client, admin)
        with client.websocket_connect("/api/v1/realtime/ws", headers=ORIGIN) as ws:
            ws.send_json({"type": "authenticate", "credential": token})
            assert _receive(ws)["type"] == "ready"
            ws.send_json({"type": "subscribe", "ticket_id": str(ticket_id)})
            assert _receive(ws)["type"] == "subscribed"

            def flood() -> None:
                hub = cast(FastAPI, client.app).state.realtime_hub
                for version in range(1, 34):
                    hub.broadcast(
                        Event(
                            event_id=uuid.uuid4(),
                            ticket_id=ticket_id,
                            ticket_version=version,
                            event_type="commented",
                            timestamp=datetime.now(UTC),
                        )
                    )

            assert client.portal is not None
            client.portal.call(flood)
            message = _receive(ws)
            if message["type"] == "ticket_changed":
                message = _receive(ws)
            assert message["type"] == "resync_required"
            with pytest.raises(WebSocketDisconnect) as closed:
                _receive(ws)
            assert closed.value.code == 1013
