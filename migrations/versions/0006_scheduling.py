"""Durable scheduling and automation audit.

Revision ID: 0006_scheduling
Revises: 0005_realtime
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_scheduling"
down_revision: str | None = "0005_realtime"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("opened_at", sa.DateTime(timezone=True)))
    op.add_column("tickets", sa.Column("escalated_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE tickets SET opened_at = created_at")
    op.alter_column("tickets", "opened_at", nullable=False)
    op.alter_column("ticket_audit", "actor_id", nullable=True)
    op.add_column(
        "ticket_audit",
        sa.Column("actor_kind", sa.String(16), server_default="user", nullable=False),
    )
    op.create_check_constraint(
        "ck_ticket_audit_actor",
        "ticket_audit",
        "(actor_kind = 'user' AND actor_id IS NOT NULL) OR "
        "(actor_kind = 'automation' AND actor_id IS NULL)",
    )
    op.create_table(
        "scheduling_policy",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("escalation_minutes", sa.Integer(), nullable=False),
        sa.Column("auto_close_minutes", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_scheduling_policy_singleton"),
        sa.CheckConstraint(
            "escalation_minutes BETWEEN 1 AND 525600",
            name="ck_scheduling_policy_escalation_range",
        ),
        sa.CheckConstraint(
            "auto_close_minutes BETWEEN 1 AND 525600",
            name="ck_scheduling_policy_close_range",
        ),
    )
    op.execute("INSERT INTO scheduling_policy VALUES (1, 60, 10080, 1)")
    op.create_table(
        "scheduling_policy_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("policy_version", sa.Integer(), nullable=False, unique=True),
        sa.Column("old_escalation_minutes", sa.Integer(), nullable=False),
        sa.Column("old_auto_close_minutes", sa.Integer(), nullable=False),
        sa.Column("escalation_minutes", sa.Integer(), nullable=False),
        sa.Column("auto_close_minutes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "scheduled_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ticket_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("effect_key", sa.String(128), nullable=False, unique=True),
        sa.Column("source_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("failure_code", sa.Text()),
        sa.CheckConstraint(
            "kind IN ('escalation', 'auto_close')", name="ck_scheduled_jobs_kind"
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'completed', 'cancelled', 'failed')",
            name="ck_scheduled_jobs_state",
        ),
    )
    op.create_index(
        "ix_scheduled_jobs_due",
        "scheduled_jobs",
        ["next_attempt_at", "id"],
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index(
        "ix_scheduled_jobs_ticket_state", "scheduled_jobs", ["ticket_id", "state"]
    )
    op.create_index("ix_scheduled_jobs_state_id", "scheduled_jobs", ["state", "id"])
    op.execute(
        """INSERT INTO scheduled_jobs (
            id, ticket_id, kind, effect_key, source_at, source_version,
            due_at, next_attempt_at, state, attempts)
        SELECT gen_random_uuid(), id, 'escalation',
               'escalation:' || id::text || ':' || version::text,
               opened_at, version,
               opened_at + interval '60 minutes',
               opened_at + interval '60 minutes', 'pending', 0
        FROM tickets WHERE status = 'open' AND assignee_id IS NULL"""
    )
    op.execute(
        """INSERT INTO scheduled_jobs (
            id, ticket_id, kind, effect_key, source_at, source_version,
            due_at, next_attempt_at, state, attempts)
        SELECT gen_random_uuid(), id, 'auto_close',
               'auto_close:' || id::text || ':' || version::text,
               resolved_at, version,
               resolved_at + interval '10080 minutes',
               resolved_at + interval '10080 minutes', 'pending', 0
        FROM tickets WHERE status = 'resolved' AND resolved_at IS NOT NULL"""
    )


def downgrade() -> None:
    raise RuntimeError(
        "Scheduling downgrade would discard jobs and audit history; "
        "restore a backup instead"
    )
