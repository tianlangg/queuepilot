"""Issue and atomically consume one-use WebSocket credentials."""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from queuepilot.auth.models import RefreshSession, User
from queuepilot.auth.permissions import Role
from queuepilot.auth.service import Principal
from queuepilot.realtime.models import ConnectionCredential


@dataclass(frozen=True)
class Identity:
    user_id: uuid.UUID
    session_id: uuid.UUID
    role: Role
    expires_at: datetime


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue(
    sessions: async_sessionmaker[AsyncSession],
    principal: Principal,
    session_id: uuid.UUID,
) -> tuple[str, datetime]:
    identifier = uuid.uuid4()
    token = f"{identifier}.{secrets.token_urlsafe(32)}"
    expires = datetime.now(UTC) + timedelta(seconds=60)
    async with sessions() as session, session.begin():
        session.add(
            ConnectionCredential(
                id=identifier,
                token_hash=_digest(token),
                user_id=principal.id,
                session_id=session_id,
                expires_at=expires,
            )
        )
    return token, expires


async def consume(
    sessions: async_sessionmaker[AsyncSession], token: str
) -> Identity | None:
    try:
        identifier = uuid.UUID(token.split(".", 1)[0])
    except (ValueError, TypeError):
        return None
    now = datetime.now(UTC)
    async with sessions() as session, session.begin():
        credential = await session.get(
            ConnectionCredential, identifier, with_for_update=True
        )
        if (
            credential is None
            or credential.used_at is not None
            or credential.expires_at <= now
            or not secrets.compare_digest(credential.token_hash, _digest(token))
        ):
            return None
        credential.used_at = now
        return await _active(session, credential.user_id, credential.session_id, now)


async def revalidate(
    sessions: async_sessionmaker[AsyncSession], identity: Identity
) -> Identity | None:
    now = datetime.now(UTC)
    if identity.expires_at <= now:
        return None
    async with sessions() as session:
        fresh = await _active(session, identity.user_id, identity.session_id, now)
        return (
            Identity(fresh.user_id, fresh.session_id, fresh.role, identity.expires_at)
            if fresh
            else None
        )


async def _active(
    session: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID, now: datetime
) -> Identity | None:
    row = (
        await session.execute(
            select(User, RefreshSession)
            .join(RefreshSession, RefreshSession.user_id == User.id)
            .where(User.id == user_id, RefreshSession.id == session_id)
        )
    ).one_or_none()
    if row is None:
        return None
    user, refresh = row
    if (
        user.disabled
        or refresh.used_at is not None
        or refresh.revoked_at is not None
        or refresh.expires_at <= now
    ):
        return None
    # Every connection has a bounded lifetime even if the refresh session is long-lived.
    return Identity(
        user.id,
        session_id,
        user.role,
        min(refresh.expires_at, now + timedelta(minutes=15)),
    )
