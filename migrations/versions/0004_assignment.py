"""Persistent assignment history.

Revision ID: 0004_assignment
Revises: 0003_tickets
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_assignment"
down_revision: str | None = "0003_tickets"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "ticket_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ticket_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id"),
            nullable=False,
        ),
        sa.Column(
            "previous_assignee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
        ),
        sa.Column(
            "assignee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("ticket_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_ticket_assignments_ticket_version",
        "ticket_assignments",
        ["ticket_id", "ticket_version"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_ticket_assignments_ticket_version")
    op.drop_table("ticket_assignments")
