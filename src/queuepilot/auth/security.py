"""Password hashing and strictly scoped bearer tokens."""

import asyncio
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from queuepilot.core.config import Settings

hasher = PasswordHasher()


def utcnow() -> datetime:
    return datetime.now(UTC)


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(hasher.hash, password)


async def verify_password(password_hash: str, password: str) -> bool:
    try:
        return await asyncio.to_thread(hasher.verify, password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def issue_access(
    user_id: uuid.UUID, session_id: uuid.UUID, settings: Settings, now: datetime
) -> str:
    return jwt.encode(
        {
            "sub": str(user_id),
            "sid": str(session_id),
            "typ": "access",
            "iss": settings.token_issuer,
            "aud": settings.token_audience,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=settings.access_token_minutes),
        },
        settings.token_secret.get_secret_value(),
        algorithm="HS256",
    )


def decode_access(token: str, settings: Settings) -> tuple[uuid.UUID, uuid.UUID]:
    claims = jwt.decode(
        token,
        settings.token_secret.get_secret_value(),
        algorithms=["HS256"],
        issuer=settings.token_issuer,
        audience=settings.token_audience,
        options={"require": ["sub", "sid", "typ", "iss", "aud", "iat", "nbf", "exp"]},
    )
    if claims["typ"] != "access":
        raise jwt.InvalidTokenError("Wrong token type")
    try:
        return uuid.UUID(claims["sub"]), uuid.UUID(claims["sid"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise jwt.InvalidTokenError("Invalid subject") from exc


def new_refresh_token(session_id: uuid.UUID) -> tuple[str, str]:
    token = f"{session_id}.{secrets.token_urlsafe(48)}"
    return token, refresh_hash(token)


def refresh_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def refresh_id(token: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(token.split(".", 1)[0]) if "." in token else None
    except ValueError:
        return None
