# Atomic assignment

Milestone 5 now publishes the outbox events described here; see
[realtime operations](0008-realtime.md).

Milestone 4 adds `POST /api/v1/tickets/{id}/claim` and
`POST /api/v1/tickets/{id}/reassign`. Both take `expected_version`; reassignment
also takes an `assignee_id`. They return the standard ticket response. All
assignment writers lock the ticket row with `SELECT ... FOR UPDATE`, check state
and version inside the transaction, then write the ticket, one assignment history
row, one audit entry, and one outbox invalidation event together. No Redis call
occurs inside the transaction. The row lock is acquired before the target user
lock, which checks the agent is active. Future workflows that lock both must
follow this order.

An agent or admin can claim an unassigned `open` ticket. It becomes
`in_progress`. Repeating a claim by the current assignee while the ticket is
`in_progress` returns 200 and the current ticket, even if the caller sends the
original version; it creates no new history or event. A competing claimant gets
409. Admins can reassign an `open` or `in_progress` ticket to an active agent.
An `open` ticket becomes `in_progress` on assignment. Reassigning to the current
assignee with the current version returns 200 without a mutation. Resolved and
closed tickets cannot be assigned. Requesters cannot claim or reassign.

Missing or invisible tickets return 404. A visible ticket with a forbidden
action returns 403. Stale versions, ineligible ticket state, and competing
assignments return 409. An invalid, disabled, or non-agent reassignment target
returns 422. PostgreSQL lock timeout (`55P03`), statement timeout (`57014`),
deadlock (`40P01`), and serialization failure (`40001`) produce sanitized 503
responses with `Retry-After: 1`. The database pool bounds lock waits to three
seconds and statements to five seconds. Other database failures propagate to
the server error handler; they are not mistaken for conflicts.

The service does not automatically retry a transaction because the next attempt
must recheck the ticket version and the caller's intent. A client receiving 409
or 503 should refetch the ticket, decide whether the assignment is still wanted,
and make a bounded retry with the latest version. A successful claim can safely
be repeated by its current assignee. History, audit, and outbox records are
committed together; milestone 5 publishes pending outbox events.

Revision `0004_assignment` adds the `ticket_assignments` table and a unique
`(ticket_id, ticket_version)` index. It retains previous and new assignee IDs,
actor, version, and UTC timestamp. The migration adds no destructive columns to
existing tables. Downgrading drops assignment history and should not be run on
a populated environment without a retention decision. Apply
`uv run --locked alembic upgrade head` before serving the new code.

## Verification

Run `sh scripts/check.sh`, then the isolated PostgreSQL suite using
`uv run --locked python scripts/test_integration.py` with the test services
described in README. The assignment tests repeat independent HTTP claim races,
check claim versus reassignment, verify persistent effects, and hold a ticket
row lock to verify a controlled timeout and rollback. Hosted CI and deployment
smoke checks remain unverified.

## Local acceptance evidence (2026-10-21)

- `uv run --offline ruff check .`: passed.
- `uv run --offline ruff format --check .`: passed.
- `uv run --offline mypy src tests scripts migrations`: passed, 43 source files.
- `uv run --offline python scripts/test_integration.py` with isolated Docker
  PostgreSQL and Redis: 96 passed. This includes repeated competing HTTP claims,
  claim versus reassignment, same-assignee idempotence, assignment history and
  audit/outbox counts, forced rollback, lock timeout, different-ticket progress,
  and migration from the previous ticket schema.

The test client emits a Starlette warning about its HTTPX adapter. Hosted CI and
deployment checks were not run. Event dispatch remains a later milestone.
