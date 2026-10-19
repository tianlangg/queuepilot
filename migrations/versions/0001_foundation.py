"""Establish the empty foundation baseline; domain tables land with features.

Revision ID: 0001_foundation
Revises: none
"""

revision: str = "0001_foundation"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Alembic creates/stamps its own version table; no domain DDL yet."""


def downgrade() -> None:
    """No domain data exists in this baseline to remove."""
