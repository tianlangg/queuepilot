# 0002: Service foundation and milestone 1 verification

Status: implemented and locally accepted. Subsequent Docker verification is
recorded in [0003](0003-milestone1-acceptance.md).

## Decisions

Use SQLAlchemy 2 async engines/sessions with psycopg 3, Alembic, and redis-py async
clients. Lifespan owns client cleanup using an exit stack. Engines and Redis
clients connect lazily: neither outage prevents the independent liveness route.
Request-scoped sessions close without committing; future services own explicit
transaction boundaries. Never share sessions across concurrent tasks.

Use bounded pools and timeouts, UTC database sessions, and secret URL fields.
Readiness checks the database's expected Alembic revision and Redis PING in
parallel. Database unavailability, missing schema, or stale revision returns 503;
Redis failure alone returns 200 with a degraded JSON status. Probes expose no
exception content. This is operational readiness, not authentication or complete
security hardening.

The initial migration deliberately establishes only Alembic's baseline. There is
no domain schema to invent before feature implementation. Subsequent revisions
must update the expected schema revision and include migration compatibility and
recovery review. No automatic API migrations or destructive downgrade operations.

Local Compose uses PostgreSQL 17.11 and Redis 7.4.11, a separate migration job, and
a non-root Python API container. Images use explicit release tags; OS image layers
are not digest-pinned. A small setup script generates local random credentials,
preserves existing values, and keeps Compose-only settings outside application
`.env`. All published ports bind to loopback. PostgreSQL uses a retained named
volume; Redis is disposable. Separate test containers use ephemeral storage.

Integration tests require explicit URLs, an opt-in, and a control database named
`queuepilot_test`. They create and drop only UUID-suffixed databases on that
server. Normal tests skip missing service config; CI requires integration tests.
No mocks or SQLite are used as evidence for PostgreSQL transactions/migrations.

## Verification evidence

On macOS ARM64, Python 3.13.5, uv 0.13.1:

- `sh scripts/check.sh --require-integration` passed in a separate fresh uv
  environment with explicit test URLs and opt-in. This includes locked dependency
  installation, Ruff lint/format, strict mypy, and the full test suite.
- Ruff linting/formatting and strict mypy for source, tests, scripts, migrations.
- Compose configuration validated using a temporary Compose v2.39.4 CLI.
- Setup script verified to preserve existing config/credentials and file permissions.
- 42 pytest cases passed with real PostgreSQL 17.11 and Redis 7.4.11.
- Empty-database upgrade and repeated head upgrade, expected version table,
  unmigrated/stale schema rejection, actual healthy/degraded/unavailable probes.
- Real PostgreSQL commit/rollback and UTC session behavior.
- Independent Uvicorn process: successful root/readiness HTTP requests and graceful
  shutdown. Test-owned databases are cleaned after each case.

The services were temporary native binaries under `/private/tmp`, not containers;
network access, PostgreSQL shared memory, and localhost binds required sandbox
approval. PostgreSQL binaries came from the Zonky embedded binary distribution;
Redis was built from its official source release. No system service installation.

One upstream Starlette deprecation warning remains for HTTPX-backed TestClient.
Tests pass. Retain HTTPX per repository conventions and revisit as the transport
changes upstream.

At the initial implementation checkpoint, Docker was unavailable and container
acceptance was pending. On 2026-10-19, Docker Desktop was available: image builds,
full Compose startup, HTTP/outage/recovery checks, and all 42 tests against Docker
services passed, including the documented quality/test gates inside Linux. See
[the final acceptance record](0003-milestone1-acceptance.md) for commands and results.
Hosted CI has not been triggered; nothing was committed or pushed. Authentication
and later milestones are not started.
