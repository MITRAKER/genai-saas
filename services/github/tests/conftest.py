"""Shared fixtures. The tests that use the database need Postgres with pgvector:

    make test-db        # starts one in Docker, on port 5433

CI starts it the same way. Set TEST_DATABASE_URL to use another database;
the tests empty its tables.
"""

import os
from collections.abc import AsyncIterator

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.db import Sessions, create_schema, make_engine, make_sessions

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://test:test@localhost:5433/test"
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def sessions() -> AsyncIterator[Sessions]:
    """Connections to an empty database, for one test."""
    engine = make_engine(TEST_DATABASE_URL)
    try:
        await create_schema(engine)
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE review_jobs, code_chunks RESTART IDENTITY"))
    except OperationalError as error:
        await engine.dispose()
        pytest.fail(f"No test database at {TEST_DATABASE_URL}: `make test-db` starts one.\n{error}")
    yield make_sessions(engine)
    await engine.dispose()


@pytest.fixture(scope="session")
def private_key() -> str:
    """A GitHub App private key made for the tests: never commit a real one."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return pem.decode()
