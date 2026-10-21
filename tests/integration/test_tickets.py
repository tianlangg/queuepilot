"""Ticket contracts and persistence against an isolated PostgreSQL database."""

import asyncio
from datetime import datetime

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from queuepilot.auth.bootstrap import bootstrap
from queuepilot.auth.service import Principal
from queuepilot.core.config import Settings
from queuepilot.db.session import create_engine
from queuepilot.main import create_app
from queuepilot.tickets import service as ticket_service
from queuepilot.tickets.models import Audit, Comment, OutboxEvent, Ticket

from .conftest import migrate

pytestmark = pytest.mark.integration


def auth(client: TestClient, email: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct-horse-battery"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def counts(settings: Settings) -> tuple[int, int, int, int]:
    engine = create_engine(settings)
    try:
        sessions = async_sessionmaker(engine)
        async with sessions() as session:
            return (
                (await session.scalar(select(func.count()).select_from(Ticket))) or 0,
                (await session.scalar(select(func.count()).select_from(Comment))) or 0,
                (await session.scalar(select(func.count()).select_from(Audit))) or 0,
                (await session.scalar(select(func.count()).select_from(OutboxEvent)))
                or 0,
            )
    finally:
        await engine.dispose()


def test_ticket_flow_permissions_and_atomic_effects(
    migrated_database: Settings,
) -> None:
    assert asyncio.run(
        bootstrap("admin@example.com", "correct-horse-battery", migrated_database)
    )
    with TestClient(create_app(migrated_database)) as client:
        admin = auth(client, "admin@example.com")
        for email, role in (
            ("one@example.com", "requester"),
            ("two@example.com", "requester"),
            ("agent@example.com", "agent"),
        ):
            created = client.post(
                "/api/v1/users",
                headers=admin,
                json={
                    "email": email,
                    "password": "correct-horse-battery",
                    "role": role,
                },
            )
            assert created.status_code == 201, created.text
        one, two, agent = (
            auth(client, email)
            for email in ("one@example.com", "two@example.com", "agent@example.com")
        )
        created = client.post(
            "/api/v1/tickets",
            headers=one,
            json={"title": "Printer", "description": "No paper"},
        )
        assert created.status_code == 201, created.text
        ticket = created.json()
        path = f"/api/v1/tickets/{ticket['id']}"
        assert ticket["version"] == 1
        assert client.get(path, headers=two).status_code == 404
        assert client.get("/api/v1/tickets", headers=two).json()["items"] == []
        assert client.get(path, headers=agent).status_code == 200
        assert (
            client.post(
                path + "/comments",
                headers=two,
                json={"body": "guess", "expected_version": 1},
            ).status_code
            == 404
        )
        assert (
            client.post(
                path + "/comments",
                headers=one,
                json={"body": "secret", "internal": True, "expected_version": 1},
            ).status_code
            == 403
        )
        assert (
            client.post(
                path + "/transitions",
                headers=agent,
                json={"status": "in_progress", "expected_version": 1},
            ).status_code
            == 403
        )
        assert asyncio.run(counts(migrated_database)) == (1, 0, 1, 1)
        internal = client.post(
            path + "/comments",
            headers=agent,
            json={"body": "staff only", "internal": True, "expected_version": 1},
        )
        assert internal.status_code == 201, internal.text
        assert client.get(path + "/comments", headers=one).json()["items"] == []
        assert len(client.get(path + "/comments", headers=agent).json()["items"]) == 1
        assert (
            client.post(
                path + "/comments",
                headers=one,
                json={"body": "stale", "expected_version": 1},
            ).status_code
            == 409
        )
        public = client.post(
            path + "/comments",
            headers=one,
            json={"body": "Any update?", "expected_version": 2},
        )
        assert public.status_code == 201
        assert len(client.get(path + "/comments", headers=one).json()["items"]) == 1
        first_comment = client.get(path + "/comments?limit=1", headers=agent).json()
        assert first_comment["next_cursor"]
        second_comment = client.get(
            path + "/comments",
            headers=agent,
            params={"limit": 1, "cursor": first_comment["next_cursor"]},
        ).json()
        assert first_comment["items"][0]["id"] != second_comment["items"][0]["id"]
        assert second_comment["next_cursor"] is None
        assert (
            client.get(path + "/comments?cursor=bad", headers=agent).status_code == 422
        )
        assert (
            client.post(
                path + "/transitions",
                headers=admin,
                json={"status": "resolved", "expected_version": 3},
            ).status_code
            == 409
        )
        for old_version, status in ((3, "in_progress"), (4, "resolved"), (5, "closed")):
            response = client.post(
                path + "/transitions",
                headers=admin,
                json={"status": status, "expected_version": old_version},
            )
            assert response.status_code == 200, response.text
            assert response.json()["version"] == old_version + 1
        reopened = client.post(
            path + "/transitions",
            headers=one,
            json={"status": "open", "expected_version": 6},
        )
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["resolved_at"] is None
        assert reopened.json()["closed_at"] is None
        assert asyncio.run(counts(migrated_database)) == (1, 2, 7, 7)
        assert (
            client.post(
                "/api/v1/tickets",
                headers=one,
                json={"title": "x", "description": "y" * 17000},
            ).status_code
            == 413
        )
        assert (
            client.post(
                "/api/v1/tickets", headers=one, json={"title": "", "description": "x"}
            ).status_code
            == 422
        )
        assert asyncio.run(counts(migrated_database)) == (1, 2, 7, 7)


def test_stable_ticket_page(migrated_database: Settings) -> None:
    assert asyncio.run(
        bootstrap("admin@example.com", "correct-horse-battery", migrated_database)
    )
    with TestClient(create_app(migrated_database)) as client:
        admin = auth(client, "admin@example.com")
        ids = []
        for number in range(3):
            response = client.post(
                "/api/v1/tickets",
                headers=admin,
                json={"title": f"T{number}", "description": "body"},
            )
            assert response.status_code == 201
            ids.append(response.json()["id"])
        first = client.get("/api/v1/tickets?limit=2", headers=admin)
        assert first.status_code == 200
        assert first.json()["next_cursor"]
        second = client.get(
            "/api/v1/tickets",
            headers=admin,
            params={"limit": 2, "cursor": first.json()["next_cursor"]},
        )
        assert second.status_code == 200, second.text
        found = [item["id"] for item in first.json()["items"] + second.json()["items"]]
        assert set(found) == set(ids)
        assert len(found) == 3
        assert client.get("/api/v1/tickets?limit=101", headers=admin).status_code == 422
        assert (
            client.get("/api/v1/tickets?cursor=bad", headers=admin).status_code == 422
        )


def test_failed_audit_insert_rolls_back_every_effect(
    migrated_database: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert asyncio.run(
        bootstrap("admin@example.com", "correct-horse-battery", migrated_database)
    )
    original = ticket_service.record

    def duplicate_audit(
        session: AsyncSession,
        ticket: Ticket,
        actor: Principal,
        action: str,
        details: dict[str, str],
        now: datetime,
    ) -> None:
        original(session, ticket, actor, action, details, now)
        session.add(
            Audit(
                ticket_id=ticket.id,
                actor_id=actor.id,
                ticket_version=ticket.version,
                action=action,
                details={},
                created_at=now,
            )
        )

    with TestClient(create_app(migrated_database)) as client:
        admin = auth(client, "admin@example.com")
        monkeypatch.setattr(ticket_service, "record", duplicate_audit)
        with pytest.raises(IntegrityError):
            client.post(
                "/api/v1/tickets",
                headers=admin,
                json={"title": "Rollback", "description": "Everything"},
            )
        assert asyncio.run(counts(migrated_database)) == (0, 0, 0, 0)


def test_upgrade_from_auth_schema(empty_database: Settings) -> None:
    migrate(empty_database, "0002_auth")
    migrate(empty_database)
    url = make_url(empty_database.database_url.get_secret_value()).set(
        drivername="postgresql"
    )
    with psycopg.connect(url.render_as_string(hide_password=False)) as connection:
        assert connection.execute("SELECT count(*) FROM tickets").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM users").fetchone() == (0,)
