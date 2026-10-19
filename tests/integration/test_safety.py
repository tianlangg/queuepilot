import pytest

from .conftest import validate_test_database


@pytest.mark.parametrize(
    ("url", "acknowledgement"),
    [
        ("postgresql+psycopg://localhost/queuepilot", "1"),
        ("postgresql+psycopg://localhost/queuepilot_test", None),
        ("sqlite:///queuepilot_test", "1"),
    ],
)
def test_reject_unsafe_test_target(url: str, acknowledgement: str | None) -> None:
    with pytest.raises(ValueError, match="Explicit opt-in"):
        validate_test_database(url, acknowledgement)


def test_reject_application_database(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "postgresql+psycopg://localhost/queuepilot_test"
    monkeypatch.setenv("QUEUEPILOT_DATABASE_URL", url)
    with pytest.raises(ValueError, match="must differ"):
        validate_test_database(url, "1")
