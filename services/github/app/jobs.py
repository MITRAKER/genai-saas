"""The review queue: a Postgres table that is also the record of every review.

Webhooks add rows ("queued"). Workers claim them with SELECT ... FOR UPDATE
SKIP LOCKED: two workers never get the same job, and neither waits for the
other. Because the queue lives in the same database as everything else, a
job and its result are saved in ordinary transactions; there is no second
system (Redis, a message broker) that could disagree with the database.

    queued ──claim──► running ──► posted | skipped | failed
       ▲                 │
       └── retry later ──┘   (a worker that crashes leaves "running" behind;
                              after a while another worker claims it again)
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from .db import Sessions
from .models import JobStatus, ReviewJob

MAX_DETAIL: Final = 500  # the length of the detail column
RETRY_FIRST: Final = timedelta(seconds=30)
RETRY_LONGEST: Final = timedelta(minutes=30)
MAX_DOUBLINGS: Final = 6  # 30 s doubled 6 times is past RETRY_LONGEST already


@dataclass(frozen=True)
class NewJob:
    installation_id: int
    repository_id: int
    repository: str  # "owner/name"
    pull_number: int
    head_sha: str
    base_sha: str


@dataclass(frozen=True)
class Job(NewJob):
    id: int
    attempts: int  # including the current one


@dataclass(frozen=True)
class Result:
    status: JobStatus
    detail: str
    review_url: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


def backoff(attempt: int) -> timedelta:
    """Wait before the next try: 30 s, 1 min, 2 min, ... at most 30 minutes."""
    doublings = min(max(attempt - 1, 0), MAX_DOUBLINGS)
    delay: timedelta = RETRY_FIRST * 2**doublings
    return min(delay, RETRY_LONGEST)


async def enqueue(sessions: Sessions, job: NewJob, now: datetime) -> bool:
    """Queue a review. False if this commit already has one.

    The unique constraint decides, inside the database, so even two copies of
    the same webhook arriving at the same moment can't both queue a review.
    """
    statement = (
        insert(ReviewJob)
        .values(
            **asdict(job),
            status=JobStatus.QUEUED,
            not_before=now,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["repository_id", "pull_number", "head_sha"])
        .returning(ReviewJob.id)
    )
    async with sessions() as session, session.begin():
        job_id = await session.scalar(statement)
    return job_id is not None


async def claim(sessions: Sessions, now: datetime, stale_after: timedelta) -> Job | None:
    """Take the oldest job that is ready, or that a crashed worker left running."""
    ready = and_(ReviewJob.status == JobStatus.QUEUED, ReviewJob.not_before <= now)
    abandoned = and_(ReviewJob.status == JobStatus.RUNNING, ReviewJob.locked_at < now - stale_after)
    statement = (
        select(ReviewJob)
        .where(or_(ready, abandoned))
        .order_by(ReviewJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    async with sessions() as session, session.begin():
        row = await session.scalar(statement)
        if row is None:
            return None
        row.status = JobStatus.RUNNING
        row.locked_at = now
        row.attempts += 1
        row.updated_at = now
        return Job(
            id=row.id,
            attempts=row.attempts,
            installation_id=row.installation_id,
            repository_id=row.repository_id,
            repository=row.repository,
            pull_number=row.pull_number,
            head_sha=row.head_sha,
            base_sha=row.base_sha,
        )


async def finish(sessions: Sessions, job_id: int, now: datetime, result: Result) -> None:
    statement = (
        update(ReviewJob)
        .where(ReviewJob.id == job_id)
        .values(
            status=result.status,
            detail=result.detail[:MAX_DETAIL],
            review_url=result.review_url,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            locked_at=None,
            updated_at=now,
        )
    )
    async with sessions() as session, session.begin():
        await session.execute(statement)


async def retry_later(
    sessions: Sessions, job_id: int, now: datetime, delay: timedelta, detail: str
) -> None:
    statement = (
        update(ReviewJob)
        .where(ReviewJob.id == job_id)
        .values(
            status=JobStatus.QUEUED,
            not_before=now + delay,
            detail=detail[:MAX_DETAIL],
            locked_at=None,
            updated_at=now,
        )
    )
    async with sessions() as session, session.begin():
        await session.execute(statement)


async def reviews_posted_since(sessions: Sessions, installation_id: int, since: datetime) -> int:
    statement = (
        select(func.count())
        .select_from(ReviewJob)
        .where(
            ReviewJob.installation_id == installation_id,
            ReviewJob.status == JobStatus.POSTED,
            ReviewJob.updated_at >= since,
        )
    )
    async with sessions() as session:
        count = await session.scalar(statement)
    return count or 0
