# QueuePilot implementation plan

Status: milestones 1–3 implemented and locally accepted. Milestone 3 ticket HTTP evidence is in [ticket design and acceptance](docs/0006-tickets.md). Hosted CI has not been triggered; later milestones remain planned.

## Scope and assumptions

Build an API-first, single-organization helpdesk with Python, FastAPI, PostgreSQL, Redis, and WebSockets. Initial roles are requester, agent, and administrator. A browser UI, multi-tenancy, email ingestion, attachments, and external integrations are outside the initial scope. Validate these assumptions before implementation if requirements change.

Deliver working vertical slices rather than building every abstraction first. Each milestone ends with runnable checks and explicit acceptance criteria. Estimates below are engineering days for one developer, excluding review and deployment infrastructure delays; total roughly 23–35 days. Reassess after the first vertical slice.

## Architecture

- **API service:** FastAPI exposes versioned REST endpoints and authenticated WebSocket connections. Routes validate input and call application services; services enforce authorization and transactional workflows. Use asynchronous SQLAlchemy with psycopg and Alembic migrations.
- **PostgreSQL:** authoritative storage for users, tickets, comments, assignments, audit history, scheduled jobs, and an event outbox. Transactions commit domain changes and their outbox events together. Redis availability must not determine whether a ticket mutation succeeds.
- **Redis:** Pub/Sub distributes live events across API replicas. Redis is ephemeral infrastructure, not the ticket database or durable job queue. Readiness and operational monitoring expose degraded notification delivery.
- **Worker:** a separate Python process claims durable outbox records and scheduled jobs from PostgreSQL in bounded batches. Use row locks and leases to coordinate multiple workers. Publish events to Redis and retry failures with backoff. A crash after publishing but before recording success can produce duplicates; consumers deduplicate using event IDs and ticket versions.
- **WebSockets:** authenticate at connection time, authorize subscriptions, and route events only to permitted viewers. Do not expose a global event feed. Redis Pub/Sub is not replayable: clients refetch authoritative REST state after reconnects, version gaps, or a resync signal. REST reads remain available during Redis outages.
- **Security:** hashed passwords, short-lived signed access tokens, revocable rotating refresh sessions, and resource-level permission checks. Browser WebSocket clients use a short-lived, single-use connection credential obtained through authenticated REST; never put long-lived access tokens in URLs. Redact credentials from logs and enforce origin restrictions.
- **Operations:** separate API and worker processes, structured logs and correlation IDs, health probes, metrics, migration jobs, containerized local dependencies, and graceful shutdown. Establish supported versions and pin dependencies during implementation, rather than guessing versions in this plan.

### Domain and consistency rules

- Requesters create and read their own tickets and add public comments. Agents read the shared queue, claim tickets, and manage tickets assigned to them. Administrators manage users, reassign tickets, and configure scheduling rules. Agents and administrators may add internal comments; requesters never receive their content or events.
- Start with `open`, `in_progress`, `resolved`, and `closed`. A transition table defines permitted transitions and actors. Claiming an open ticket moves it to `in_progress`. Reopening clears resolution timestamps and recomputes relevant deadlines.
- Every ticket mutation increments a version and writes audit history. API event envelopes include event ID, ticket ID, ticket version, event type, timestamp, and a minimal authorized payload. Do not assume global event ordering.
- Claiming a specific ticket uses `SELECT ... FOR UPDATE` inside one transaction, then checks assignment eligibility before updating. Two agents racing for the same ticket produce exactly one success and one conflict. Manual reassignment uses the same lock and requires administrator permission. All assignment writers must follow this protocol.
- An optional “claim next” operation uses an explicitly ordered eligible query with `FOR UPDATE SKIP LOCKED`; it is not required for the first release. Worker batch claims use `SKIP LOCKED` independently of ticket claiming.
- First scheduling use cases: unassigned-ticket escalation and automatic closure after a configurable resolved period. Store UTC timestamps; deadlines live in PostgreSQL. Define policy defaults and clock behavior before milestone 6. Jobs recheck current ticket state under lock and use a unique effect key so retries cannot repeat a business action.
- No promise of exactly-once delivery: database effects are idempotent and notifications are at least once when delivered through the outbox. Clients must tolerate duplicate or missed live notifications. Audit records provide durable history.

## Proposed folder structure

These paths are a blueprint, not folders to scaffold in the planning phase.

```text
queuepilot/
├── PLAN.md
├── AGENTS.md
├── README.md
├── pyproject.toml
├── uv.lock
├── .env.example
├── compose.yaml
├── Dockerfile
├── alembic.ini
├── migrations/                 # Reviewed schema revisions
├── src/queuepilot/
│   ├── main.py                 # App factory and lifespan
│   ├── core/                   # Settings, security, logging, clock
│   ├── db/                     # Engine, session, model metadata
│   ├── api/                    # Versioned router and common dependencies
│   ├── auth/                   # Users, sessions, permissions, auth routes
│   ├── tickets/                # Models, schemas, routes, transactional services
│   ├── realtime/               # WS connections, subscriptions, Redis listener
│   ├── outbox/                 # Durable events and delivery processing
│   └── jobs/                   # Worker entry point, claims, scheduling policies
├── tests/
│   ├── unit/
│   ├── integration/            # Real PostgreSQL and Redis
│   ├── concurrency/            # Independent database sessions and race barriers
│   └── e2e/                    # API, WebSockets, worker across processes
├── scripts/                   # Repeatable development/verification commands
└── docs/                      # API examples, decisions, deployment/runbooks
```

Keep models, request/response schemas, and service logic close to their feature. Shared infrastructure belongs in `core` or `db`; avoid a generic repository framework until an actual need emerges. Request handlers own transport concerns; services own transaction boundaries. Workers call the same domain operations as HTTP routes where semantics match.

## Milestones

### 1. Reproducible foundation (2–3 days)

Stage 1 (Python foundation) is implemented and locally verified; see
[acceptance evidence](docs/0001-python-foundation.md#stage-1-acceptance-evidence).
The full foundation is implemented and locally accepted; see the
[service foundation decisions](docs/0002-service-foundation.md) and
[Docker acceptance evidence](docs/0003-milestone1-acceptance.md). The documented
quality/test gates also passed inside Linux against Docker test services. Hosted
GitHub Actions has not been triggered; no commit or push was authorized.

Deliver packaging with a lockfile, typed settings, app factory, dependency containers for PostgreSQL and Redis, database sessions, initial migration, liveness/readiness, and CI lint/type/test commands. Document one-command local startup and environment setup without embedding secrets.

Tests: app startup/shutdown, missing configuration, database readiness failure, migration of an empty database, and smoke requests.

Acceptance: a fresh checkout can install dependencies, start services, migrate, and pass CI using documented commands. Liveness does not depend on external services; readiness reports database failure and distinguishes Redis degradation.

### 2. Authentication and authorization (3–5 days)

Implemented and locally accepted; see [design and permission matrix](docs/0004-authentication.md)
and [acceptance evidence](docs/0005-milestone2-acceptance.md). Hosted CI remains
unverified. Ticket policy entries are defined but ticket endpoints arrive later.

Deliver user persistence, password hashing, login, refresh rotation, logout/revocation, role checks, administrator bootstrap, and the explicit resource permission matrix. No public role escalation or unrestricted administrator registration.

Tests: valid/invalid login, expired/invalid tokens, refresh replay and revocation, disabled users, role escalation attempts, and cross-user access denial. Include cookie/CSRF checks if cookies carry credentials; document token storage assumptions.

Acceptance: all protected routes reject unauthenticated callers; each permission matrix cell has a test. Secrets and passwords never appear in responses or logs. Bootstrap is explicit and repeatable without hard-coded credentials.

### 3. Ticket vertical slice (3–4 days)

Implemented and locally accepted; see [ticket design and acceptance evidence](docs/0006-tickets.md). Hosted CI remains unverified.

Deliver ticket creation, authorized detail/list endpoints, bounded pagination and filtering, public/internal comments, lifecycle transitions, ticket versions, indexes, audit history, and transactional outbox insertion. Establish request-size limits and consistent error responses.

Tests: creation/read/update flow, ownership checks, internal-comment isolation, transition matrix, validation boundaries, stable pagination, rollback, and atomic audit/outbox persistence.

Acceptance: requester and agent flows work through HTTP against PostgreSQL; failed writes leave no partial audit or outbox records. List and detail endpoints enforce identical visibility rules. API examples document success and conflict cases.

### 4. Atomic assignment (2–3 days)

Deliver locked claim and administrator reassignment endpoints, assignment history, deterministic conflict responses, bounded lock/statement timeouts, and a documented transaction retry policy. Keep transactions short and never call Redis while holding ticket locks.

Tests: synchronize two independent sessions/processes claiming one ticket; assert one winner and one HTTP 409, one assignment effect, and matching audit/outbox records. Repeat races, test different-ticket concurrency, rollback/lock release, reassignment versus claim, and timeout handling.

Acceptance: repeated real PostgreSQL races never overwrite a competing claim or create duplicate effects. A second claim by the current assignee has a documented idempotent response. Deadlocks/timeouts are bounded and produce controlled responses.

### 5. Real-time updates and reliable event publication (4–6 days)

Deliver outbox dispatcher, Redis fan-out, authenticated subscriptions, connection-credential issuance, heartbeats, payload validation, bounded per-connection queues, slow-client disconnect/resync behavior, and connection cleanup. Define reauthorization on role/session changes and credential expiry. Start with minimal invalidation events so REST applies current visibility rules.

Tests: ticket creation/assignment/comment events, cross-replica delivery, prohibited subscriptions, internal-comment leakage, revoked sessions, disallowed origins, single-use credential replay, reconnect/refetch, duplicate events, worker crashes, Redis outage/recovery, and slow clients.

Acceptance: two API replicas receive updates through Redis; unauthorized clients receive no restricted content. A committed ticket survives a Redis outage and its pending event is retried after recovery. REST refetch restores correct state even if a live message is missed. Healthy-path local event delivery target: p95 below 1 second under the documented smoke load.

### 6. Automated scheduling (3–5 days)

Deliver durable due jobs, concurrent claims and leases, retries/backoff, failed-job inspection, escalation and auto-close policies, cancellation/rescheduling on state changes, and graceful worker shutdown. Record policy changes and automation actors in audit history.

Tests: injected-clock deadline boundaries, UTC handling, two workers claiming one due action, crash/lease recovery, retry exhaustion, stale jobs after reopening/reassignment, concurrent human edits, and duplicate execution attempts.

Acceptance: automated actions run within 60 seconds of their due time under documented normal load. Worker restarts do not lose jobs; repeated attempts produce one business effect. Stale jobs cannot overwrite newer user actions, and operators can inspect and safely retry failed work.

### 7. Release hardening and operational handoff (6–9 days)

Deliver full-stack smoke scenarios, load/concurrency harness, CI integration services, secure runtime configuration, rate/connection limits, graceful shutdown, outbox/job lag metrics, deployment and rollback instructions, database backup/restore rehearsal, and migration compatibility guidance.

Tests: complete requester→agent→automation flow, role changes during active WebSocket sessions, API/worker restarts, PostgreSQL/Redis interruptions, clean-install and upgrade migrations, and backup restoration. Test CORS/origin rules and sanitized error handling.

Acceptance: all previous gates pass in CI and documented smoke tests pass on a staging deployment. On a recorded reference environment with 10,000 seeded tickets, 50 concurrent HTTP clients, and 100 WebSockets, target p95 below 300 ms for list/detail and below 500 ms for mutations, with no unexpected 5xx responses over a 10-minute run. Record actual results and bottlenecks; do not claim production capacity from this baseline. Restore rehearsal recovers tickets, jobs, and audit history; deployment requires no repository secrets.

## Verification and release gates

- Unit tests cover pure permissions, transitions, retry policy, and deadline calculation with an injected clock; avoid sleep-based timing assertions.
- Integration tests use real PostgreSQL for transactions, constraints, migrations, and row locks. SQLite is not an acceptable substitute. Redis integration tests exercise actual fan-out and disconnection behavior.
- Concurrency tests use separate connections and deterministic synchronization, bounded waits, and repeated runs. A serial sequence is not a race test.
- E2E tests run API replicas and worker independently so process-local shortcuts cannot pass unnoticed.
- Migrations are reviewed SQL changes, tested from empty and prior schemas. Destructive migrations need an explicit data-retention and rollout decision.
- Every milestone updates README instructions and records its commands, results, unresolved issues, and acceptance evidence. Completion requires passing checks or an explicit documented deviation accepted by the project owner.

## Decisions to confirm before expanding scope

Initial assumptions above allow implementation to begin once requested. Confirm deployment target and capacity expectations before release hardening; confirm SLA/business-hour calendars before expanding escalation policies. Multi-tenancy, attachment storage, email delivery, and a user interface require separate scope and authorization. Milestones 1–3 have been authorized. Later milestones, deployment, commits, and pushes require separate authorization.
