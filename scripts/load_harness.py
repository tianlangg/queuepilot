"""Run an isolated full-stack reference load and print measured p95 latencies."""

import argparse
import asyncio
import json
import math
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
from dotenv import dotenv_values
from psycopg import sql
from pydantic import SecretStr
from sqlalchemy.engine import make_url
from websockets.asyncio.client import ClientConnection, connect
from websockets.typing import Origin

from queuepilot.auth.bootstrap import bootstrap
from queuepilot.core.config import Settings


def test_url(root: Path) -> str:
    values = dotenv_values(root / ".env.test")
    raw = os.environ.get("QUEUEPILOT_TEST_DATABASE_URL") or values.get(
        "QUEUEPILOT_TEST_DATABASE_URL"
    )
    if raw is None:
        raise ValueError("Missing isolated test database URL")
    url = make_url(raw)
    if (
        url.drivername != "postgresql+psycopg"
        or url.database != "queuepilot_test"
        or url.host not in {"127.0.0.1", "localhost"}
        or url.port != 55433
    ):
        raise ValueError(
            "Only the local queuepilot_test server on port 55433 is allowed"
        )
    return raw


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * fraction) - 1]


async def wait_ready(
    client: httpx.AsyncClient, base: str, process: subprocess.Popen[bytes]
) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("API stopped during benchmark startup")
        try:
            response = await client.get(base + "/health/ready")
            if response.status_code == 200 and response.json()["status"] == "ready":
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.1)
    raise RuntimeError("API did not become ready")


async def run_load(
    base: str,
    email: str,
    password: str,
    ticket_ids: list[str],
    clients: int,
    sockets: int,
    duration: int,
    think_ms: int,
    api_workers: int,
    api: subprocess.Popen[bytes],
) -> dict[str, Any]:
    timings: dict[str, list[float]] = defaultdict(list)
    failures: dict[str, int] = defaultdict(int)
    resync_signals = 0
    events = 0
    live_metrics: dict[str, Any] | None = None
    connections: list[ClientConnection] = []
    readers: list[asyncio.Task[None]] = []
    async with httpx.AsyncClient(
        timeout=10,
        limits=httpx.Limits(max_connections=max(100, clients + sockets + 10)),
    ) as http:
        await wait_ready(http, base, api)
        login = await http.post(
            base + "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        if login.status_code != 200:
            raise RuntimeError("Benchmark administrator login failed")
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        async def read_socket(connection: ClientConnection) -> None:
            nonlocal events, resync_signals
            async for raw in connection:
                message = json.loads(raw)
                if message.get("type") == "heartbeat":
                    await connection.send('{"type":"pong"}')
                elif message.get("type") == "ticket_changed":
                    events += 1
                elif message.get("type") == "resync_required":
                    resync_signals += 1

        try:
            for index in range(sockets):
                credential = await http.post(
                    base + "/api/v1/realtime/credentials", headers=headers
                )
                if credential.status_code != 200:
                    raise RuntimeError("WebSocket credential issuance failed")
                connection = await connect(
                    base.replace("http://", "ws://") + "/api/v1/realtime/ws",
                    origin=Origin("http://127.0.0.1:8000"),
                    proxy=None,
                )
                connections.append(connection)
                await connection.send(
                    json.dumps(
                        {
                            "type": "authenticate",
                            "credential": credential.json()["credential"],
                        }
                    )
                )
                ready = json.loads(await asyncio.wait_for(connection.recv(), timeout=5))
                if ready.get("type") != "ready":
                    raise RuntimeError("WebSocket authentication failed")
                await connection.send(
                    json.dumps(
                        {"type": "subscribe", "ticket_id": ticket_ids[index % clients]}
                    )
                )
                subscribed = json.loads(
                    await asyncio.wait_for(connection.recv(), timeout=5)
                )
                if subscribed.get("type") != "subscribed":
                    raise RuntimeError("WebSocket subscription failed")
                readers.append(asyncio.create_task(read_socket(connection)))

            end = time.monotonic() + duration

            async def client_task(index: int) -> None:
                version = 1
                ticket_id = ticket_ids[index]
                await asyncio.sleep((index / clients) * (think_ms / 1000))
                while time.monotonic() < end:
                    for name, method, path, body in (
                        ("list", "GET", "/api/v1/tickets?limit=20", None),
                        ("detail", "GET", f"/api/v1/tickets/{ticket_id}", None),
                        (
                            "mutation",
                            "POST",
                            f"/api/v1/tickets/{ticket_id}/comments",
                            {"body": "Load sample", "expected_version": version},
                        ),
                    ):
                        started = time.monotonic()
                        try:
                            response = await http.request(
                                method, base + path, headers=headers, json=body
                            )
                        except httpx.HTTPError:
                            failures["transport"] += 1
                            return
                        timings[name].append(time.monotonic() - started)
                        if response.status_code >= 500:
                            failures["5xx"] += 1
                        if response.status_code != (201 if name == "mutation" else 200):
                            failures[f"{name}_{response.status_code}"] += 1
                            return
                        if name == "mutation":
                            version += 1
                    await asyncio.sleep(think_ms / 1000)

            await asyncio.gather(*(client_task(index) for index in range(clients)))
            failures["websocket_closed"] += sum(reader.done() for reader in readers)
            if failures["websocket_closed"] == 0:
                del failures["websocket_closed"]
            metrics = await http.get(
                base + "/api/v1/operations/metrics", headers=headers
            )
            live_metrics = metrics.json() if metrics.status_code == 200 else None
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            for connection in connections:
                await connection.close()
        return {
            "measured_at": datetime.now(UTC).isoformat(),
            "seeded_tickets": len(ticket_ids),
            "clients": clients,
            "websockets": sockets,
            "api_workers": api_workers,
            "benchmark_access_token_minutes": 60,
            "duration_seconds": duration,
            "samples": {name: len(values) for name, values in timings.items()},
            "p95_ms": {
                name: round(percentile(values, 0.95) * 1000, 2)
                for name, values in timings.items()
            },
            "websocket_events": events,
            "websocket_resync_signals": resync_signals,
            "failures": dict(failures),
            "queue_metrics": live_metrics,
        }


def stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-isolated-test-server", action="store_true")
    parser.add_argument("--seed-count", type=int, default=10_000)
    parser.add_argument("--clients", type=int, default=50)
    parser.add_argument("--websockets", type=int, default=100)
    parser.add_argument("--duration", type=int, default=600)
    parser.add_argument("--think-ms", type=int, default=100)
    parser.add_argument("--api-workers", type=int, default=4)
    args = parser.parse_args()
    if not args.confirm_isolated_test_server:
        parser.error("Explicit isolated-test-server confirmation is required")
    if not (1 <= args.clients <= 200 and args.clients <= args.seed_count <= 100_000):
        parser.error("Require 1-200 clients and seed_count between clients and 100000")
    if not (0 <= args.websockets <= 500 and 1 <= args.duration <= 3600):
        parser.error("WebSockets must be 0-500 and duration 1-3600 seconds")
    if not 0 <= args.think_ms <= 10_000:
        parser.error("think-ms must be 0-10000")
    if not 1 <= args.api_workers <= 8:
        parser.error("api-workers must be 1-8")
    root = Path(__file__).resolve().parents[1]
    raw = test_url(root)
    url = make_url(raw)
    control = url.set(drivername="postgresql").render_as_string(hide_password=False)
    name = f"queuepilot_test_load_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(control, autocommit=True, connect_timeout=5) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        api: subprocess.Popen[bytes] | None = None
        worker: subprocess.Popen[bytes] | None = None
        try:
            database = url.set(database=name)
            password = secrets.token_urlsafe(32)
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("QUEUEPILOT_")
            }
            environment.update(
                QUEUEPILOT_ENVIRONMENT="test",
                QUEUEPILOT_DATABASE_URL=database.render_as_string(hide_password=False),
                QUEUEPILOT_REDIS_URL="redis://127.0.0.1:56380/0",
                QUEUEPILOT_TOKEN_SECRET=secrets.token_urlsafe(48),
                QUEUEPILOT_ACCESS_TOKEN_MINUTES="60",
                QUEUEPILOT_AUTH_REQUESTS_PER_MINUTE="10000",
                QUEUEPILOT_READ_REQUESTS_PER_MINUTE="10000",
                QUEUEPILOT_WRITE_REQUESTS_PER_MINUTE="10000",
            )
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root,
                env=environment,
                check=True,
                capture_output=True,
                timeout=30,
            )
            settings = Settings(
                environment="test",
                database_url=SecretStr(environment["QUEUEPILOT_DATABASE_URL"]),
                redis_url=SecretStr(environment["QUEUEPILOT_REDIS_URL"]),
                token_secret=SecretStr(environment["QUEUEPILOT_TOKEN_SECRET"]),
            )
            email = "load@example.invalid"
            if not asyncio.run(bootstrap(email, password, settings)):
                raise RuntimeError("Benchmark administrator bootstrap failed")
            with psycopg.connect(
                database.set(drivername="postgresql").render_as_string(
                    hide_password=False
                )
            ) as connection:
                user = connection.execute(
                    "SELECT id FROM users WHERE email = %s", (email,)
                ).fetchone()
                assert user is not None
                user_id = user[0]
                ids = [
                    str(row[0])
                    for row in connection.execute(
                        """INSERT INTO tickets
                        (id, requester_id, assignee_id, title, description,
                         status, version, created_at, updated_at, opened_at)
                        SELECT gen_random_uuid(), %s, %s,
                               'Load ticket', 'Reference load',
                               'in_progress', 1, now(), now(), now()
                        FROM generate_series(1, %s) RETURNING id""",
                        (user_id, user_id, args.seed_count),
                    )
                ]
                connection.execute(
                    """INSERT INTO ticket_audit
                    (id, ticket_id, actor_id, actor_kind, ticket_version,
                     action, details, created_at)
                    SELECT gen_random_uuid(), id, %s, 'user', 1,
                           'created', '{}'::jsonb, created_at FROM tickets""",
                    (user_id,),
                )
            port = free_port()
            base = f"http://127.0.0.1:{port}"
            with tempfile.TemporaryDirectory(prefix="queuepilot-load-") as logs:
                with (Path(logs) / "api.log").open("wb") as api_log:
                    api = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "uvicorn",
                            "queuepilot.main:create_app",
                            "--factory",
                            "--host",
                            "127.0.0.1",
                            "--port",
                            str(port),
                            "--workers",
                            str(args.api_workers),
                        ],
                        cwd=root,
                        env=environment,
                        stdout=api_log,
                        stderr=subprocess.STDOUT,
                    )
                with (Path(logs) / "worker.log").open("wb") as worker_log:
                    worker = subprocess.Popen(
                        [sys.executable, "-m", "queuepilot.outbox.dispatcher"],
                        cwd=root,
                        env=environment,
                        stdout=worker_log,
                        stderr=subprocess.STDOUT,
                    )
                try:
                    result = asyncio.run(
                        run_load(
                            base,
                            email,
                            password,
                            ids,
                            args.clients,
                            args.websockets,
                            args.duration,
                            args.think_ms,
                            args.api_workers,
                            api,
                        )
                    )
                    print(json.dumps(result, indent=2, sort_keys=True))
                    p95 = result["p95_ms"]
                    if (
                        result["failures"]
                        or p95.get("list", 0) >= 300
                        or p95.get("detail", 0) >= 300
                        or p95.get("mutation", 0) >= 500
                    ):
                        print(
                            "Reference load target missed; see measured result",
                            file=sys.stderr,
                        )
                        raise SystemExit(1)
                except Exception:
                    for filename in ("api.log", "worker.log"):
                        print(
                            f"{filename}: {Path(logs, filename).read_text()[-4000:]}",
                            file=sys.stderr,
                        )
                    raise
                finally:
                    stop(worker)
                    stop(api)
        finally:
            if worker is not None:
                stop(worker)
            if api is not None:
                stop(api)
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


if __name__ == "__main__":
    main()
