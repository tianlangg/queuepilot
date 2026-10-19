# 0001: Python foundation

Status: accepted for milestone 1, stage 1. This is a historical stage 1 record;
[0002](0002-service-foundation.md) describes the subsequent service foundation.

Use Python 3.13 (minor series pinned, patch updates allowed), uv, a committed
lockfile, and Hatchling packaging with the `src/queuepilot` layout. Runtime
dependencies provide the ASGI app, server, response models, and validated settings;
the development group provides HTTPX, pytest, Ruff, and mypy.

`create_app()` loads immutable settings per app, with optional explicit settings
injection for tests. No import-time application or cached global settings are
created. Require the deployment environment explicitly; do not invent service
credentials before those services exist. Optional local `.env` loading is relative
to the working directory; process environment values take precedence.

Keep the single root route and response schema in `main.py` until feature routes
justify separate modules. The lifespan logs startup and shutdown, using `finally`
for cleanup even when the context exits exceptionally. No resources or artificial
readiness flags are allocated. Uvicorn owns process logging configuration.

External services, health/readiness probes, persistence, authentication, workers,
and CI automation remain outside this stage. Full milestone 1 acceptance has not
been met by this stage alone.

## Stage 1 acceptance evidence

Verified on macOS ARM64 with Python 3.13.5 and uv 0.13.1:

- `uv sync --locked`: successful, including installation into a separate empty
  environment from the lockfile using downloaded package cache.
- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: passed.
- `uv run mypy src tests`: passed in strict mode.
- `uv run pytest`: 11 passed, with one upstream Starlette warning deprecating
  its HTTPX TestClient transport in favor of `httpx2`. HTTPX is retained per the
  repository conventions; revisit the transport as that dependency evolves.
- A Uvicorn process from the separate environment served `GET /` with HTTP 200
  and `{"message":"Welcome to QueuePilot"}`. SIGTERM completed ASGI shutdown.
  Network downloads and localhost binding required sandbox approval.

Stage 1 acceptance is satisfied; later milestone 1 stages are not implemented.
No PostgreSQL, Redis, Docker, or application credentials were required. Other
operating systems and Python patch releases were not exercised here.
