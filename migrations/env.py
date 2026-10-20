"""Run migrations with the same validated settings and async driver as the API."""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from queuepilot.auth import models  # noqa: F401 - registers metadata
from queuepilot.core.config import Settings
from queuepilot.db.base import Base
from queuepilot.db.session import create_engine


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = create_engine(Settings())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=Settings().database_url.get_secret_value(),
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_online())
