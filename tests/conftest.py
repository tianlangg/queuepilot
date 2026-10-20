"""Keep tests independent of developer configuration and local .env files."""

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for key in os.environ:
        if key.startswith("QUEUEPILOT_") and not key.startswith("QUEUEPILOT_TEST_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(
        "QUEUEPILOT_DATABASE_URL",
        "postgresql+psycopg://unused@127.0.0.1:1/queuepilot_test",
    )
    monkeypatch.setenv("QUEUEPILOT_REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv(
        "QUEUEPILOT_TOKEN_SECRET", "test-only-token-secret-at-least-32-bytes"
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--require-integration",
        action="store_true",
        help="Fail instead of skipping services",
    )
