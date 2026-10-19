"""Real service tests create/drop only unique databases on an opted-in test server."""

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from queuepilot.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def validate_test_database(raw: str, acknowledged: str | None) -> str:
    url = make_url(raw)
    if (
        acknowledged != "1"
        or url.drivername != "postgresql+psycopg"
        or url.database != "queuepilot_test"
    ):
        raise ValueError(
            "Explicit opt-in and a dedicated queuepilot_test database required"
        )
    development = os.environ.get("QUEUEPILOT_DATABASE_URL")
    if development:
        other = make_url(development)
        if (url.host, url.port, url.database) == (
            other.host,
            other.port,
            other.database,
        ):
            raise ValueError("Test database must differ from the application database")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def service_urls(pytestconfig: pytest.Config) -> tuple[str, str]:
    database = os.environ.get("QUEUEPILOT_TEST_DATABASE_URL")
    redis = os.environ.get("QUEUEPILOT_TEST_REDIS_URL")
    if not database or not redis:
        if pytestconfig.getoption("--require-integration"):
            pytest.fail(
                "Set QUEUEPILOT_TEST_DATABASE_URL and QUEUEPILOT_TEST_REDIS_URL"
            )
        pytest.skip("Explicit isolated PostgreSQL/Redis URLs not configured")
    validate_test_database(database, os.environ.get("QUEUEPILOT_ALLOW_TEST_DATABASE"))
    return database, redis


@pytest.fixture
def empty_database(service_urls: tuple[str, str]) -> Iterator[Settings]:
    database_url, redis_url = service_urls
    control = make_url(database_url).set(drivername="postgresql")
    name = f"queuepilot_test_{uuid.uuid4().hex}"
    with psycopg.connect(
        control.render_as_string(hide_password=False),
        autocommit=True,
        connect_timeout=5,
    ) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield Settings(
                environment="test",
                database_url=SecretStr(
                    make_url(database_url)
                    .set(database=name)
                    .render_as_string(hide_password=False)
                ),
                redis_url=SecretStr(redis_url),
                probe_timeout_seconds=0.5,
            )
        finally:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


def migrate(settings: Settings, target: str = "head") -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("QUEUEPILOT_")
    }
    env.update(
        QUEUEPILOT_ENVIRONMENT="test",
        QUEUEPILOT_DATABASE_URL=settings.database_url.get_secret_value(),
        QUEUEPILOT_REDIS_URL=settings.redis_url.get_secret_value(),
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "alembic.ini"),
            "upgrade",
            target,
        ],
        env=env,
        check=True,
        capture_output=True,
        timeout=20,
    )


@pytest.fixture
def migrated_database(empty_database: Settings) -> Settings:
    migrate(empty_database)
    return empty_database
