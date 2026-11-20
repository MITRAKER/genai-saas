"""Database connection and schema."""

from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base

type Sessions = async_sessionmaker[AsyncSession]

# Any fixed number works: each process takes this lock before creating the
# schema, so the web service and the worker starting together can't collide.
SCHEMA_LOCK: Final = 7_340_031


def make_engine(url: str) -> AsyncEngine:
    """One engine with a bounded connection pool, created at startup (JPL rule 3)."""
    return create_async_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)


def make_sessions(engine: AsyncEngine) -> Sessions:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_schema(engine: AsyncEngine) -> None:
    """Add the pgvector extension and our tables if they are missing.

    Fine while the schema is young; the day a table changes shape, migrations
    (Alembic) take over, as core's comment promises too.
    """
    async with engine.begin() as connection:
        await connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEMA_LOCK})
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.run_sync(Base.metadata.create_all)
