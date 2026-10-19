import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from queuepilot.core.config import Settings
from queuepilot.main import create_app


def test_factory_creates_independent_apps() -> None:
    first = create_app(Settings(environment="test", app_name="First"))
    second = create_app(Settings(environment="test", app_name="Second"))
    assert first is not second
    with TestClient(first) as client:
        assert client.get("/").json() == {"message": "Welcome to First"}
    with TestClient(second) as client:
        assert client.get("/").json() == {"message": "Welcome to Second"}


def test_factory_loads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUEUEPILOT_ENVIRONMENT", "test")
    with TestClient(create_app()) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.json() == {"message": "Welcome to QueuePilot"}
        assert client.get("/missing").status_code == 404


def test_factory_rejects_missing_configuration() -> None:
    with pytest.raises(ValidationError, match="environment"):
        create_app()


def test_lifespan_startup_and_shutdown(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="queuepilot.main")
    app = create_app(Settings(environment="test"))
    assert not caplog.records
    with TestClient(app) as client:
        assert caplog.messages == ["Starting QueuePilot"]
        assert client.get("/").status_code == 200
    assert caplog.messages == ["Starting QueuePilot", "Stopped QueuePilot"]


def test_lifespan_cleanup_on_exception(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="queuepilot.main")
    app = create_app(Settings(environment="test"))
    with pytest.raises(RuntimeError, match="interrupted"):
        with TestClient(app):
            raise RuntimeError("interrupted")
    assert caplog.messages == ["Starting QueuePilot", "Stopped QueuePilot"]
