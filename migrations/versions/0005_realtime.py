"""Outbox retry state and single-use connection credentials.

Revision ID: 0005_realtime
Revises: 0004_assignment
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_realtime"
down_revision: str | None = "0004_assignment"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.add_column("outbox_events", sa.Column("lease_until", sa.DateTime(timezone=True)))
    op.add_column(
        "outbox_events", sa.Column("lease_token", postgresql.UUID(as_uuid=True))
    )
    op.add_column(
        "outbox_events",
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.drop_index("ix_outbox_events_pending", table_name="outbox_events")
    op.create_index(
        "ix_outbox_events_due",
        "outbox_events",
        ["available_at", "id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_table(
        "connection_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("refresh_sessions.id"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_connection_credentials_expires", "connection_credentials", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_connection_credentials_expires", table_name="connection_credentials"
    )
    op.drop_table("connection_credentials")
    op.drop_index("ix_outbox_events_due", table_name="outbox_events")
    op.create_index("ix_outbox_events_pending", "outbox_events", ["created_at", "id"])
    op.drop_column("outbox_events", "attempts")
    op.drop_column("outbox_events", "lease_token")
    op.drop_column("outbox_events", "lease_until")
    op.drop_column("outbox_events", "available_at")
