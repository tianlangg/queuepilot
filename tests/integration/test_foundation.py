import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from queuepilot.core.config import Settings
from queuepilot.db.base import SCHEMA_REVISION
from queuepilot.db.session import create_engine
from queuepilot.main import create_app

from .conftest import migrate

pytestmark = pytest.mark.integration


def test_migrate_empty_database_and_repeat_upgrade(empty_database: Settings) -> None:
    migrate(empty_database)
    migrate(empty_database)
    url = make_url(empty_database.database_url.get_secret_value()).set(
        drivername="postgresql"
    )
    with psycopg.connect(url.render_as_string(hide_password=False)) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert row == (SCHEMA_REVISION,)
        tables = connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
        assert tables == [("alembic_version",)]


def test_unmigrated_database_is_not_ready(empty_database: Settings) -> None:
    with TestClient(create_app(empty_database)) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["database"] == "unavailable"
        assert client.get("/health/live").status_code == 200


def test_healthy_services(migrated_database: Settings) -> None:
    with TestClient(create_app(migrated_database)) as client:
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready", "database": "ok", "redis": "ok"}
        assert client.get("/").json() == {"message": "Welcome to QueuePilot"}


def test_redis_outage_is_degraded(migrated_database: Settings) -> None:
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        settings = migrated_database.model_copy(
            update={"redis_url": SecretStr(f"redis://127.0.0.1:{port}/0")}
        )
        with TestClient(create_app(settings)) as client:
            response = client.get("/health/ready")
            assert response.status_code == 200
            assert response.json() == {
                "status": "degraded",
                "database": "ok",
                "redis": "unavailable",
            }
            assert client.get("/").status_code == 200


def test_database_outage_is_not_ready(migrated_database: Settings) -> None:
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        url = make_url(migrated_database.database_url.get_secret_value()).set(
            host="127.0.0.1", port=reserved.getsockname()[1]
        )
        settings = migrated_database.model_copy(
            update={
                "database_url": SecretStr(url.render_as_string(hide_password=False))
            }
        )
        with TestClient(create_app(settings)) as client:
            response = client.get("/health/ready")
            assert response.status_code == 503
            assert response.json() == {
                "status": "not_ready",
                "database": "unavailable",
                "redis": "ok",
            }
            assert client.get("/health/live").status_code == 200


def test_session_rollback_and_explicit_commit(migrated_database: Settings) -> None:
    async def exercise() -> None:
        engine = create_engine(migrated_database)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("CREATE TABLE probe (id integer PRIMARY KEY)")
                )
            async with sessions() as session:
                await session.execute(text("INSERT INTO probe VALUES (1)"))
                # No commit: closing must roll back.
            async with sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM probe")) == 0
                await session.rollback()
                async with session.begin():
                    await session.execute(text("INSERT INTO probe VALUES (2)"))
            async with sessions() as session:
                assert await session.scalar(text("SELECT id FROM probe")) == 2
                assert await session.scalar(text("SHOW timezone")) == "UTC"
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_stale_schema_is_not_ready(migrated_database: Settings) -> None:
    url = make_url(migrated_database.database_url.get_secret_value()).set(
        drivername="postgresql"
    )
    with psycopg.connect(url.render_as_string(hide_password=False)) as connection:
        connection.execute("UPDATE alembic_version SET version_num = 'stale'")
    with TestClient(create_app(migrated_database)) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["database"] == "unavailable"


def test_real_http_process(migrated_database: Settings, tmp_path: Path) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("QUEUEPILOT_")
    }
    env.update(
        QUEUEPILOT_ENVIRONMENT="test",
        QUEUEPILOT_DATABASE_URL=migrated_database.database_url.get_secret_value(),
        QUEUEPILOT_REDIS_URL=migrated_database.redis_url.get_secret_value(),
    )
    with socket.socket() as listener, (tmp_path / "server.log").open("w+") as log:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "queuepilot.main:create_app",
                "--factory",
                "--fd",
                str(listener.fileno()),
            ],
            pass_fds=(listener.fileno(),),
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, "Server exited before HTTP request"
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health/ready", timeout=1
                    ) as response:
                        assert response.status == 200
                        assert json.load(response)["status"] == "ready"
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/", timeout=1
                    ) as response:
                        assert json.load(response) == {
                            "message": "Welcome to QueuePilot"
                        }
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log.seek(0)
        assert "Application shutdown complete" in log.read()
        assert process.returncode in (0, -signal.SIGTERM)
