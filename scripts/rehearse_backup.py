"""Rehearse pg_dump/pg_restore using two disposable databases on the test server."""

import argparse
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg import sql
from sqlalchemy.engine import make_url


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-isolated-test-server", action="store_true")
    args = parser.parse_args()
    if not args.confirm_isolated_test_server:
        parser.error("Explicit --confirm-isolated-test-server is required")
    root = Path(__file__).resolve().parents[1]
    values = dotenv_values(root / ".env.test")
    raw = os.environ.get("QUEUEPILOT_TEST_DATABASE_URL") or values.get(
        "QUEUEPILOT_TEST_DATABASE_URL"
    )
    if raw is None:
        parser.error("Set QUEUEPILOT_TEST_DATABASE_URL for the isolated test server")
    url = make_url(raw)
    if (
        url.drivername != "postgresql+psycopg"
        or url.database != "queuepilot_test"
        or url.host not in {"127.0.0.1", "localhost"}
        or url.port != 55433
    ):
        parser.error("Only the local queuepilot_test server on port 55433 is allowed")
    control = url.set(drivername="postgresql").render_as_string(hide_password=False)
    suffix = uuid.uuid4().hex[:12]
    source = f"queuepilot_test_backup_{suffix}"
    restored = f"queuepilot_test_restored_{suffix}"
    compose = [
        "docker",
        "compose",
        "--env-file",
        str(root / ".env.compose"),
        "--profile",
        "test",
        "exec",
        "-T",
        "postgres-test",
    ]
    created: list[str] = []
    with psycopg.connect(control, autocommit=True, connect_timeout=5) as admin:
        try:
            for name in (source, restored):
                admin.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
                )
                created.append(name)
            source_url = url.set(database=source)
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("QUEUEPILOT_")
            }
            environment.update(
                QUEUEPILOT_ENVIRONMENT="test",
                QUEUEPILOT_DATABASE_URL=source_url.render_as_string(
                    hide_password=False
                ),
                QUEUEPILOT_REDIS_URL="redis://127.0.0.1:56380/0",
                QUEUEPILOT_TOKEN_SECRET="backup-rehearsal-only-secret-32-bytes",
            )
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=root,
                env=environment,
                check=True,
                timeout=30,
                capture_output=True,
            )
            ticket_id = uuid.uuid4()
            user_id = uuid.uuid4()
            stamp = datetime.now(UTC) - timedelta(hours=2)
            with psycopg.connect(
                source_url.set(drivername="postgresql").render_as_string(
                    hide_password=False
                )
            ) as database:
                database.execute(
                    """INSERT INTO users (id, email, password_hash, role)
                    VALUES (%s, 'backup@example.invalid', 'rehearsal-only', 'admin')""",
                    (user_id,),
                )
                database.execute(
                    """INSERT INTO tickets
                    (id, requester_id, title, description, status, version,
                     created_at, updated_at, opened_at)
                    VALUES (%s, %s, 'Backup', 'Rehearsal', 'open', 1,
                            %s, %s, %s)""",
                    (ticket_id, user_id, stamp, stamp, stamp),
                )
                database.execute(
                    """INSERT INTO ticket_audit
                    (id, ticket_id, actor_id, actor_kind, ticket_version,
                     action, details, created_at)
                    VALUES (%s, %s, %s, 'user', 1, 'created', '{}', %s)""",
                    (uuid.uuid4(), ticket_id, user_id, stamp),
                )
                database.execute(
                    """INSERT INTO scheduled_jobs
                    (id, ticket_id, kind, effect_key, source_at, source_version,
                     due_at, next_attempt_at, state, attempts)
                    VALUES (%s, %s, 'escalation', %s, %s, 1,
                            %s, %s, 'pending', 0)""",
                    (
                        uuid.uuid4(),
                        ticket_id,
                        f"escalation:{ticket_id}:1",
                        stamp,
                        stamp + timedelta(hours=1),
                        stamp + timedelta(hours=1),
                    ),
                )
            dump = subprocess.run(
                [*compose, "pg_dump", "-Fc", "-U", "queuepilot_test", "-d", source],
                check=True,
                capture_output=True,
                timeout=30,
            ).stdout
            subprocess.run(
                [
                    *compose,
                    "pg_restore",
                    "--no-owner",
                    "--no-privileges",
                    "-U",
                    "queuepilot_test",
                    "-d",
                    restored,
                ],
                input=dump,
                check=True,
                capture_output=True,
                timeout=30,
            )
            restore_url = url.set(database=restored, drivername="postgresql")
            with psycopg.connect(
                restore_url.render_as_string(hide_password=False)
            ) as database:
                revision = database.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
                ticket = database.execute(
                    "SELECT version, status FROM tickets WHERE id = %s", (ticket_id,)
                ).fetchone()
                audit = database.execute(
                    "SELECT count(*) FROM ticket_audit WHERE ticket_id = %s",
                    (ticket_id,),
                ).fetchone()
                job = database.execute(
                    "SELECT state FROM scheduled_jobs WHERE ticket_id = %s",
                    (ticket_id,),
                ).fetchone()
                if (
                    revision != ("0006_scheduling",)
                    or ticket != (1, "open")
                    or audit != (1,)
                    or job != ("pending",)
                ):
                    raise RuntimeError("Restored database failed integrity checks")
            print("Backup restore rehearsal passed: ticket, audit, job, schema")
        finally:
            for name in reversed(created):
                admin.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                        sql.Identifier(name)
                    )
                )


if __name__ == "__main__":
    main()
