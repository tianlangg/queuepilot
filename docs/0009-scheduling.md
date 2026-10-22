# Milestone 6: automated scheduling

## Rules and API

The database policy starts at 60 elapsed minutes for escalation and 10,080
elapsed minutes (seven days) for auto-close. The policy uses UTC instants, not
business hours. Both values are integers from 1 through 525,600 minutes.
`GET /api/v1/scheduling/policy` returns both values and a version;
`PATCH /api/v1/scheduling/policy` requires both values plus `expected_version`.
Only administrators may read or change policy. A stale version returns 409.
Each change writes `scheduling_policy_audit` with the administrator, old/new
values, timestamp, and policy version.

An unassigned `open` ticket gets an escalation job from `opened_at`. Escalation
sets `escalated_at`, increments the ticket version, and writes a ticket audit
entry and outbox invalidation. A resolved ticket gets an auto-close job from
`resolved_at`. Closing retains the resolution timestamp and writes the same
atomic effects. Assignment or status change cancels incompatible jobs. Reopening
sets a new `opened_at` and clears the escalation marker. Comments do not reset
the clock. Automation audit entries have `actor_kind=automation` and no user ID.
The ticket response exposes `opened_at` and `escalated_at`.

Each job has a unique effect key based on its kind, ticket, and state-entry
version. The worker claims at most 10 due jobs per cycle with `FOR UPDATE SKIP
LOCKED` and a 30-second lease. It then locks the ticket, reads current policy,
and locks the job, checking the lease token, ticket state, and state-entry time.
Ticket effects, audit, outbox, and job completion commit together. A lost lease
or duplicate execution attempt cannot repeat a completed effect. A stale job is
cancelled; a changed deadline is rescheduled. Ticket writers lock the ticket
before the policy and job rows. The policy update locks policy before updating
pending jobs; it does not lock tickets. Calls to Redis happen after commit.

Execution failures retry after 1, 2, 4, and 8 seconds, then become `failed` on
the fifth failed attempt. The operator can list jobs with
`GET /api/v1/scheduling/jobs?state=failed&limit=20` (limit 1–100), inspect the
sanitized failure code, and call `POST /api/v1/scheduling/jobs/{id}/retry`.
Job lists sort by UUID; pass the last returned ID as `after` to fetch the next
page.
Retry resets attempts and makes the job immediately claimable; execution still
rechecks ticket and policy. A retry of a nonfailed job returns 409. The worker
uses the existing graceful signal shutdown path, so committed claims survive a
restart and become claimable when their leases expire. With the 0.2-second idle
poll, normal local load is designed to act within 60 seconds of due time;
the target is not an SLA under database outage or sustained backlog.

## Migration and recovery

Revision `0006_scheduling` backfills `opened_at` from `created_at`, creates the
default policy, and schedules existing unassigned open and resolved tickets.
Past-due backfilled jobs run after the worker starts. The migration makes ticket
audit actor IDs nullable with a constraint distinguishing users from automation.
It has no automatic downgrade because dropping jobs and policy audit would lose
history. Restore a database backup to recover from a bad migration. Keep the
worker stopped during a migration and start it after the new schema is ready.

## Local acceptance evidence

The isolated PostgreSQL scheduling tests exercise an exact deadline boundary,
UTC timestamps, competing claims, expired lease recovery, duplicate execution,
retry exhaustion and operator retry, policy rescheduling, stale jobs after human
edits, auto-close, and automation audit. An integrated worker smoke check
processed a past-due job within five seconds. The Milestone 5 to 6 migration
was tested with an existing ticket, and the empty-schema migration passed.
Local `ruff check`, `ruff format --check`, `mypy src tests scripts migrations`,
and the isolated PostgreSQL/Redis suite passed (111 tests). Hosted CI and a
sustained worker-lag load test have not been run.
