from pathlib import Path

import pytest
from pydantic import ValidationError

from queuepilot.core.config import Settings


def test_required_environment() -> None:
    with pytest.raises(ValidationError, match="environment"):
        Settings()


def test_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUEUEPILOT_ENVIRONMENT", "production")
    monkeypatch.setenv("QUEUEPILOT_APP_NAME", " Custom API ")
    settings = Settings()
    assert settings.environment == "production"
    assert settings.app_name == "Custom API"


@pytest.mark.parametrize(
    ("key", "value"),
    [("QUEUEPILOT_ENVIRONMENT", "staging"), ("QUEUEPILOT_APP_NAME", "   ")],
)
def test_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv("QUEUEPILOT_ENVIRONMENT", "test")
    monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        Settings()


def test_optional_dotenv_and_environment_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "QUEUEPILOT_ENVIRONMENT=development\nQUEUEPILOT_APP_NAME=Local API\n",
        encoding="utf-8",
    )
    assert Settings().environment == "development"
    monkeypatch.setenv("QUEUEPILOT_ENVIRONMENT", "production")
    settings = Settings()
    assert settings.environment == "production"
    assert settings.app_name == "Local API"


def test_unknown_dotenv_key_fails(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "QUEUEPILOT_ENVIRONMENT=test\nQUEUEPILOT_APP_NAM=typo\n", encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="Extra inputs"):
        Settings()


@pytest.mark.parametrize("field", ["DATABASE_URL", "REDIS_URL"])
def test_missing_service_configuration(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    monkeypatch.setenv("QUEUEPILOT_ENVIRONMENT", "test")
    monkeypatch.delenv(f"QUEUEPILOT_{field}")
    with pytest.raises(ValidationError, match=field.lower()):
        Settings()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("DATABASE_URL", "sqlite:///unsafe.db"),
        ("DATABASE_URL", "postgresql+psycopg://localhost"),
        ("REDIS_URL", "http://localhost"),
        ("REDIS_URL", "redis://localhost/not-a-db"),
        ("PROBE_TIMEOUT_SECONDS", "0"),
        ("PROBE_TIMEOUT_SECONDS", "nan"),
        ("PROBE_TIMEOUT_SECONDS", "11"),
    ],
)
def test_invalid_service_configuration(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv("QUEUEPILOT_ENVIRONMENT", "test")
    monkeypatch.setenv(f"QUEUEPILOT_{key}", value)
    with pytest.raises(ValidationError):
        Settings()


def test_credentials_are_not_in_settings_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUEUEPILOT_ENVIRONMENT", "test")
    monkeypatch.setenv(
        "QUEUEPILOT_DATABASE_URL", "postgresql+psycopg://user:private@localhost/db"
    )
    assert "private" not in repr(Settings())
