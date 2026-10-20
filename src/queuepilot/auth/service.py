"""Authentication and user workflows; each write owns one transaction."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

import jwt
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from queuepilot.auth.models import RefreshSession, User
from queuepilot.auth.permissions import Action, Role, allowed
from queuepilot.auth.schemas import TokenPair, UserCreate, UserResponse
from queuepilot.auth.security import (
    decode_access,
    hash_password,
    issue_access,
    new_refresh_token,
    refresh_hash,
    refresh_id,
    utcnow,
    verify_password,
)
from queuepilot.core.config import Settings


class AuthError(Exception):
    pass


class ForbiddenError(Exception):
    pass


class ConflictError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    id: uuid.UUID
    role: Role


def normalize_email(email: str) -> str:
    value = email.strip().lower()
    if (
        len(value) > 320
        or len(value) < 3
        or "@" not in value
        or any(c.isspace() for c in value)
    ):
        raise ValueError("Invalid email")
    return value


def authorize(principal: Principal, action: Action, *, owner: bool = False) -> None:
    if not allowed(principal.role, action, owner=owner):
        raise ForbiddenError


async def authenticate(
    token: str, settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> Principal:
    try:
        user_id, session_id = decode_access(token, settings)
    except jwt.InvalidTokenError as exc:
        raise AuthError from exc
    async with sessions() as session:
        row = await session.execute(
            select(User, RefreshSession)
            .join(RefreshSession, RefreshSession.user_id == User.id)
            .where(User.id == user_id, RefreshSession.id == session_id)
        )
        pair = row.one_or_none()
        if pair is None:
            raise AuthError
        user, refresh = pair
        if (
            user.disabled
            or refresh.used_at is not None
            or refresh.revoked_at is not None
            or refresh.expires_at <= utcnow()
        ):
            raise AuthError
        return Principal(id=user.id, role=cast(Role, user.role))


async def create_user(
    session: AsyncSession, principal: Principal, request: UserCreate
) -> UserResponse:
    authorize(principal, "user_create")
    try:
        email = normalize_email(request.email)
    except ValueError as exc:
        raise AuthError from exc
    password_hash = await hash_password(request.password.get_secret_value())
    try:
        async with session.begin():
            user = User(email=email, password_hash=password_hash, role=request.role)
            session.add(user)
            await session.flush()
            await session.refresh(user)
            result = UserResponse.model_validate(user)
    except IntegrityError as exc:
        raise ConflictError from exc
    return result


async def get_user(
    session: AsyncSession, principal: Principal, user_id: uuid.UUID
) -> UserResponse | None:
    authorize(principal, "user_read", owner=principal.id == user_id)
    user = await session.get(User, user_id)
    return UserResponse.model_validate(user) if user is not None else None


async def set_disabled(
    session: AsyncSession,
    principal: Principal,
    user_id: uuid.UUID,
    disabled: bool,
    now: datetime | None = None,
) -> UserResponse | None:
    authorize(principal, "user_disable", owner=principal.id == user_id)
    timestamp = now or utcnow()
    async with session.begin():
        user = await session.get(User, user_id, with_for_update=True)
        if user is None:
            return None
        user.disabled = disabled
        if disabled:
            await session.execute(
                update(RefreshSession)
                .where(
                    RefreshSession.user_id == user_id,
                    RefreshSession.revoked_at.is_(None),
                )
                .values(revoked_at=timestamp)
            )
        await session.flush()
        result = UserResponse.model_validate(user)
    return result


async def login(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    email: str,
    password: str,
    now: datetime | None = None,
) -> TokenPair:
    try:
        normalized = normalize_email(email)
    except ValueError as exc:
        raise AuthError from exc
    async with sessions() as session:
        user = await session.scalar(select(User).where(User.email == normalized))
        if user is None or user.disabled:
            raise AuthError
        user_id, password_hash = user.id, user.password_hash
    if not await verify_password(password_hash, password):
        raise AuthError
    timestamp = now or utcnow()
    sid = uuid.uuid4()
    refresh_token, digest = new_refresh_token(sid)
    async with sessions() as session, session.begin():
        user = await session.get(User, user_id, with_for_update=True)
        if user is None or user.disabled:
            raise AuthError
        session.add(
            RefreshSession(
                id=sid,
                family_id=sid,
                user_id=user_id,
                token_hash=digest,
                expires_at=timestamp + timedelta(days=settings.refresh_token_days),
            )
        )
    return TokenPair(
        access_token=issue_access(user_id, sid, settings, timestamp),
        refresh_token=refresh_token,
    )


async def refresh(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    token: str,
    now: datetime | None = None,
) -> TokenPair:
    sid = refresh_id(token)
    if sid is None:
        raise AuthError
    timestamp = now or utcnow()
    async with sessions() as session, session.begin():
        candidate = await session.get(RefreshSession, sid)
        if candidate is None:
            raise AuthError
        user = await session.get(User, candidate.user_id, with_for_update=True)
        old = await session.get(
            RefreshSession, sid, with_for_update=True, populate_existing=True
        )
        if old is None or old.token_hash != refresh_hash(token):
            raise AuthError
        if old.used_at is not None:
            await session.execute(
                update(RefreshSession)
                .where(
                    RefreshSession.family_id == old.family_id,
                    RefreshSession.revoked_at.is_(None),
                )
                .values(revoked_at=timestamp)
            )
            # Commit the family revocation, then reject the replay.
            replay = True
        else:
            replay = False
            if (
                user is None
                or user.disabled
                or old.revoked_at is not None
                or old.expires_at <= timestamp
            ):
                raise AuthError
            old.used_at = timestamp
            next_id = uuid.uuid4()
            new_token, digest = new_refresh_token(next_id)
            session.add(
                RefreshSession(
                    id=next_id,
                    family_id=old.family_id,
                    user_id=old.user_id,
                    token_hash=digest,
                    expires_at=timestamp + timedelta(days=settings.refresh_token_days),
                )
            )
            user_id = old.user_id
    if replay:
        raise AuthError
    return TokenPair(
        access_token=issue_access(user_id, next_id, settings, timestamp),
        refresh_token=new_token,
    )


async def logout(
    session: AsyncSession,
    token: str,
    settings: Settings,
    principal: Principal,
    now: datetime | None = None,
) -> None:
    try:
        user_id, sid = decode_access(token, settings)
    except jwt.InvalidTokenError as exc:
        raise AuthError from exc
    if user_id != principal.id:
        raise AuthError
    async with session.begin():
        refresh_session = await session.get(RefreshSession, sid)
        if refresh_session is None:
            raise AuthError
        await session.execute(
            update(RefreshSession)
            .where(
                RefreshSession.family_id == refresh_session.family_id,
                RefreshSession.revoked_at.is_(None),
            )
            .values(revoked_at=now or utcnow())
        )
