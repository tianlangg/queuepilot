# QueuePilot working conventions

## Scope and workflow

- Read `PLAN.md`, relevant code, and the current Git diff before changes. The repository is currently in a planning phase: do not implement the application until the user requests implementation.
- Follow the current user request and deliver the relevant milestone as a small, reviewable change. Do not commit, push, deploy, or contact others unless authorized by the user.
- Preserve unrelated work. Never reset or delete user changes to make checks pass. Report blockers and verification limits honestly.
- Do not spawn sub-agents unless the user explicitly requests delegation or parallel agent work.
- Update documentation when behavior, environment variables, schema, commands, or operational expectations change. Record major architectural decisions in `docs/` once implementation begins.

## Python and architecture

- Use the `src/queuepilot` layout in `PLAN.md`. Pin a supported Python version and dependencies in `pyproject.toml` and `uv.lock` when scaffolding begins; use `uv` for repeatable environments.
- Use type annotations on public functions and explicit Pydantic input/output models. Apply Ruff formatting/linting and mypy with a checked-in configuration. Avoid broad ignores and unexplained `Any`.
- Keep FastAPI routes thin. Services enforce permissions, lifecycle rules, and transaction boundaries; models describe persistence. Do not expose ORM objects directly as public responses.
- Use asynchronous database/Redis clients in async handlers. Avoid blocking I/O in the event loop and external calls inside database transactions.
- Use UTC-aware timestamps and an injectable clock. Keep configuration in validated settings; document required values in `.env.example` with safe placeholders.
- Prefer straightforward feature modules over generic frameworks. Add dependencies only when they solve a concrete requirement.

## Persistence and concurrency

- PostgreSQL is authoritative. Use Alembic migrations for schema changes, database constraints for invariants, and indexes justified by access patterns. Review generated migrations before use.
- Every assignment writer must acquire the ticket row with `SELECT ... FOR UPDATE`, validate eligibility within that transaction, and write assignment/audit/outbox changes atomically. Define a consistent lock order for multi-row workflows.
- Use one explicit transaction owner per operation; helpers must not silently commit. Never share a SQLAlchemy session across concurrent tasks.
- Persist domain changes and outbox events in the same transaction. Publish only after commit through the dispatcher. Redis failure must not roll back a valid ticket mutation.
- Design worker actions for retries: bounded claims, leases, backoff, unique effect keys, and state rechecks under lock. Do not equate a Redis lock with transactional database correctness.
- Increment ticket versions on mutations and preserve audit history. Document API conflict/idempotency semantics. Never claim exactly-once notification delivery.

## Security and API behavior

- Check resource authorization in services and query filters, not merely in route role decorators. Use explicit response fields; keep internal comments and private events inaccessible to requesters.
- Hash passwords with a maintained password-hashing library. Validate signed token algorithms, issuer/audience, and expiry explicitly. Rotate/revoke refresh sessions. Never hard-code bootstrap credentials.
- Do not log passwords, tokens, connection credentials, or sensitive ticket content. Never commit `.env`, keys, database dumps, or production data.
- Authenticate WebSockets, authorize every subscription, validate origins, and enforce expiration/revocation behavior. Use short-lived single-use connection credentials; bound queues and clean up disconnected clients.
- Provide bounded pagination, request limits, predictable status codes, and sanitized error responses. Keep API contracts and permission matrices documented.

## Testing and verification

- Establish pytest, HTTPX, Ruff, and mypy in milestone 1. Document exact working commands in README and CI; do not present planned commands as already passing.
- Run checks appropriate to the change. Default implementation gates, once configured, are `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, and `uv run pytest`. State which checks ran and their results.
- Use real isolated PostgreSQL databases for integration, migration, assignment, and concurrency tests. Use real Redis for Pub/Sub integration. Never run destructive tests against a development or production database; require explicit test database configuration and a safety guard.
- Race tests need independent connections, synchronized contenders, bounded timeouts, and assertions on committed state and audit/outbox effects. Mocks and SQLite cannot verify PostgreSQL locking.
- Test permission denials, cross-user reads, internal-comment isolation, transition conflicts, rollbacks, duplicate delivery, stale scheduled jobs, worker crashes, reconnects, and dependency outages as the relevant features land.
- Inject time into scheduling tests; avoid arbitrary sleeps. Use bounded polling only for genuine cross-process eventual behavior. Keep tests deterministic and ensure resources are cleaned up.
- Test migrations from empty and prior schemas. Do not automatically downgrade destructive migrations; document recovery and retention implications.
- Do not add tests that only repeat implementation details. For documentation-only changes, verify content, links/paths, scope, and diff; application test runs are unnecessary.

## Completion report

Summarize the resulting behavior, meaningful files changed, checks performed, and material limitations. Distinguish implemented functionality from proposals. Do not mark a milestone complete without its acceptance evidence.
