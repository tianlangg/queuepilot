"""Assignment races and durable effects on independent PostgreSQL connections."""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import cast

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from queuepilot.auth.bootstrap import bootstrap
from queuepilot.core.config import Settings
from queuepilot.db.session import create_engine
from queuepilot.main import create_app
from queuepilot.tickets import service as ticket_service
from queuepilot.tickets.models import Assignment, Audit, OutboxEvent, Ticket

from .conftest import migrate

pytestmark = pytest.mark.integration


def test_upgrade_from_ticket_schema(empty_database: Settings) -> None:
    migrate(empty_database, "0003_tickets")
    migrate(empty_database)
    url = make_url(empty_database.database_url.get_secret_value()).set(
        drivername="postgresql"
    )
    with psycopg.connect(url.render_as_string(hide_password=False)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM ticket_assignments"
        ).fetchone() == (0,)


def _login(client: TestClient, email: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct-horse-battery"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _setup(
    client: TestClient,
) -> tuple[dict[str, str], dict[str, str], dict[str, str], str, str]:
    admin = _login(client, "admin@example.com")
    ids = []
    for name in ("agent1", "agent2"):
        response = client.post(
            "/api/v1/users",
            headers=admin,
            json={
                "email": f"{name}@example.com",
                "password": "correct-horse-battery",
                "role": "agent",
            },
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    return (
        admin,
        _login(client, "agent1@example.com"),
        _login(client, "agent2@example.com"),
        ids[0],
        ids[1],
    )


def _ticket(client: TestClient, admin: dict[str, str]) -> str:
    response = client.post(
        "/api/v1/tickets",
        headers=admin,
        json={"title": "Assign", "description": "A ticket"},
    )
    assert response.status_code == 201, response.text
    return cast(str, response.json()["id"])


async def _effects(
    settings: Settings, ticket_id: str
) -> tuple[int, int, int, int, int]:
    engine = create_engine(settings)
    try:
        async with async_sessionmaker(engine)() as session:
            key = uuid.UUID(ticket_id)
            ticket = await session.get(Ticket, key)
            assert ticket is not None
            return (
                ticket.version,
                (
                    await session.scalar(
                        select(func.count())
                        .select_from(Assignment)
                        .where(Assignment.ticket_id == key)
                    )
                )
                or 0,
                (
                    await session.scalar(
                        select(func.count())
                        .select_from(Audit)
                        .where(Audit.ticket_id == key)
                    )
                )
                or 0,
                (
                    await session.scalar(
                        select(func.count())
                        .select_from(OutboxEvent)
                        .where(OutboxEvent.ticket_id == key)
                    )
                )
                or 0,
                1 if ticket.status == "in_progress" else 0,
            )
    finally:
        await engine.dispose()


def _race(
    client: TestClient,
    path: str,
    first: dict[str, str],
    second: dict[str, str],
    second_path: str | None = None,
    second_body: dict[str, str | int] | None = None,
) -> list[int]:
    gate = Barrier(3)

    def send(target: str, headers: dict[str, str], body: dict[str, str | int]) -> int:
        gate.wait(timeout=5)
        return int(client.post(target, headers=headers, json=body).status_code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(send, path, first, {"expected_version": 1})
        b = pool.submit(
            send, second_path or path, second, second_body or {"expected_version": 1}
        )
        gate.wait(timeout=5)
        return sorted((a.result(timeout=10), b.result(timeout=10)))


def test_claim_races_and_reassignment(migrated_database: Settings) -> None:
    assert asyncio.run(
        bootstrap("admin@example.com", "correct-horse-battery", migrated_database)
    )
    with TestClient(create_app(migrated_database)) as client:
        admin, first, second, first_id, second_id = _setup(client)
        for _ in range(5):
            ticket_id = _ticket(client, admin)
            path = f"/api/v1/tickets/{ticket_id}"
            assert _race(client, path + "/claim", first, second) == [200, 409]
            assert asyncio.run(_effects(migrated_database, ticket_id)) == (
                2,
                1,
                2,
                2,
                1,
            )
            winner = client.get(path, headers=admin).json()["assignee_id"]
            winner_header = first if winner == first_id else second
            repeated = client.post(
                path + "/claim", headers=winner_header, json={"expected_version": 1}
            )
            assert repeated.status_code == 200
            assert repeated.json()["version"] == 2
            assert asyncio.run(_effects(migrated_database, ticket_id)) == (
                2,
                1,
                2,
                2,
                1,
            )

        ticket_id = _ticket(client, admin)
        path = f"/api/v1/tickets/{ticket_id}"
        assert _race(
            client,
            path + "/claim",
            first,
            admin,
            path + "/reassign",
            {"assignee_id": second_id, "expected_version": 1},
        ) == [200, 409]
        assert asyncio.run(_effects(migrated_database, ticket_id)) == (2, 1, 2, 2, 1)
        changed = client.post(
            path + "/reassign",
            headers=admin,
            json={
                "assignee_id": second_id
                if client.get(path, headers=admin).json()["assignee_id"] == first_id
                else first_id,
                "expected_version": 2,
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["version"] == 3
        assert asyncio.run(_effects(migrated_database, ticket_id)) == (3, 2, 3, 3, 1)


def test_assignment_permissions_rollback_and_timeout(
    migrated_database: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert asyncio.run(
        bootstrap("admin@example.com", "correct-horse-battery", migrated_database)
    )
    with TestClient(create_app(migrated_database)) as client:
        admin, first, _, first_id, second_id = _setup(client)
        ticket_id = _ticket(client, admin)
        path = f"/api/v1/tickets/{ticket_id}"
        requester = client.post(
            "/api/v1/users",
            headers=admin,
            json={
                "email": "requester@example.com",
                "password": "correct-horse-battery",
                "role": "requester",
            },
        )
        requester_header = _login(client, "requester@example.com")
        assert requester.status_code == 201
        assert (
            client.post(
                path + "/claim", headers=requester_header, json={"expected_version": 1}
            ).status_code
            == 404
        )
        assert (
            client.post(
                path + "/reassign",
                headers=first,
                json={"assignee_id": first_id, "expected_version": 1},
            ).status_code
            == 403
        )
        assert (
            client.post(
                path + "/reassign",
                headers=admin,
                json={"assignee_id": requester.json()["id"], "expected_version": 1},
            ).status_code
            == 422
        )
        assert asyncio.run(_effects(migrated_database, ticket_id)) == (1, 0, 1, 1, 0)

        def abort_record(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("forced audit failure")

        with monkeypatch.context() as patch:
            patch.setattr(ticket_service, "record", abort_record)
            with pytest.raises(RuntimeError, match="forced audit failure"):
                client.post(
                    path + "/claim", headers=first, json={"expected_version": 1}
                )
        assert asyncio.run(_effects(migrated_database, ticket_id)) == (1, 0, 1, 1, 0)

        url = make_url(migrated_database.database_url.get_secret_value()).set(
            drivername="postgresql"
        )
        with psycopg.connect(url.render_as_string(hide_password=False)) as connection:
            connection.execute(
                "SELECT id FROM tickets WHERE id = %s FOR UPDATE", (ticket_id,)
            )
            response = client.post(
                path + "/claim", headers=first, json={"expected_version": 1}
            )
            assert response.status_code == 503, response.text
            assert response.headers["retry-after"] == "1"
            connection.rollback()
        assert asyncio.run(_effects(migrated_database, ticket_id)) == (1, 0, 1, 1, 0)
        assert (
            client.post(
                path + "/claim", headers=first, json={"expected_version": 1}
            ).status_code
            == 200
        )

        other_id = _ticket(client, admin)
        other_path = f"/api/v1/tickets/{other_id}"
        with psycopg.connect(url.render_as_string(hide_password=False)) as connection:
            connection.execute(
                "SELECT id FROM tickets WHERE id = %s FOR UPDATE", (ticket_id,)
            )
            with ThreadPoolExecutor(max_workers=2) as pool:
                blocked = pool.submit(
                    client.post,
                    path + "/reassign",
                    headers=admin,
                    json={"assignee_id": second_id, "expected_version": 2},
                )
                independent = pool.submit(
                    client.post,
                    other_path + "/claim",
                    headers=first,
                    json={"expected_version": 1},
                )
                assert independent.result(timeout=2).status_code == 200
                connection.rollback()
                assert blocked.result(timeout=5).status_code == 200
