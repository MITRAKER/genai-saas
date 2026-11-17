from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

# One engine with a bounded connection pool, created at startup (JPL rule 3).
engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=5, max_overflow=10)
SessionLocal = sessionmaker(bind=engine, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one DB session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
