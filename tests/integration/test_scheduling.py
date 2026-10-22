"""Durable deadlines and lease behavior against isolated PostgreSQL."""

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from queuepilot.auth.bootstrap import bootstrap
from queuepilot.auth.models import User
from queuepilot.auth.service import Principal
from queuepilot.core.config import Settings
from queuepilot.db.session import create_engine
from queuepilot.jobs.models import PolicyAudit, ScheduledJob
from queuepilot.jobs.service import PolicyUpdate, change_policy, list_jobs, retry_job
from queuepilot.jobs.worker import claim_batch, execute, fail
from queuepilot.main import create_app
from queuepilot.outbox.dispatcher import run_worker
from queuepilot.tickets.models import Audit, Ticket
from queuepilot.tickets.schemas import TicketCreate
from queuepilot.tickets.service import create_ticket, transition

from .conftest import migrate

pytestmark = pytest.mark.integration


def test_upgrade_from_realtime_backfills_jobs(empty_database: Settings) -> None:
    migrate(empty_database, "0005_realtime")
    assert asyncio.run(
        bootstrap("admin@example.com", "correct-horse-battery", empty_database)
    )
    url = make_url(empty_database.database_url.get_secret_value()).set(
        drivername="postgresql"
    )
    ticket_id = uuid.uuid4()
    created = datetime(2026, 1, 1, tzinfo=UTC)
    with psycopg.connect(url.render_as_string(hide_password=False)) as connection:
        user_id = connection.execute(
            "SELECT id FROM users WHERE email = %s", ("admin@example.com",)
        ).fetchone()
        assert user_id is not None
        connection.execute(
            """INSERT INTO tickets
            (id, requester_id, title, description,
             status, version, created_at, updated_at)
            VALUES (%s, %s, 'Old ticket', 'Existing data', 'open', 3, %s, %s)""",
            (ticket_id, user_id[0], created, created),
        )
        connection.commit()
    migrate(empty_database)
    with psycopg.connect(url.render_as_string(hide_password=False)) as connection:
        row = connection.execute(
            "SELECT opened_at FROM tickets WHERE id = %s", (ticket_id,)
        ).fetchone()
        assert row == (created,)
        job = connection.execute(
            "SELECT kind, due_at, state FROM scheduled_jobs WHERE ticket_id = %s",
            (ticket_id,),
        ).fetchone()
        assert job == ("escalation", created + timedelta(minutes=60), "pending")


def test_admin_policy_and_job_routes(migrated_database: Settings) -> None:
    assert asyncio.run(
        bootstrap("admin@example.com", "correct-horse-battery", migrated_database)
    )
    with TestClient(create_app(migrated_database)) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@example.com",
                "password": "correct-horse-battery",
            },
        )
        assert login.status_code == 200
        admin = {"Authorization": f"Bearer {login.json()['access_token']}"}
        created = client.post(
            "/api/v1/users",
            headers=admin,
            json={
                "email": "requester@example.com",
                "password": "correct-horse-battery",
                "role": "requester",
            },
        )
        assert created.status_code == 201
        requester_login = client.post(
            "/api/v1/auth/login",
            json={
                "email": "requester@example.com",
                "password": "correct-horse-battery",
            },
        )
        requester = {
            "Authorization": f"Bearer {requester_login.json()['access_token']}"
        }
        assert client.get("/api/v1/scheduling/policy").status_code == 401
        assert (
            client.get("/api/v1/scheduling/policy", headers=requester).status_code
            == 403
        )
        assert (
            client.get("/api/v1/scheduling/jobs", headers=requester).status_code == 403
        )
        assert (
            client.get("/api/v1/operations/metrics", headers=requester).status_code
            == 403
        )
        metrics = client.get("/api/v1/operations/metrics", headers=admin)
        assert metrics.status_code == 200
        assert metrics.json()["pending_jobs"] == 0
        policy = client.get("/api/v1/scheduling/policy", headers=admin)
        assert policy.status_code == 200
        assert policy.json() == {
            "escalation_minutes": 60,
            "auto_close_minutes": 10080,
            "version": 1,
        }
        changed = client.patch(
            "/api/v1/scheduling/policy",
            headers=admin,
            json={
                "escalation_minutes": 30,
                "auto_close_minutes": 90,
                "expected_version": 1,
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["version"] == 2
        assert (
            client.patch(
                "/api/v1/scheduling/policy",
                headers=admin,
                json={
                    "escalation_minutes": 30,
                    "auto_close_minutes": 90,
                    "expected_version": 1,
                },
            ).status_code
            == 409
        )
        assert client.get("/api/v1/scheduling/jobs", headers=admin).json() == []
        assert (
            client.get(
                "/api/v1/scheduling/jobs",
                headers=admin,
                params={"after": str(uuid.uuid4()), "limit": 1},
            ).json()
            == []
        )
        assert (
            client.post(
                f"/api/v1/scheduling/jobs/{uuid.uuid4()}/retry", headers=admin
            ).status_code
            == 404
        )


def test_escalation_claim_recovery_and_single_effect(
    migrated_database: Settings,
) -> None:
    assert asyncio.run(
        bootstrap("admin@example.com", "correct-horse-battery", migrated_database)
    )

    async def scenario() -> None:
        engine = create_engine(migrated_database)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        start = datetime(2026, 1, 1, 12, tzinfo=UTC)
        try:
            async with sessions() as session:
                admin = await session.scalar(
                    select(User).where(User.email == "admin@example.com")
                )
                assert admin is not None
                principal = Principal(admin.id, "admin")
            async with sessions() as session:
                ticket = await create_ticket(
                    session, principal, TicketCreate(title="A", description="B"), start
                )
            assert await claim_batch(sessions, now=start + timedelta(minutes=59)) == []
            first, second = await asyncio.gather(
                claim_batch(sessions, now=start + timedelta(minutes=60)),
                claim_batch(sessions, now=start + timedelta(minutes=60)),
            )
            claims = first + second
            assert len(claims) == 1
            assert (
                await claim_batch(
                    sessions, now=start + timedelta(minutes=60, seconds=29)
                )
                == []
            )
            recovered = await claim_batch(
                sessions, now=start + timedelta(minutes=60, seconds=31)
            )
            assert len(recovered) == 1
            assert not await execute(
                sessions, claims[0], now=start + timedelta(minutes=60, seconds=31)
            )
            assert await execute(
                sessions, recovered[0], now=start + timedelta(minutes=60, seconds=31)
            )
            assert not await execute(
                sessions, recovered[0], now=start + timedelta(minutes=60, seconds=31)
            )
            async with sessions() as session:
                row = await session.get(Ticket, ticket.id)
                assert (
                    row is not None
                    and row.escalated_at is not None
                    and row.version == 2
                )
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(Audit)
                        .where(Audit.ticket_id == ticket.id)
                    )
                    == 2
                )
                audit = await session.scalar(
                    select(Audit).where(
                        Audit.ticket_id == ticket.id, Audit.action == "escalated"
                    )
                )
                assert (
                    audit is not None
                    and audit.actor_id is None
                    and audit.actor_kind == "automation"
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_policy_reschedule_stale_closure_and_failed_retry(
    migrated_database: Settings,
) -> None:
    assert asyncio.run(
        bootstrap("admin@example.com", "correct-horse-battery", migrated_database)
    )

    async def scenario() -> None:
        engine = create_engine(migrated_database)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        start = datetime(2026, 2, 1, tzinfo=UTC)
        try:
            async with sessions() as session:
                admin = await session.scalar(
                    select(User).where(User.email == "admin@example.com")
                )
                assert admin is not None
                principal = Principal(admin.id, "admin")
            async with sessions() as session:
                ticket = await create_ticket(
                    session, principal, TicketCreate(title="A", description="B"), start
                )
            async with sessions() as session:
                policy = await change_policy(
                    session,
                    principal,
                    PolicyUpdate(
                        escalation_minutes=10, auto_close_minutes=20, expected_version=1
                    ),
                    start,
                )
                assert policy.version == 2
            assert await claim_batch(sessions, now=start + timedelta(minutes=9)) == []
            claims = await claim_batch(sessions, now=start + timedelta(minutes=10))
            assert len(claims) == 1
            # The human transition wins; the stale job cannot escalate.
            async with sessions() as session:
                progressed = await transition(
                    session,
                    principal,
                    ticket.id,
                    "in_progress",
                    1,
                    start + timedelta(minutes=10),
                )
            assert progressed.version == 2
            assert not await execute(
                sessions, claims[0], now=start + timedelta(minutes=10)
            )
            async with sessions() as session:
                resolved = await transition(
                    session,
                    principal,
                    ticket.id,
                    "resolved",
                    2,
                    start + timedelta(minutes=11),
                )
            auto_claim = (
                await claim_batch(sessions, now=start + timedelta(minutes=31))
            )[0]
            async with sessions() as session:
                reopened = await transition(
                    session,
                    principal,
                    ticket.id,
                    "in_progress",
                    resolved.version,
                    start + timedelta(minutes=31),
                )
            assert reopened.status == "in_progress"
            assert not await execute(
                sessions, auto_claim, now=start + timedelta(minutes=31)
            )
            async with sessions() as session:
                row = await session.get(Ticket, ticket.id)
                assert (
                    row is not None
                    and row.status == "in_progress"
                    and row.escalated_at is None
                )
                assert (
                    await session.scalar(select(func.count()).select_from(PolicyAudit))
                    == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_retry_exhaustion(migrated_database: Settings) -> None:
    assert asyncio.run(
        bootstrap("admin@example.com", "correct-horse-battery", migrated_database)
    )

    async def scenario() -> None:
        engine = create_engine(migrated_database)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        start = datetime(2026, 3, 1, tzinfo=UTC)
        try:
            async with sessions() as session:
                admin = await session.scalar(
                    select(User).where(User.email == "admin@example.com")
                )
                assert admin is not None
                principal = Principal(admin.id, "admin")
            async with sessions() as session:
                ticket = await create_ticket(
                    session,
                    principal,
                    TicketCreate(title="A", description="B"),
                    start,
                )
            for attempt in range(5):
                now = start + timedelta(minutes=60, seconds=100 * attempt)
                claim = (await claim_batch(sessions, now=now))[0]
                await fail(sessions, claim, now=now)
            async with sessions() as session:
                job = await session.scalar(
                    select(ScheduledJob).where(ScheduledJob.ticket_id == ticket.id)
                )
                assert job is not None and job.state == "failed" and job.attempts == 5
                assert job.failure_code == "execution_error"
                job_id = job.id
                page = await list_jobs(session, principal, "failed", 1, None)
                assert [item.id for item in page] == [job_id]
                assert await list_jobs(session, principal, "failed", 1, job_id) == []
            assert await claim_batch(sessions, now=start + timedelta(days=1)) == []
            async with sessions() as session:
                retried = await retry_job(
                    session,
                    principal,
                    job_id,
                    start + timedelta(days=1),
                )
                assert retried.state == "pending" and retried.attempts == 0
            claim = (await claim_batch(sessions, now=start + timedelta(days=1)))[0]
            assert await execute(sessions, claim, now=start + timedelta(days=1))
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_auto_close_at_deadline(migrated_database: Settings) -> None:
    assert asyncio.run(
        bootstrap("admin@example.com", "correct-horse-battery", migrated_database)
    )

    async def scenario() -> None:
        engine = create_engine(migrated_database)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        start = datetime(2026, 4, 1, tzinfo=UTC)
        try:
            async with sessions() as session:
                admin = await session.scalar(
                    select(User).where(User.email == "admin@example.com")
                )
                assert admin is not None
                principal = Principal(admin.id, "admin")
            async with sessions() as session:
                ticket = await create_ticket(
                    session, principal, TicketCreate(title="A", description="B"), start
                )
            async with sessions() as session:
                await transition(session, principal, ticket.id, "in_progress", 1, start)
            async with sessions() as session:
                await transition(session, principal, ticket.id, "resolved", 2, start)
            assert (
                await claim_batch(
                    sessions, now=start + timedelta(days=7, microseconds=-1)
                )
                == []
            )
            claim = (await claim_batch(sessions, now=start + timedelta(days=7)))[0]
            assert await execute(sessions, claim, now=start + timedelta(days=7))
            async with sessions() as session:
                row = await session.get(Ticket, ticket.id)
                assert row is not None and row.status == "closed"
                assert row.closed_at == start + timedelta(days=7)
                assert row.version == 4
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_worker_processes_past_due_job_promptly(migrated_database: Settings) -> None:
    assert asyncio.run(
        bootstrap("admin@example.com", "correct-horse-battery", migrated_database)
    )

    async def scenario() -> None:
        engine = create_engine(migrated_database)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        stop = asyncio.Event()
        task: asyncio.Task[None] | None = None
        try:
            async with sessions() as session:
                admin = await session.scalar(
                    select(User).where(User.email == "admin@example.com")
                )
                assert admin is not None
                principal = Principal(admin.id, "admin")
            async with sessions() as session:
                ticket = await create_ticket(
                    session,
                    principal,
                    TicketCreate(title="A", description="B"),
                    datetime.now(UTC) - timedelta(minutes=61),
                )
            start = time.monotonic()
            task = asyncio.create_task(run_worker(migrated_database, stop))
            while time.monotonic() - start < 5:
                async with sessions() as session:
                    row = await session.get(Ticket, ticket.id)
                    if row is not None and row.escalated_at is not None:
                        assert time.monotonic() - start < 5
                        break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("Worker did not process the past-due job within 5 seconds")
        finally:
            stop.set()
            if task is not None:
                await asyncio.wait_for(task, timeout=5)
            await engine.dispose()

    asyncio.run(scenario())
