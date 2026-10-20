import asyncio

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from queuepilot.auth.bootstrap import bootstrap
from queuepilot.core.config import Settings
from queuepilot.main import create_app

from .conftest import migrate

pytestmark = pytest.mark.integration


def create_admin(settings: Settings) -> None:
    assert asyncio.run(
        bootstrap("Admin@Example.com", "correct-horse-battery", settings)
    )
    assert not asyncio.run(
        bootstrap("admin@example.com", "correct-horse-battery", settings)
    )


def login(
    client: TestClient, email: str, password: str = "correct-horse-battery"
) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return dict(response.json())


def headers(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def test_authentication_rotation_replay_and_logout(migrated_database: Settings) -> None:
    create_admin(migrated_database)
    with TestClient(create_app(migrated_database)) as client:
        assert client.get("/api/v1/auth/me").status_code == 401
        assert client.post("/api/v1/auth/logout").status_code == 401
        assert client.get(f"/api/v1/users/{'0' * 32}").status_code == 401
        assert (
            client.post(
                "/api/v1/users",
                json={
                    "email": "someone@example.com",
                    "password": "another-good-password",
                    "role": "requester",
                },
            ).status_code
            == 401
        )
        assert (
            client.patch(
                f"/api/v1/users/{'0' * 32}/status", json={"disabled": True}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"email": "admin@example.com", "password": "wrong"},
            ).status_code
            == 401
        )
        tokens = login(client, "ADMIN@example.com")
        me = client.get("/api/v1/auth/me", headers=headers(tokens))
        assert me.status_code == 200
        assert set(me.json()) == {"id", "email", "role", "disabled", "created_at"}
        assert "password" not in me.text
        rotated_response = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert rotated_response.status_code == 200
        rotated = rotated_response.json()
        assert client.get("/api/v1/auth/me", headers=headers(tokens)).status_code == 401
        assert (
            client.get("/api/v1/auth/me", headers=headers(rotated)).status_code == 200
        )
        assert (
            client.post(
                "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
            ).status_code
            == 401
        )
        assert (
            client.get("/api/v1/auth/me", headers=headers(rotated)).status_code == 401
        )
        assert (
            client.post(
                "/api/v1/auth/refresh", json={"refresh_token": rotated["refresh_token"]}
            ).status_code
            == 401
        )
        fresh = login(client, "admin@example.com")
        assert (
            client.post("/api/v1/auth/logout", headers=headers(fresh)).status_code
            == 200
        )
        assert client.get("/api/v1/auth/me", headers=headers(fresh)).status_code == 401
        assert (
            client.post(
                "/api/v1/auth/refresh", json={"refresh_token": fresh["refresh_token"]}
            ).status_code
            == 401
        )


def test_invalid_expired_and_wrong_claims(migrated_database: Settings) -> None:
    create_admin(migrated_database)
    with TestClient(create_app(migrated_database)) as client:
        tokens = login(client, "admin@example.com")
        claims = jwt.decode(
            tokens["access_token"],
            migrated_database.token_secret.get_secret_value(),
            algorithms=["HS256"],
            audience=migrated_database.token_audience,
        )
        for changes in (
            {"exp": 1},
            {"aud": "other"},
            {"iss": "other"},
            {"typ": "refresh"},
            {"sid": "bad"},
        ):
            changed = jwt.encode(
                claims | changes,
                migrated_database.token_secret.get_secret_value(),
                algorithm="HS256",
            )
            assert (
                client.get(
                    "/api/v1/auth/me", headers={"Authorization": f"Bearer {changed}"}
                ).status_code
                == 401
            )
        assert (
            client.get(
                "/api/v1/auth/me", headers={"Authorization": "Bearer junk"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/v1/auth/refresh", json={"refresh_token": "junk"}
            ).status_code
            == 401
        )
        private = "must-not-appear-in-validation-response"
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": [private]},
        )
        assert response.status_code == 422
        assert private not in response.text


def test_user_permissions_disable_and_no_role_escalation(
    migrated_database: Settings,
) -> None:
    create_admin(migrated_database)
    with TestClient(create_app(migrated_database)) as client:
        admin = login(client, "admin@example.com")
        admin_id = client.get("/api/v1/auth/me", headers=headers(admin)).json()["id"]
        assert (
            client.patch(
                f"/api/v1/users/{admin_id}/status",
                headers=headers(admin),
                json={"disabled": True},
            ).status_code
            == 403
        )
        for role in ("requester", "agent"):
            response = client.post(
                "/api/v1/users",
                headers=headers(admin),
                json={
                    "email": f"{role}@example.com",
                    "password": "another-good-password",
                    "role": role,
                },
            )
            assert response.status_code == 201, response.text
            assert "password" not in response.text
        assert (
            client.post(
                "/api/v1/users",
                headers=headers(admin),
                json={
                    "email": "evil@example.com",
                    "password": "another-good-password",
                    "role": "admin",
                },
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/users",
                headers=headers(admin),
                json={
                    "email": "evil@example.com",
                    "password": "another-good-password",
                    "role": "requester",
                    "is_admin": True,
                },
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/users",
                headers=headers(admin),
                json={
                    "email": "requester@example.com",
                    "password": "another-good-password",
                    "role": "requester",
                },
            ).status_code
            == 409
        )
        requester = login(client, "requester@example.com", "another-good-password")
        agent = login(client, "agent@example.com", "another-good-password")
        requester_id = client.get("/api/v1/auth/me", headers=headers(requester)).json()[
            "id"
        ]
        agent_id = client.get("/api/v1/auth/me", headers=headers(agent)).json()["id"]
        for actor in (requester, agent):
            assert (
                client.post(
                    "/api/v1/users",
                    headers=headers(actor),
                    json={
                        "email": "evil@example.com",
                        "password": "another-good-password",
                        "role": "requester",
                    },
                ).status_code
                == 403
            )
            assert (
                client.patch(
                    f"/api/v1/users/{requester_id}/status",
                    headers=headers(actor),
                    json={"disabled": True},
                ).status_code
                == 403
            )
        assert (
            client.get(
                f"/api/v1/users/{agent_id}", headers=headers(requester)
            ).status_code
            == 403
        )
        assert (
            client.get(
                f"/api/v1/users/{requester_id}", headers=headers(requester)
            ).status_code
            == 200
        )
        assert (
            client.get(f"/api/v1/users/{agent_id}", headers=headers(admin)).status_code
            == 200
        )
        assert (
            client.patch(
                f"/api/v1/users/{requester_id}/status",
                headers=headers(admin),
                json={"disabled": True},
            ).status_code
            == 200
        )
        assert (
            client.get("/api/v1/auth/me", headers=headers(requester)).status_code == 401
        )
        assert (
            client.post(
                "/api/v1/auth/login",
                json={
                    "email": "requester@example.com",
                    "password": "another-good-password",
                },
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/v1/auth/refresh",
                json={"refresh_token": requester["refresh_token"]},
            ).status_code
            == 401
        )


def test_upgrade_from_foundation(empty_database: Settings) -> None:
    migrate(empty_database, "0001_foundation")
    migrate(empty_database)
    url = make_url(empty_database.database_url.get_secret_value()).set(
        drivername="postgresql"
    )
    with psycopg.connect(url.render_as_string(hide_password=False)) as connection:
        assert connection.execute("SELECT count(*) FROM users").fetchone() == (0,)
