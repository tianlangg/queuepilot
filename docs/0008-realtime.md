# Realtime invalidations and outbox delivery

Milestone 5 adds a separate outbox worker and authorized, per-ticket WebSocket
subscriptions. PostgreSQL remains authoritative. Every ticket mutation commits
its audit row and outbox event in the same transaction; the worker claims due
events with `FOR UPDATE SKIP LOCKED`, commits a 30-second lease, publishes to
Redis, and marks the row published. It never publishes inside a ticket
transaction. Up to 10 events are processed per cycle. Failed Redis publication
clears the lease and schedules bounded exponential backoff from 1 to 60 seconds.
An expired lease can be reclaimed by another worker. A crash after Redis publish
but before the database acknowledgement can cause duplicates. There is no
exactly-once notification guarantee.

Each API replica has a Redis Pub/Sub listener and a set of local connections.
Events are validated before fan-out. The wire envelope contains event ID,
ticket ID, ticket version, event type, and UTC timestamp. Outbox payloads only
carry an internal-comment visibility marker where needed; no ticket title,
description, or comment body is published. Each connection can subscribe to at
most 32 specific tickets. There is no global feed. The API rechecks active
session status, current role, and ticket visibility before delivery. Requesters
never receive internal-comment events. Duplicate event IDs and old ticket
versions are suppressed; a gap produces `resync_required`. Redis is not
replayable, so clients must refetch authorized REST state on connect, reconnect,
gap, or resync signal. A bounded queue of 32 events per connection closes a slow
client with code 1013 and a resync hint.

## Connection protocol and security

An authenticated REST call to `POST /api/v1/realtime/credentials` issues a
random one-use credential. Only its SHA-256 hash is stored. It expires after 60
seconds and is consumed under a row lock, so concurrent replay has one winner.
The WebSocket at `/api/v1/realtime/ws` requires an exact configured `Origin` and
an `authenticate` message containing the credential within five seconds. This
avoids putting bearer tokens in URLs. Missing or invalid credentials close with
code 1008. The connection sends `ready` with `resync_required: true` after
authentication. A `subscribe` command requires a ticket UUID and is checked
against the current database visibility rule. `unsubscribe` removes it.

The server sends an application `heartbeat` about every 10 seconds; the client
responds with `pong`. Sessions are revalidated at most five seconds apart and
again before each event. Revocation, disablement, or unanswered heartbeats close
the connection with 1008. A connection lasts no longer than 15 minutes or its
refresh session expiry. Changed roles are observed on the next revalidation,
and each delivery uses the current role and ticket ownership. Connection
credentials are purged by the worker in bounded batches once expired for a day.
Set `QUEUEPILOT_WEBSOCKET_ORIGINS` to the exact browser origins for the
deployment; wildcard origins are rejected. The default permits only localhost
port 8000 for local development.

## Operations and migration

Run `uv run --locked alembic upgrade head`, then start the API and a separate
`uv run --locked python -m queuepilot.outbox.dispatcher` process. Compose's
`worker` service does this locally. The API and worker can start while Redis is
unavailable. Ticket writes still commit; the worker retries pending rows after
recovery. Readiness reports Redis degradation. It does not detect a stopped
worker; inspect its logs and pending outbox count. Worker logs deliberately omit
driver errors and event payloads. Stop the worker with SIGTERM or SIGINT for
graceful pool closure. Existing claims remain recoverable after lease expiry.

Revision `0005_realtime` adds outbox `available_at`, `lease_until`,
`lease_token`, and `attempts`, replaces the pending index with a due index, and
adds `connection_credentials` with an expiry index. Existing pending events get
an immediate availability timestamp and are published after the worker starts.
The migration does not remove existing ticket data. Downgrading drops credential
records and retry state, so plan retention and rollback before doing so on a
populated environment. Migrations are run separately from API startup.

## Local acceptance evidence (2026-10-21)

- `uv run --offline ruff check .`: passed.
- `uv run --offline ruff format --check .`: passed.
- `uv run --offline mypy src tests scripts migrations`: passed.
- `uv run --offline python scripts/test_integration.py` with isolated Docker
  PostgreSQL and Redis: 104 passed. Tests cover credential expiry and replay,
  origin checks, hidden-ticket subscription denial, internal-comment isolation,
  revoked sessions, assignment and comment events, duplicate suppression,
  bounded slow-client disconnect, Redis outage/recovery, lease reclaim,
  migration from `0004_assignment`, and a two-Uvicorn-process/one-worker flow.
- Alembic `check` after upgrading an isolated `0004_assignment` database:
  passed with no model/schema drift.
- The separate-process smoke test delivered 20 sequential comment events to
  both replicas with local p95 **0.246 seconds**, measured from HTTP mutation
  start through receipt on both WebSockets. Stopping the worker left a committed
  ticket mutation readable through REST; restarting it delivered the pending
  event. This is a local smoke result, not a production capacity claim.
- `docker compose --env-file .env.compose config --quiet` and
  `docker compose --env-file .env.compose build api worker`: passed.

The test client emits a Starlette warning about its HTTPX adapter. Hosted CI
and deployment smoke checks were not run. Worker lag metrics and release load
testing remain part of milestone 7.
