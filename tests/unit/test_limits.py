"""Admission limits and browser-origin behavior."""

import asyncio

from fastapi.testclient import TestClient
from redis.asyncio import Redis

from queuepilot.core.config import Settings
from queuepilot.core.limits import SlidingWindowLimiter
from queuepilot.main import create_app
from queuepilot.realtime.hub import Hub


def test_sliding_window_boundary() -> None:
    async def scenario() -> None:
        limiter = SlidingWindowLimiter()
        assert await limiter.allow("client", 2, now=100)
        assert await limiter.allow("client", 2, now=101)
        assert not await limiter.allow("client", 2, now=101)
        assert await limiter.allow("client", 2, now=160)

    asyncio.run(scenario())


def test_http_limit_and_cors() -> None:
    app = create_app(
        Settings(
            environment="test", auth_requests_per_minute=2, read_requests_per_minute=2
        )
    )
    with TestClient(app) as client:
        for _ in range(2):
            assert client.post("/api/v1/auth/login", json={}).status_code == 422
        blocked = client.post("/api/v1/auth/login", json={})
        assert blocked.status_code == 429
        assert blocked.headers["Retry-After"] == "60"
        assert client.get("/api/v1/tickets").status_code == 401
        assert client.get("/api/v1/tickets").status_code == 401
        assert client.get("/api/v1/tickets").status_code == 429
        assert blocked.headers["X-Request-ID"]
        assert (
            client.get("/", headers={"X-Request-ID": "safe_123"}).headers[
                "X-Request-ID"
            ]
            == "safe_123"
        )
        allowed = client.options(
            "/api/v1/tickets",
            headers={
                "Origin": "http://127.0.0.1:8000",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert allowed.status_code == 200
        assert allowed.headers["Access-Control-Allow-Origin"] == "http://127.0.0.1:8000"
        denied = client.options(
            "/api/v1/tickets",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert denied.status_code == 400


def test_websocket_capacity_reservation() -> None:
    hub = Hub(Redis.from_url("redis://127.0.0.1:6379/0"))
    assert hub.try_open(1)
    assert not hub.try_open(1)
    hub.release()
    assert hub.try_open(1)
    hub.release()
