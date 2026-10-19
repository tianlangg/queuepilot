# QueuePilot

An API-first helpdesk under construction. Milestone 1 supplies packaging, typed
configuration, async PostgreSQL sessions, Alembic migrations, Redis clients,
health probes, local containers, and CI checks. Authentication, tickets, workers,
and WebSockets are later milestones. See [PLAN.md](PLAN.md).

## Prerequisites and one-command startup

Install Python 3.13, [uv](https://docs.astral.sh/uv/getting-started/installation/),
and Docker with Compose v2 (`up --wait` support). Start the Docker engine.
From the repository root:

```sh
sh scripts/dev.sh
```

The script generates local configuration if missing, builds the non-root API
image, starts PostgreSQL 17.11 and Redis 7.4.11, migrates the database, and waits
for API readiness. Open <http://127.0.0.1:8000/docs>.

If Docker Desktop is installed on macOS but `docker` is not found, enable its CLI
tools on your PATH, or run the script with its bundled tools:

```sh
PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH" sh scripts/dev.sh
```

Generated configuration contains random local database credentials and is ignored
by Git. Existing values are preserved. Keep `.env.compose`: changing its password
does **not** change credentials in an existing PostgreSQL volume. To rotate them,
change the database role password and matching configuration together.

```sh
curl --fail http://127.0.0.1:8000/
curl --fail http://127.0.0.1:8000/health/live
curl --fail http://127.0.0.1:8000/health/ready
docker compose --env-file .env.compose logs -f api
docker compose --env-file .env.compose down
```

`down` retains the PostgreSQL volume. Redis persistence is disabled. Do not use
`down -v` unless you intend to permanently remove local database data. Ports bind
to localhost only. These containers are a development setup, not production
infrastructure; Redis has no local authentication. Never expose it publicly.

## Run Python on the host

```sh
uv python install 3.13
uv sync --locked
python3 scripts/init_env.py
docker compose --env-file .env.compose up -d --wait postgres redis
uv run --locked alembic upgrade head
uv run --locked uvicorn queuepilot.main:create_app --factory --reload
```

Stop the Compose API first if it occupies port 8000:

```sh
docker compose --env-file .env.compose stop api
```

Python is constrained to the 3.13 series; patch updates remain allowed.
`uv sync --locked` installs the package and development tools into `.venv` and
fails if the dependency declarations and lockfile disagree. Runtime-only installs
use `uv sync --locked --no-dev`. Omit `--reload` outside development.

## Configuration

`python3 scripts/init_env.py` creates or adds missing values to `.env`,
`.env.compose`, and `.env.test`. It never prints credentials. `.env.example`
documents safe placeholders for manual setup; do not use `REPLACE_ME` as a real
password. Never commit local environment files.

| Application variable | Required | Meaning |
| --- | --- | --- |
| `QUEUEPILOT_ENVIRONMENT` | Yes | `development`, `test`, or `production` |
| `QUEUEPILOT_DATABASE_URL` | Yes | `postgresql+psycopg://user:password@host:port/database` |
| `QUEUEPILOT_REDIS_URL` | Yes | `redis://host:port/0` or `rediss://...` |
| `QUEUEPILOT_APP_NAME` | No | Nonblank display name; default `QueuePilot` |
| `QUEUEPILOT_PROBE_TIMEOUT_SECONDS` | No | Per-dependency probe deadline, >0 and <=10; default 2 |

The app reads an optional `.env` in the **current working directory**; environment
variables override it. Unknown `.env` keys are rejected. `.env.compose` belongs
to Compose and contains `POSTGRES_PASSWORD`; `.env.test` belongs to the integration
test runner. Neither is loaded into application settings. Compose injects internal
service addresses; host settings use ports 55432 (PostgreSQL) and 56379 (Redis).

Settings are validated when the factory runs and are immutable per app. URLs are
represented as secrets in settings, and probe responses contain no connection
strings or exception details. Do not log raw settings values or caught driver
exceptions. The environment name alone does not enable security features.

## Probes and resource ownership

| Endpoint | Response |
| --- | --- |
| `GET /` | 200, `{"message":"Welcome to QueuePilot"}` with default app name |
| `GET /health/live` | 200, `{"status":"ok"}`; no dependency I/O |
| `GET /health/ready` | 200 `ready` if PostgreSQL schema and Redis are healthy |
| Same, Redis unavailable | 200 `degraded`; REST remains eligible for traffic |
| Same, database unavailable/unmigrated/stale | 503 `not_ready` |

Readiness includes `database` and `redis`, each `ok` or `unavailable`, uses
`Cache-Control: no-store`, and checks dependencies concurrently with bounded
per-dependency deadlines. Redis degradation should be monitored from the JSON;
HTTP status alone does not distinguish it. No notifications are implemented yet.

Lifespan creates lazy clients so service outages do not prevent liveness. Shutdown
closes Redis and disposes the database pool, including on exceptional exits.
Sessions are per request/task; `get_session` closes and rolls back uncommitted
work. Services must own explicit transactions; helpers never silently commit.
Database connections use UTC, a 5-second statement timeout, and a 3-second lock
timeout. Pools are bounded. No domain tables or transaction retry policy exist yet.

## Migrations

```sh
uv run --locked alembic upgrade head
uv run --locked alembic current
uv run --locked alembic check
```

The reviewed initial revision establishes an empty baseline and Alembic's version
table. User/ticket tables will be added with their features. Migrations are a
separate operation, never an API startup side effect; Compose gates API startup on
the migration job. Readiness requires the expected revision from
`src/queuepilot/db/base.py`; update that constant when adding a revision.

Review generated revisions before running them. Do not automatically downgrade
future destructive changes; define recovery, compatibility, and data retention
for each such migration. The baseline contains no domain data.

## Verification

Fast checks do not require services:

```sh
sh scripts/check.sh
```

This runs the following commands; integration tests explicitly skip if isolated
service URLs are absent:

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts migrations
uv run pytest
```

For the full acceptance suite, start the separate ephemeral test services:

```sh
python3 scripts/init_env.py
docker compose --env-file .env.compose --profile test up -d --wait postgres-test redis-test
uv run --locked python scripts/test_integration.py
docker compose --env-file .env.compose --profile test stop postgres-test redis-test
```

The test runner reads `.env.test`, opts in to database creation, and uses
`--require-integration` so missing configuration fails instead of skipping tests.
Test PostgreSQL is on port 55433 and test Redis on 56380. The control database must
be named `queuepilot_test`. Its role must have `CREATEDB`; the local test container
provides that. Each integration case creates a uniquely named database, applies
migrations as needed, and drops **only that database** in cleanup. The control
server must be dedicated to testing. Redis checks only use PING; nothing is flushed.

For an independently managed test server, explicitly supply
`QUEUEPILOT_TEST_DATABASE_URL`, `QUEUEPILOT_TEST_REDIS_URL`, and
`QUEUEPILOT_ALLOW_TEST_DATABASE=1`, then run:

```sh
uv run --locked pytest --require-integration
```

Do not point those settings at development or production infrastructure. A guard
requires the exact control database name and opt-in, and rejects a matching
application target when supplied in the process environment. It cannot determine
whether an operator has mislabeled a real server. Cleanup does not downgrade or
reset existing application schemas. A hard-killed test process can leave uniquely
named test databases for manual cleanup on the dedicated test server.

CI runs locked installation, all quality checks, real-service tests, Compose
configuration validation, an image build, and a full Compose HTTP smoke check.
Milestone 1 passed local Docker acceptance and its quality/test gates inside
Linux, with all 42 tests passing against Docker services. Hosted GitHub Actions
has not been triggered. See [acceptance evidence and limitations](docs/0003-milestone1-acceptance.md).

## Learning map

- `main.py`: factory, lifespan, `AsyncExitStack`, and explicit root response model.
- `core/config.py`: Pydantic configuration validation and secret representation.
- `core/resources.py`: application-owned engine/session factory/Redis client.
- `db/session.py`: bounded engine construction and request-local session dependency.
- `db/base.py`: model metadata, constraint naming, and expected schema revision.
- `api/health.py`: thin probe routes and sanitized failure behavior.
- `migrations/`: Alembic environment and reviewed baseline revision.
- `tests/`: isolated configuration, HTTP/lifecycle tests, and real-service evidence.
- `scripts/`, `compose.yaml`, `Dockerfile`, `.github/workflows/ci.yml`: repeatable operations.

Trace startup through the factory and lifespan, then follow a readiness request.
Study the difference between closing a session, rolling back, and committing by
reading the real PostgreSQL transaction test. Compare the missing-service unit
checks with real integration tests: mocks verify response contracts, while real
services verify driver and migration behavior.
