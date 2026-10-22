"""Two API processes and a worker communicate only through PostgreSQL and Redis."""

import asyncio
import json
import os
import socket
import statistics
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest
from sqlalchemy import update
from websockets.asyncio.client import connect
from websockets.typing import Origin

from queuepilot.auth.bootstrap import bootstrap
from queuepilot.core.config import Settings
from queuepilot.db.session import create_engine
from queuepilot.jobs.models import ScheduledJob
from queuepilot.tickets.models import Ticket

pytestmark = pytest.mark.integration


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _start(args: list[str], env: dict[str, str], log: Path) -> subprocess.Popen[str]:
    stream = log.open("w")
    try:
        return subprocess.Popen(
            args, env=env, stdout=stream, stderr=subprocess.STDOUT, text=True
        )
    finally:
        stream.close()


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


async def _ready(
    client: httpx.AsyncClient, port: int, process: subprocess.Popen[str]
) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        assert process.poll() is None, "API process stopped during startup"
        try:
            response = await client.get(f"http://127.0.0.1:{port}/health/ready")
            if response.status_code == 200 and response.json()["status"] == "ready":
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.05)
    pytest.fail("API did not become ready")


async def _message(ws: object) -> dict[str, object]:
    # The concrete ClientConnection has recv(); this keeps the call site compact.
    from websockets.asyncio.client import ClientConnection

    assert isinstance(ws, ClientConnection)
    return cast(
        dict[str, object], json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    )


def test_two_processes_worker_recovery_and_delivery_latency(
    migrated_database: Settings, tmp_path: Path
) -> None:
    assert asyncio.run(
        bootstrap("admin@example.com", "correct-horse-battery", migrated_database)
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("QUEUEPILOT_")
    }
    env.update(
        QUEUEPILOT_ENVIRONMENT="test",
        QUEUEPILOT_DATABASE_URL=migrated_database.database_url.get_secret_value(),
        QUEUEPILOT_REDIS_URL=migrated_database.redis_url.get_secret_value(),
        QUEUEPILOT_TOKEN_SECRET=migrated_database.token_secret.get_secret_value(),
    )
    first_port, second_port = _port(), _port()
    first = _start(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "queuepilot.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(first_port),
        ],
        env,
        tmp_path / "api1.log",
    )
    second = _start(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "queuepilot.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(second_port),
        ],
        env,
        tmp_path / "api2.log",
    )
    worker: subprocess.Popen[str] | None = None

    async def exercise() -> None:
        nonlocal first, worker
        async with httpx.AsyncClient(timeout=3) as http:
            await _ready(http, first_port, first)
            await _ready(http, second_port, second)
            first_url = f"http://127.0.0.1:{first_port}"
            second_url = f"http://127.0.0.1:{second_port}"
            admin_login = await http.post(
                first_url + "/api/v1/auth/login",
                json={
                    "email": "admin@example.com",
                    "password": "correct-horse-battery",
                },
            )
            assert admin_login.status_code == 200
            admin = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
            for email, role in (
                ("owner@example.com", "requester"),
                ("agent@example.com", "agent"),
            ):
                response = await http.post(
                    first_url + "/api/v1/users",
                    headers=admin,
                    json={
                        "email": email,
                        "password": "correct-horse-battery",
                        "role": role,
                    },
                )
                assert response.status_code == 201, response.text
            headers = []
            for base, email in (
                (first_url, "owner@example.com"),
                (second_url, "agent@example.com"),
            ):
                response = await http.post(
                    base + "/api/v1/auth/login",
                    json={"email": email, "password": "correct-horse-battery"},
                )
                assert response.status_code == 200
                headers.append(
                    {"Authorization": f"Bearer {response.json()['access_token']}"}
                )
            owner, agent = headers
            created = await http.post(
                first_url + "/api/v1/tickets",
                headers=owner,
                json={"title": "Processes", "description": "Real sockets"},
            )
            assert created.status_code == 201
            ticket_id = created.json()["id"]
            tokens = []
            for base, auth in ((first_url, owner), (second_url, agent)):
                response = await http.post(
                    base + "/api/v1/realtime/credentials", headers=auth
                )
                assert response.status_code == 200
                tokens.append(response.json()["credential"])
            async with (
                connect(
                    f"ws://127.0.0.1:{first_port}/api/v1/realtime/ws",
                    origin=Origin("http://127.0.0.1:8000"),
                    proxy=None,
                ) as owner_ws,
                connect(
                    f"ws://127.0.0.1:{second_port}/api/v1/realtime/ws",
                    origin=Origin("http://127.0.0.1:8000"),
                    proxy=None,
                ) as agent_ws,
            ):
                for ws, token in ((owner_ws, tokens[0]), (agent_ws, tokens[1])):
                    await ws.send(
                        json.dumps({"type": "authenticate", "credential": token})
                    )
                    assert (await _message(ws))["type"] == "ready"
                    await ws.send(
                        json.dumps({"type": "subscribe", "ticket_id": ticket_id})
                    )
                    assert (await _message(ws))["type"] == "subscribed"
                worker = _start(
                    [sys.executable, "-m", "queuepilot.outbox.dispatcher"],
                    env,
                    tmp_path / "worker.log",
                )
                assert (await _message(owner_ws))["ticket_version"] == 1
                assert (await _message(agent_ws))["ticket_version"] == 1
                latencies = []
                for version in range(2, 22):
                    started = time.monotonic()
                    response = await http.post(
                        first_url + f"/api/v1/tickets/{ticket_id}/comments",
                        headers=owner,
                        json={
                            "body": f"Update {version}",
                            "expected_version": version - 1,
                        },
                    )
                    assert response.status_code == 201, response.text
                    for ws in (owner_ws, agent_ws):
                        message = await _message(ws)
                        assert message["ticket_version"] == version, message
                    latencies.append(time.monotonic() - started)
                p95 = statistics.quantiles(latencies, n=20)[18]
                print(f"local realtime smoke: n={len(latencies)}, p95={p95:.3f}s")
                assert p95 < 1.0

                assert worker is not None
                _stop(worker)
                stalled = await http.post(
                    first_url + f"/api/v1/tickets/{ticket_id}/comments",
                    headers=owner,
                    json={"body": "After worker stop", "expected_version": 21},
                )
                assert stalled.status_code == 201
                assert (
                    await http.get(
                        second_url + f"/api/v1/tickets/{ticket_id}", headers=agent
                    )
                ).json()["version"] == 22
                worker = _start(
                    [sys.executable, "-m", "queuepilot.outbox.dispatcher"],
                    env,
                    tmp_path / "worker-restart.log",
                )
                assert (await _message(owner_ws))["ticket_version"] == 22
                assert (await _message(agent_ws))["ticket_version"] == 22

                claimed = await http.post(
                    second_url + f"/api/v1/tickets/{ticket_id}/claim",
                    headers=agent,
                    json={"expected_version": 22},
                )
                assert claimed.status_code == 200, claimed.text
                assert claimed.json()["assignee_id"] is not None
                assert (await _message(owner_ws))["ticket_version"] == 23
                assert (await _message(agent_ws))["ticket_version"] == 23
                policy = await http.patch(
                    first_url + "/api/v1/scheduling/policy",
                    headers=admin,
                    json={
                        "escalation_minutes": 60,
                        "auto_close_minutes": 1,
                        "expected_version": 1,
                    },
                )
                assert policy.status_code == 200, policy.text
                resolved = await http.post(
                    second_url + f"/api/v1/tickets/{ticket_id}/transitions",
                    headers=agent,
                    json={"status": "resolved", "expected_version": 23},
                )
                assert resolved.status_code == 200, resolved.text
                assert (await _message(owner_ws))["ticket_version"] == 24
                assert (await _message(agent_ws))["ticket_version"] == 24
                # Move the deadline back in this isolated test database, then
                # exercise the real worker process and REST/WebSocket flow.
                past = datetime.now(UTC) - timedelta(minutes=2)
                engine = create_engine(migrated_database)
                try:
                    async with engine.begin() as connection:
                        await connection.execute(
                            update(Ticket)
                            .where(Ticket.id == uuid.UUID(ticket_id))
                            .values(resolved_at=past)
                        )
                        await connection.execute(
                            update(ScheduledJob)
                            .where(
                                ScheduledJob.ticket_id == uuid.UUID(ticket_id),
                                ScheduledJob.kind == "auto_close",
                                ScheduledJob.state == "pending",
                            )
                            .values(
                                source_at=past,
                                due_at=past + timedelta(minutes=1),
                                next_attempt_at=past + timedelta(minutes=1),
                            )
                        )
                finally:
                    await engine.dispose()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    detail = await http.get(
                        first_url + f"/api/v1/tickets/{ticket_id}", headers=owner
                    )
                    if detail.json()["status"] == "closed":
                        assert detail.json()["version"] == 25
                        break
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("Auto-close did not complete across worker and API")
            _stop(first)
            first = _start(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "queuepilot.main:create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(first_port),
                ],
                env,
                tmp_path / "api1-restart.log",
            )
            await _ready(http, first_port, first)
            restored = await http.get(
                first_url + f"/api/v1/tickets/{ticket_id}", headers=owner
            )
            assert restored.status_code == 200
            assert restored.json()["status"] == "closed"

    try:
        asyncio.run(exercise())
    finally:
        if worker is not None:
            _stop(worker)
        _stop(first)
        _stop(second)
