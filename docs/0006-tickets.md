# Ticket vertical slice

Milestone 5 now publishes the outbox events described here; see
[realtime operations](0008-realtime.md).

Milestone 3 adds `tickets`, `ticket_comments`, `ticket_audit`, and `outbox_events`
in reviewed revision `0003_tickets`. PostgreSQL owns ticket state. Services own
transactions and lock the ticket row before comment or status writes. Each write
increments the ticket version and inserts one audit row and one pending outbox
event in the same transaction. The event payload is empty: it is an invalidation
hint, not a copy of ticket or comment content. A future dispatcher will publish
after commit. Audit entries are not exposed through requester routes.

## API rules

All endpoints require a valid bearer token. `POST /api/v1/tickets` takes `title`
(1–200 characters) and `description` (1–10,000). `GET /api/v1/tickets` accepts
`status`, `limit` (1–100, default 20), and an opaque `cursor`; `GET
/api/v1/tickets/{id}` returns one ticket. Requesters see only their own tickets
in both routes. Agents and admins see the shared queue. Page order is newest
first by creation time and ID. `next_cursor` is null on the last page.

`GET /api/v1/tickets/{id}/comments` has `limit` and `cursor` with the same bounds;
comments are oldest first. Requesters see public comments only. `POST
/api/v1/tickets/{id}/comments` takes `body` (1–10,000), `internal` (default
false), and `expected_version`. Requesters cannot create internal comments;
agents and admins can. Comments on a hidden ticket return 404.

`POST /api/v1/tickets/{id}/transitions` takes `status` and `expected_version`.
Allowed edges are `open → in_progress → resolved → closed`, `resolved →
in_progress`, and `closed → open`. Admins can use every edge. Requesters can
reopen their own ticket from resolved or closed. Agents need an assignment to
transition. Assignment endpoints are documented in
[assignment rules](0007-assignment.md). Reopening clears the resolution and
closure timestamps. A stale version or invalid edge returns 409;
a forbidden action returns 403. Repeating a successful write with the same
version returns 409. Clients should refetch before retrying.

The API returns 401 for missing credentials, 404 for missing or invisible tickets,
403 for an action forbidden on a visible ticket, 413 for a body over 16 KiB, and
422 for malformed input or cursor. Validation errors and dependency exceptions
do not include ticket text or credentials. Example successful create response:

```json
{"id":"a4e7c3d2-9118-4421-970d-35b488288042","requester_id":"39dca74d-458b-44ea-a3cf-f22455e4d250","assignee_id":null,"title":"Printer offline","description":"Floor 2 printer is offline","status":"open","version":1,"created_at":"2026-10-21T10:00:00Z","updated_at":"2026-10-21T10:00:00Z","opened_at":"2026-10-21T10:00:00Z","escalated_at":null,"resolved_at":null,"closed_at":null}
```

Example conflict: a comment with `expected_version: 1` after another mutation
advanced the ticket to version 2 returns HTTP 409 with
`{"detail":"Ticket version or transition conflict"}`. Refetch the ticket and
resubmit with the current version if the comment is still relevant.

## Operations and verification

Apply `uv run --locked alembic upgrade head` before serving the new code. The
migration adds tables and indexes; it does not alter or remove existing data.
Its downgrade deletes ticket, comment, audit, and outbox data and must not be
used on a populated environment without an explicit retention decision. The
requester and status indexes support queue filters; comment and audit indexes
support per-ticket reads and ordered history. Outbox publication runs in the
worker; see [realtime behavior](0008-realtime.md).

Run `sh scripts/check.sh` and the isolated PostgreSQL suite via
`uv run --locked python scripts/test_integration.py` after starting the test
services as documented in README. The suite covers authorization, internal
comment isolation, version and transition conflicts, stable ticket pagination,
and atomic audit/outbox effects, including a forced database failure.

## Local acceptance evidence (2026-10-21)

- `uv run --offline ruff check .`: passed.
- `uv run --offline ruff format --check .`: passed.
- `uv run --offline mypy src tests scripts migrations`: passed, 41 source files.
- `uv run --offline pytest`: 77 passed, 16 integration cases skipped without explicit service URLs.
- `uv run --offline python scripts/test_integration.py` with isolated Docker test
  PostgreSQL and Redis: 93 passed. This includes fresh and `0002_auth` upgrade
  migrations, HTTP requester/agent/admin flows, comment visibility and
  pagination, conflict handling, and forced audit constraint failure with zero
  committed ticket/comment/audit/outbox records.

The test client emits a Starlette deprecation warning about its HTTPX adapter.
Hosted CI and deployment smoke checks were not run at this milestone. See
[assignment rules](0007-assignment.md), [realtime behavior](0008-realtime.md),
and [scheduling rules](0009-scheduling.md) for the later features.
