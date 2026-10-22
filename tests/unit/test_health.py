import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from queuepilot.api.health import redis_ready
from queuepilot.core.config import Settings
from queuepilot.core.resources import Resources
from queuepilot.main import create_app


@pytest.mark.parametrize(
    ("database_ok", "redis_ok", "code", "status"),
    [
        (True, True, 200, "ready"),
        (True, False, 200, "degraded"),
        (False, True, 503, "not_ready"),
        (False, False, 503, "not_ready"),
    ],
)
def test_readiness_contract(
    database_ok: bool, redis_ok: bool, code: int, status: str
) -> None:
    with (
        patch(
            "queuepilot.api.health.database_ready", AsyncMock(return_value=database_ok)
        ),
        patch("queuepilot.api.health.redis_ready", AsyncMock(return_value=redis_ok)),
        TestClient(create_app(Settings(environment="test"))) as client,
    ):
        response = client.get("/health/ready")
    assert response.status_code == code
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "status": status,
        "database": "ok" if database_ok else "unavailable",
        "redis": "ok" if redis_ok else "unavailable",
    }


def test_liveness_and_database_outage() -> None:
    with TestClient(create_app(Settings(environment="test"))) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {
            "status": "not_ready",
            "database": "unavailable",
            "redis": "unavailable",
        }
        assert client.get("/").status_code == 200
        assert client.get("/health/live").status_code == 200


def test_resources_closed_on_shutdown() -> None:
    engine = AsyncMock()
    redis = AsyncMock()
    app = create_app(Settings(environment="test"))
    with (
        patch("queuepilot.main.create_engine", return_value=engine),
        patch("queuepilot.main.create_redis", return_value=redis),
        patch("queuepilot.main.Hub") as hub_class,
    ):
        hub_class.return_value.close = AsyncMock()
        with TestClient(app):
            engine.dispose.assert_not_awaited()
            redis.aclose.assert_not_awaited()
        engine.dispose.assert_awaited_once()
        redis.aclose.assert_awaited_once()
    assert not hasattr(app.state, "resources")


def test_engine_closed_if_later_startup_fails() -> None:
    engine = AsyncMock()
    with (
        patch("queuepilot.main.create_engine", return_value=engine),
        patch(
            "queuepilot.main.create_redis", side_effect=RuntimeError("startup failed")
        ),
        pytest.raises(RuntimeError, match="startup failed"),
        TestClient(create_app(Settings(environment="test"))),
    ):
        pass
    engine.dispose.assert_awaited_once()


def test_probe_deadline_handles_unresponsive_redis() -> None:
    async def unresponsive() -> bool:
        await asyncio.Event().wait()
        return True

    async def exercise() -> None:
        redis = AsyncMock()
        redis.ping.side_effect = unresponsive
        resources = Resources(
            engine=AsyncMock(),
            sessions=async_sessionmaker(),
            redis=redis,
            probe_timeout_seconds=0.01,
        )
        assert await asyncio.wait_for(redis_ready(resources), timeout=1) is False

    asyncio.run(exercise())
