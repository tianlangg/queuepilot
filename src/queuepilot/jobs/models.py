"""Scheduling policy, its audit trail, and leased due jobs."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from queuepilot.db.base import Base


class SchedulingPolicy(Base):
    __tablename__ = "scheduling_policy"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint(
            "escalation_minutes BETWEEN 1 AND 525600", name="escalation_range"
        ),
        CheckConstraint("auto_close_minutes BETWEEN 1 AND 525600", name="close_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    escalation_minutes: Mapped[int] = mapped_column(Integer)
    auto_close_minutes: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer)


class PolicyAudit(Base):
    __tablename__ = "scheduling_policy_audit"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    policy_version: Mapped[int] = mapped_column(Integer, unique=True)
    old_escalation_minutes: Mapped[int] = mapped_column(Integer)
    old_auto_close_minutes: Mapped[int] = mapped_column(Integer)
    escalation_minutes: Mapped[int] = mapped_column(Integer)
    auto_close_minutes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ScheduledJob(Base):
    __tablename__ = "scheduled_jobs"
    __table_args__ = (
        CheckConstraint("kind IN ('escalation', 'auto_close')", name="kind"),
        CheckConstraint(
            "state IN ('pending', 'completed', 'cancelled', 'failed')", name="state"
        ),
        Index(
            "ix_scheduled_jobs_due",
            "next_attempt_at",
            "id",
            postgresql_where="state = 'pending'",
        ),
        Index("ix_scheduled_jobs_ticket_state", "ticket_id", "state"),
        Index("ix_scheduled_jobs_state_id", "state", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tickets.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16))
    effect_key: Mapped[str] = mapped_column(String(128), unique=True)
    source_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_version: Mapped[int] = mapped_column(Integer)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(Text)
