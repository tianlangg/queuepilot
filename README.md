# QueuePilot

An API-first helpdesk under construction. Milestones 1–7 supply the service
foundation, users, bearer authentication, rotating refresh sessions, admin
managed accounts, ticket HTTP flows, atomic assignment, live invalidations,
automated scheduling, and release operations. See
[PLAN.md](PLAN.md).

## Prerequisites and one-command startup

Install Python 3.13, [uv](https://docs.astral.sh/uv/getting-started/installation/),
and Docker with Compose v2 (`up --wait` support). Start the Docker engine.
From the repository root:

```sh
sh scripts/dev.sh
```

The script generates local configuration if missing, builds the non-root API
image, starts PostgreSQL 17.11 and Redis 7.4.11, migrates the database, starts the
API and outbox worker, and waits
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
docker compose --env-file .env.compose logs -f worker
docker compose --env-file .env.compose down
```

`down` retains the PostgreSQL volume. Redis persistence is disabled. Do not use
`down -v` unless you intend to permanently remove local database data. Ports bind
to localhost only. These containers are a development setup, not production
infrastructure; Redis has no local authentication. Never expose it publicly.

After startup, create the first administrator explicitly. The password is read
from a hidden terminal prompt and must have at least 12 characters:

```sh
uv run --locked python -m queuepilot.auth.bootstrap --email admin@example.com
```

Run the command again with the same email to confirm the account already exists.
It rejects a different email once an administrator exists and never promotes an
existing requester or agent. In Docker, use
`docker compose --env-file .env.compose run --rm api python -m queuepilot.auth.bootstrap --email admin@example.com`.
The command requires a migrated database and application settings. Use a unique
password from a password manager; no default credentials exist.

## Run Python on the host

```sh
uv python install 3.13
uv sync --locked
python3 scripts/init_env.py
docker compose --env-file .env.compose up -d --wait postgres redis
uv run --locked alembic upgrade head
uv run --locked uvicorn queuepilot.main:create_app --factory --reload --ws-max-size 1024 --ws-max-queue 32 --no-proxy-headers
# In a second terminal, from the repository root:
uv run --locked python -m queuepilot.outbox.dispatcher
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
| `QUEUEPILOT_TOKEN_SECRET` | Yes | Random signing key, at least 32 characters; keep stable across API replicas and restarts |
| `QUEUEPILOT_TOKEN_ISSUER` | No | Access token issuer; default `queuepilot` |
| `QUEUEPILOT_TOKEN_AUDIENCE` | No | Access token audience; default `queuepilot-api` |
| `QUEUEPILOT_ACCESS_TOKEN_MINUTES` | No | Access token lifetime, 1–60 minutes; default 15 |
| `QUEUEPILOT_REFRESH_TOKEN_DAYS` | No | Refresh session lifetime, 1–90 days; default 30 |
| `QUEUEPILOT_WEBSOCKET_ORIGINS` | No | Comma-separated exact HTTP(S) origins permitted to open WebSockets; default localhost ports 8000 |
| `QUEUEPILOT_AUTH_REQUESTS_PER_MINUTE` | No | Per-process, per-client limit for login, refresh, and WebSocket credential issuance; default 30 |
| `QUEUEPILOT_READ_REQUESTS_PER_MINUTE` | No | Per-process, per-client limit for API reads; default 300 |
| `QUEUEPILOT_WRITE_REQUESTS_PER_MINUTE` | No | Per-process, per-client limit for other writes; default 120 |
| `QUEUEPILOT_WEBSOCKET_CONNECTION_LIMIT` | No | Concurrent WebSocket admission cap per API process; default 500 |

The app reads an optional `.env` in the **current working directory**; environment
variables override it. Unknown `.env` keys are rejected. `.env.compose` belongs
to Compose and contains `POSTGRES_PASSWORD`; `.env.test` belongs to the integration
test runner. Neither is loaded into application settings. Compose injects internal
service addresses; host settings use ports 55432 (PostgreSQL) and 56379 (Redis).

Settings are validated when the factory runs and are immutable per app. URLs are
represented as secrets in settings, and probe responses contain no connection
strings or exception details. Do not log raw settings values or caught driver
exceptions. Production settings require explicit non-local HTTPS browser origins and a unique
token secret. REST CORS uses the same exact browser-origin list as WebSockets.

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
HTTP status alone does not distinguish it. Redis degradation means live
notifications are unavailable until the listener and worker reconnect.

Lifespan creates lazy clients so service outages do not prevent liveness. Shutdown
closes Redis and disposes the database pool, including on exceptional exits.
Sessions are per request/task; `get_session` closes and rolls back uncommitted
work. Services must own explicit transactions; helpers never silently commit.
Database connections use UTC, a 5-second statement timeout, and a 3-second lock
timeout. Pools are bounded. Assignment retry policy is described below. The
worker uses its own database pool and Redis client. API startup and ticket REST
writes do not depend on Redis availability; live delivery resumes when Redis does.

## Authentication and accounts

The API uses JSON bearer tokens in the `Authorization` header; it does not set
credential cookies, so cookie CSRF handling does not apply. Store refresh tokens
in a protected client secret store, never in a URL or log. Access tokens expire
after 15 minutes by default and are checked against the active database session
on every protected request. Rotation invalidates the prior access token; reuse
of an old refresh token revokes its whole family. Logout revokes the current
family. Disabling a user revokes all their refresh sessions and denies login.
Signing key rotation invalidates existing access tokens; refresh sessions remain
valid and can issue tokens with the new key. Revoke sessions if the key was
compromised.

```sh
curl -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"YOUR_PASSWORD"}'
# POST /api/v1/auth/refresh {"refresh_token":"..."}
# POST /api/v1/auth/logout with Authorization: Bearer <access_token>
# GET  /api/v1/auth/me with Authorization: Bearer <access_token>
```

An administrator can `POST /api/v1/users` with `email`, `password`, and role
`requester` or `agent`; `GET /api/v1/users/{id}`; and
`PATCH /api/v1/users/{id}/status` with `{"disabled":true}`. Users can read their
own record. All user routes require authentication. Public registration, API
administrator creation, and role mutation are unavailable. Duplicate emails
return 409, denied actions 403, invalid credentials 401, and invalid input 422.
Authentication responses carry `Cache-Control: no-store`. See the
[permission matrix](docs/0004-authentication.md) for complete rules.

## Tickets

All `/api/v1/tickets` endpoints require a bearer token. A requester can create,
list, and read their own tickets, and add public comments. Agents and admins can
read the shared queue and add public or internal comments. Internal comments are
absent from requester responses. Admins can change ticket status; agents can
change status only when assigned.
Requesters can reopen their own resolved or closed tickets.

```sh
curl -X POST http://127.0.0.1:8000/api/v1/tickets \
  -H 'Authorization: Bearer YOUR_ACCESS_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"title":"Printer offline","description":"Floor 2 printer is offline"}'
# GET /api/v1/tickets?limit=20&status=open
# GET /api/v1/tickets/{id}
# GET /api/v1/tickets/{id}/comments?limit=20
# POST /api/v1/tickets/{id}/comments
# {"body":"Investigating","internal":false,"expected_version":1}
# POST /api/v1/tickets/{id}/transitions
# {"status":"in_progress","expected_version":2}
# POST /api/v1/tickets/{id}/claim {"expected_version":1}
# POST /api/v1/tickets/{id}/reassign (admin only)
# {"assignee_id":"AGENT_UUID","expected_version":2}
```

Lists use stable `(created_at, id)` keyset ordering. Follow `next_cursor` using
the same filters; limit is 1–100 (default 20). Detail and list use the same
visibility rule. Comments use their own ascending cursor. A missing or hidden
ticket returns 404, a prohibited action 403, stale version or invalid transition
409, malformed input or cursor 422, and a body over 16 KiB 413. Each mutation
increments `version`, writes audit and an outbox invalidation event in one
transaction. Supply the current `expected_version` for comments and transitions;
retry conflicts after refetching. The worker publishes pending outbox events to
Redis after commit. See [ticket API and transition rules](docs/0006-tickets.md).

Agents and admins can claim an unassigned open ticket. Claim moves it to
`in_progress`; repeating a claim by its current assignee returns 200 without a
new version or event, even with the original `expected_version`. Admins can assign
an open or in-progress ticket to an active agent; assignment of an open ticket
also moves it to `in_progress`. A changed assignment increments the version and
writes assignment history, audit, and outbox records atomically. Stale versions or
competing claims return 409. An invalid, disabled, or non-agent reassignment
target returns 422. Lock, statement, deadlock, and serialization failures return
503 with `Retry-After: 1`. Clients should refetch before a bounded retry; the
service does not automatically replay an assignment. See [assignment rules](docs/0007-assignment.md).

## Live updates and worker

`POST /api/v1/realtime/credentials` requires a valid bearer token and returns a
single-use credential valid for 60 seconds. Connect to
`/api/v1/realtime/ws` with an allowed `Origin`, then send
`{"type":"authenticate","credential":"..."}` as the first WebSocket message
within five seconds. Do not put bearer tokens or the connection credential in
the URL. A successful connection sends `{"type":"ready","resync_required":true}`.
Refetch ticket state through REST after connecting, then send
`{"type":"subscribe","ticket_id":"UUID"}`. The server checks current ticket
visibility and replies with `subscribed` and the current version, or a sanitized
`error`. `unsubscribe` removes a ticket; `pong` acknowledges a `heartbeat`.
Connections allow 32 ticket subscriptions and close if heartbeats go unanswered
for 30 seconds. A credential cannot be reused. Active connections end within
15 minutes, on session revocation/disablement, or on backpressure.

`ticket_changed` contains an event ID, ticket ID, version, event type, and
timestamp; it contains no ticket or comment text. Requesters receive no internal
comment events. The server rechecks the session and ticket permission before
delivery. A `resync_required` message, close code 1013, reconnect, or version
gap means the client must refetch REST state and subscribe again. Live events
are hints, not a durable stream. Duplicates are suppressed per connection by
event ID and ticket version, but a missed event is possible. See the
[realtime protocol and operations](docs/0008-realtime.md).

The worker also claims up to 10 due scheduled jobs per cycle. An unassigned open
ticket escalates after 60 elapsed minutes; a resolved ticket closes after seven
elapsed days. Administrators can change both UTC elapsed-time periods through
`GET/PATCH /api/v1/scheduling/policy`, supplying both minute values and the
current `expected_version`. `GET /api/v1/scheduling/jobs?state=failed&limit=20`
lists failed jobs; `POST /api/v1/scheduling/jobs/{id}/retry` queues one again.
Use the last job ID as the `after` query parameter to page through a job list.
Policy changes reschedule pending jobs. Ticket transitions and assignments cancel
stale work, and the worker rechecks state under the ticket lock. See
[scheduling rules and operations](docs/0009-scheduling.md).

The worker claims up to 10 due outbox events per cycle, publishes after claim
commit, and records success in PostgreSQL. It uses 30-second leases and bounded
retry backoff (1–60 seconds). A crash after publish can cause a duplicate;
delivery is at least once when Redis is available, not exactly once. Redis
outages do not roll back ticket mutations. `GET /health/ready` reports Redis
degradation, but does not prove the worker is running; inspect worker logs and
pending rows when investigating delivery lag:

```sql
SELECT count(*) FROM outbox_events WHERE published_at IS NULL;
```

Administrators can query `GET /api/v1/operations/metrics` for outbox and job
lag, failed-job counts, and local WebSocket connection count. API requests and
WebSocket admission have bounded per-process limits; a trusted ingress must
enforce fleet-wide limits across replicas. See the [release and operations
runbook](docs/0010-release.md) for deployment, backup/restore rehearsal,
rollback, and the isolated reference-load harness.

## Migrations

```sh
uv run --locked alembic upgrade head
uv run --locked alembic current
uv run --locked alembic check
```

The reviewed revisions establish the foundation baseline, authentication tables,
and the ticket, comment, audit, outbox, assignment history, and connection
credential tables. Revision `0005_realtime` adds outbox retry and lease
columns. Revision `0006_scheduling` adds ticket deadlines, policy audit, and
durable jobs; it backfills eligible existing tickets. It cannot be downgraded
without discarding job and audit history. Migrations are a
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
server must be dedicated to testing. Redis tests exercise Pub/Sub; nothing is flushed.

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

CI runs locked installation, all quality checks, real-service tests, an isolated
backup/restore rehearsal and short load smoke, Compose configuration validation,
an image build, and a full Compose HTTP smoke check.
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
