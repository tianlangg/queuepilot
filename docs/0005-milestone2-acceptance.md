# Milestone 2 local acceptance evidence

Date: 2026-10-19. Environment: macOS arm64, Python 3.13.5, uv 0.13.1,
Docker 29.8.2, isolated PostgreSQL 17.11 and Redis 7.4.11 containers.

## Implemented behavior

Migration `0002_auth` adds constrained users and indexed refresh sessions.
An explicit terminal command creates the first administrator without default
credentials. Admins create requester and agent accounts and disable accounts.
Bearer login, refresh rotation, replay family revocation, logout, access expiry,
disabled-user denial, and user read permissions work against PostgreSQL.
All user routes require authentication. Response models omit password hashes;
validation errors do not echo submitted values. Credential cookies are not used.

## Checks run

| Check | Result |
| --- | --- |
| `uv sync --locked` | Passed after updating `uv.lock` |
| `uv run --locked ruff check .` | Passed |
| `uv run --locked ruff format --check .` | Passed; 42 files formatted |
| `uv run --locked mypy src tests scripts migrations` | Passed; 34 source files |
| `uv run --locked pytest tests/unit -q` | Passed; 73 tests |
| `uv run --locked python scripts/test_integration.py` | Passed; 89 tests total against dedicated PostgreSQL/Redis |
| `git diff --check` | Passed |
| `docker compose --env-file .env.compose up --build --wait api` | Passed; API healthy after migration |
| Localhost `/health/ready` | 200, database and Redis `ok` |
| Localhost `/api/v1/auth/me` without credentials | 401 |

Integration tests covered migration from empty and revision `0001_foundation`,
repeatable bootstrap, valid and invalid login, JWT claim and expiry validation,
refresh replay and family revocation, logout, disabled users, admin creation
restrictions, role denial, and cross-user reads. Pure policy tests cover each
role/action cell in the documented matrix. The test runner used its guarded,
dedicated `queuepilot_test` control database and cleaned up per-test databases.
Containers started for acceptance were stopped; the development database
volume was retained.

## Limits

Hosted GitHub Actions has not been triggered because no commit or push was
authorized. Browser token storage, password reset, email verification, role
changes, and refresh-session retention cleanup remain future work. Ticket
permission rules are documented and unit tested, but ticket endpoints are a
later milestone. The existing TestClient/httpx deprecation warning is upstream
and did not affect test results.
