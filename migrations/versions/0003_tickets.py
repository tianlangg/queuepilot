"""Ticket vertical slice tables.

Revision ID: 0003_tickets
Revises: 0002_auth
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_tickets"
down_revision: str | None = "0002_auth"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "tickets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "requester_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "assignee_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('open', 'in_progress', 'resolved', 'closed')",
            name="ck_tickets_status",
        ),
        sa.CheckConstraint("version > 0", name="ck_tickets_version_positive"),
    )
    op.create_index(
        "ix_tickets_requester_created", "tickets", ["requester_id", "created_at", "id"]
    )
    op.create_index(
        "ix_tickets_status_created", "tickets", ["status", "created_at", "id"]
    )
    op.create_table(
        "ticket_comments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ticket_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id"),
            nullable=False,
        ),
        sa.Column(
            "author_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("internal", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_ticket_comments_ticket_created",
        "ticket_comments",
        ["ticket_id", "created_at", "id"],
    )
    op.create_table(
        "ticket_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ticket_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id"),
            nullable=False,
        ),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("ticket_version", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_ticket_audit_ticket_version",
        "ticket_audit",
        ["ticket_id", "ticket_version"],
        unique=True,
    )
    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ticket_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id"),
            nullable=False,
        ),
        sa.Column("ticket_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_outbox_events_pending", "outbox_events", ["created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_outbox_events_pending")
    op.drop_table("outbox_events")
    op.drop_index("ix_ticket_audit_ticket_version")
    op.drop_table("ticket_audit")
    op.drop_index("ix_ticket_comments_ticket_created")
    op.drop_table("ticket_comments")
    op.drop_index("ix_tickets_status_created")
    op.drop_index("ix_tickets_requester_created")
    op.drop_table("tickets")
