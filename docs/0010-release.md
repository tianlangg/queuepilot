# Milestone 7: release and operational handoff

## Deployment configuration

Run the API and worker as separate processes against the same migrated
PostgreSQL database and Redis endpoint. Keep the migration job separate from
application startup. Provide all `QUEUEPILOT_*` values through a secret manager
or protected runtime environment; never bake them into an image. Production
settings reject localhost browser origins and known example token secrets.
Use a unique signing secret of at least 32 characters, TLS at the ingress,
private network access to PostgreSQL/Redis, and a `rediss://` URL when Redis
crosses an untrusted network. The Compose file is a local reference only.
Its API/worker containers run as a non-root user with a read-only root
filesystem, temporary `/tmp`, no Linux capabilities, and no-new-privileges.

Set `QUEUEPILOT_WEBSOCKET_ORIGINS` to exact HTTPS browser origins. The same list
controls REST CORS preflight. Browser credentials use bearer headers, not
cookies; CORS credentials are disabled. The API applies per-process, per-client
IP limits: 30 login/refresh/connection-credential requests, 300 API reads, and
120 other writes per minute by default. Override these with
`QUEUEPILOT_AUTH_REQUESTS_PER_MINUTE` and
`QUEUEPILOT_READ_REQUESTS_PER_MINUTE` and
`QUEUEPILOT_WRITE_REQUESTS_PER_MINUTE` after measuring the deployment. The
WebSocket admission cap is 500 per API process by default, configurable with
`QUEUEPILOT_WEBSOCKET_CONNECTION_LIMIT`. Put a fleet-wide rate and connection
limit at the trusted ingress for multiple replicas. Do not trust unvalidated
`X-Forwarded-For`; the container disables proxy-header rewriting and the
application uses the socket peer address. `X-Request-ID`
is returned on HTTP responses; a supplied ID is accepted only if it is at most
64 alphanumeric, underscore, or hyphen characters.

## Deploy, observe, and recover

1. Back up the current database using a consistent PostgreSQL custom-format
   dump. Verify the backup can be restored into a separate database before a
   release. Include the `alembic_version`, tickets, audit, outbox, jobs, and
   scheduling policy tables. Redis Pub/Sub is ephemeral and is not a backup
   source.
2. Stop or drain the old worker and apply `alembic upgrade head` once. This
   revision backfills jobs, so start the new worker only after the migration
   completes. Start API replicas with the same schema revision and token
   configuration. Use `GET /health/live` for process liveness and
   `GET /health/ready` for database/Redis health.
3. Use administrator `GET /api/v1/operations/metrics` to inspect pending outbox
   events, oldest outbox age, pending/due/failed jobs, and oldest due-job age.
   The WebSocket count is local to the API replica serving the request; scrape
   or query each replica to see total connections. Alert on growing lag and
   failed jobs. `GET /api/v1/scheduling/jobs?state=failed` and its `after`
   cursor allow inspection; retry only after fixing the underlying failure.
4. Stop the API and worker gracefully before restoring. Restore the verified
   dump into a new database, point the deployment at that database, and check
   the recorded schema revision before startup. A prior application image may
   run against a newer schema only when explicitly tested for that migration.
   Revision `0006_scheduling` has no automatic downgrade because it would
   discard jobs and policy audit history. Restore the pre-upgrade backup if
   schema rollback is required. Reconcile any ticket changes made after the
   backup with the incident owner before switching traffic.

The local rehearsal command below creates two uniquely named databases only
on the dedicated `queuepilot_test` service at localhost port 55433. It migrates
and seeds ticket/audit/job rows, takes a `pg_dump -Fc`, runs `pg_restore`, verifies
the schema and rows, then drops both temporary databases. It refuses any other
database target and requires explicit confirmation:

```sh
docker compose --env-file .env.compose --profile test up -d --wait postgres-test redis-test
uv run --locked python scripts/rehearse_backup.py --confirm-isolated-test-server
```

## Reference load

The disposable load harness creates a fresh test database, migrates it, seeds
tickets and audit rows, launches separate API and worker processes, connects
WebSockets, and measures list/detail/comment request latency. It prints JSON
with sample counts, p95, failure counts, Pub/Sub observations, and queue lag,
then stops processes and drops the database. It accepts only the dedicated
local test server on port 55433. It temporarily raises per-process rate limits
inside its disposable API processes so admission control does not mask service
latency. It also sets a 60-minute access-token lifetime so the single
benchmark session remains valid through setup and the ten-minute run:

```sh
uv run --locked python scripts/load_harness.py \
  --confirm-isolated-test-server --seed-count 10000 --clients 50 \
  --websockets 100 --duration 600 --think-ms 1000 --api-workers 4
```

The one-second staggered think time models 50 concurrent clients making
repeated list, detail, and comment requests. The harness raises an error if
any request fails, list/detail p95 is at least 300 ms, or mutation p95 is at
least 500 ms. It is a local reference workload, not a production capacity
claim. Results depend strongly on request burst shape: in 30-second checks on
the local Docker test server, a one-process 100 ms think-time run reached
1,022/823/921 ms p95 (list/detail/mutation), while four workers with a
staggered one-second think time reached 47/34/44 ms with no HTTP failures.
The 2026-10-22 local ten-minute reference run used four API workers, 10,000
seeded tickets, 50 HTTP clients, 100 WebSockets, and one-second staggered think
time. It completed 27,594 samples of each HTTP operation, with p95 of 89.29 ms
for list, 61.98 ms for detail, and 69.05 ms for comments. There were no HTTP
failures or early WebSocket closures. The readers received 55,088 ticket
events and 100 initial resync signals; the initial signals reflect subscriptions
starting at version zero against tickets seeded at version one. Final queue
metrics showed zero pending outbox events and zero due or failed jobs. The
reported 44 WebSocket connections were on the single API worker that served
the metrics request; 100 connections were admitted across four workers.

## Acceptance evidence

The local isolated suite passed on 2026-10-22: 115 tests using real PostgreSQL
and Redis, including the two-process flow below. Ruff lint/format, mypy over
source/tests/scripts/migrations, Compose configuration, and API/worker image
builds also passed. The integration suite runs two independent API processes
and a worker, covers cross-replica Redis delivery, worker restart,
requester-to-agent claim,
resolution, auto-close, and API restart/refetch. Other integration tests cover
PostgreSQL assignment races, isolated migration upgrades, Redis outage/recovery,
authorization, and durable job leases. Unit tests cover CORS, sanitized
validation, production settings, HTTP admission limits, and WebSocket capacity.
The backup restore rehearsal passed locally with ticket, audit, job, and schema
verification. Hosted GitHub Actions and a staging deployment have not run from
this workspace. The milestone's hosted CI and staging gates remain open.
