"""The GitHub integration's tables.

review_jobs  one row per commit to review: the job queue *and* the record
             of what happened to each job.
code_chunks  slices of repositories' code with their embeddings (pgvector),
             searched for code related to a pull request.
"""

from datetime import datetime
from enum import StrEnum
from typing import Final

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Length of every embedding we store: Voyage's voyage-code-3 returns 1024
# numbers by default, and our local embedder is built to match.
EMBEDDING_DIMENSIONS: Final = 1024
SHA_LENGTH: Final = 64  # 40 for SHA-1 repositories, 64 for SHA-256 ones


class Base(DeclarativeBase):
    pass


class JobStatus(StrEnum):
    QUEUED = "queued"  # waiting for a worker (and for not_before)
    RUNNING = "running"  # claimed by a worker
    POSTED = "posted"  # the review is on the pull request
    SKIPPED = "skipped"  # nothing to do (closed, outdated, over a limit); see detail
    FAILED = "failed"  # gave up; see detail


class ReviewJob(Base):
    __tablename__ = "review_jobs"
    __table_args__ = (
        # One review per commit: a redelivered webhook can't queue a second one.
        UniqueConstraint("repository_id", "pull_number", "head_sha"),
        Index("ix_review_jobs_claim", "status", "not_before"),
        Index("ix_review_jobs_installation", "installation_id", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    installation_id: Mapped[int] = mapped_column(BigInteger)
    repository_id: Mapped[int] = mapped_column(BigInteger)
    repository: Mapped[str] = mapped_column(String(200))  # "owner/name"
    pull_number: Mapped[int]
    head_sha: Mapped[str] = mapped_column(String(SHA_LENGTH))
    base_sha: Mapped[str] = mapped_column(String(SHA_LENGTH))
    status: Mapped[str] = mapped_column(String(20))
    detail: Mapped[str] = mapped_column(String(500), default="")
    attempts: Mapped[int] = mapped_column(default=0)
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_url: Mapped[str | None] = mapped_column(String(500))
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CodeChunk(Base):
    __tablename__ = "code_chunks"
    __table_args__ = (
        UniqueConstraint("repository_id", "model", "path", "blob_sha", "start_line"),
        Index("ix_code_chunks_repository", "repository_id", "model"),
        Index("ix_code_chunks_installation", "installation_id"),
        # HNSW: a graph index that finds the nearest vectors without comparing
        # the query with every row. vector_cosine_ops: we search by cosine distance.
        Index(
            "ix_code_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Every search filters by repository: one customer's code must never
    # show up in another customer's review.
    installation_id: Mapped[int] = mapped_column(BigInteger)
    repository_id: Mapped[int] = mapped_column(BigInteger)
    # Vectors from different models live in different spaces; never mix them.
    model: Mapped[str] = mapped_column(String(100))
    path: Mapped[str] = mapped_column(String(1000))
    blob_sha: Mapped[str] = mapped_column(String(SHA_LENGTH))
    start_line: Mapped[int]
    end_line: Mapped[int]
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
