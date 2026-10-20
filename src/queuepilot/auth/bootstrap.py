"""Explicit, repeatable first-administrator bootstrap; password from a prompt."""

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from queuepilot.auth.models import User
from queuepilot.auth.security import hash_password
from queuepilot.auth.service import normalize_email
from queuepilot.core.config import Settings
from queuepilot.db.session import create_engine


async def bootstrap(email: str, password: str, settings: Settings) -> bool:
    """Create the first admin; return False when the same admin already exists."""
    normalized = normalize_email(email)
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    hashed = await hash_password(password)
    engine = create_engine(settings)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session, session.begin():
            # Fixed transaction advisory lock serializes even when no admin row exists.
            await session.execute(text("SELECT pg_advisory_xact_lock(7123456789)"))
            existing = await session.scalar(
                select(User).where(User.email == normalized)
            )
            if existing is not None:
                if existing.role != "admin" or existing.disabled:
                    raise ValueError("Email belongs to another or disabled user")
                return False
            if await session.scalar(
                select(User.id).where(User.role == "admin").limit(1)
            ):
                raise ValueError("An administrator already exists")
            session.add(User(email=normalized, password_hash=hashed, role="admin"))
        return True
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the first QueuePilot administrator"
    )
    parser.add_argument("--email", required=True)
    args = parser.parse_args()
    password = getpass.getpass("Administrator password (12+ characters): ")
    try:
        created = asyncio.run(bootstrap(args.email, password, Settings()))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    print("Administrator created." if created else "Administrator already exists.")


if __name__ == "__main__":
    main()
