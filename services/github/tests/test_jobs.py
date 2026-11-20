import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.jobs import (
    NewJob,
    Result,
    backoff,
    claim,
    enqueue,
    finish,
    retry_later,
    reviews_posted_since,
)
from app.models import JobStatus, ReviewJob
from tests.fakes import NOW

pytestmark = pytest.mark.anyio

STALE_AFTER = timedelta(minutes=30)


def new_job(head: str = "a" * 40, installation: int = 7, pull: int = 1) -> NewJob:
    return NewJob(
        installation_id=installation,
        repository_id=42,
        repository="octo-org/widgets",
        pull_number=pull,
        head_sha=head,
        base_sha="b" * 40,
    )


async def row(sessions, job_id: int) -> ReviewJob:
    async with sessions() as session:
        found = await session.get(ReviewJob, job_id)
    assert found is not None
    return found


async def test_each_commit_is_queued_once(sessions):
    assert await enqueue(sessions, new_job(), NOW) is True
    assert await enqueue(sessions, new_job(), NOW) is False  # a redelivered webhook
    assert await enqueue(sessions, new_job(head="c" * 40), NOW) is True  # a new commit
    queued = await row(sessions, 1)
    assert (queued.status, queued.attempts, queued.detail) == (JobStatus.QUEUED, 0, "")
    assert queued.not_before == NOW


async def test_claim_takes_the_oldest_ready_job(sessions):
    assert await claim(sessions, NOW, STALE_AFTER) is None
    await enqueue(sessions, new_job(), NOW)
    await enqueue(sessions, new_job(head="c" * 40), NOW)

    first = await claim(sessions, NOW, STALE_AFTER)
    assert first is not None
    assert (first.id, first.attempts, first.head_sha) == (1, 1, "a" * 40)
    assert (first.repository, first.pull_number, first.installation_id) == (
        "octo-org/widgets",
        1,
        7,
    )
    running = await row(sessions, 1)
    assert (running.status, running.locked_at) == (JobStatus.RUNNING, NOW)

    second = await claim(sessions, NOW, STALE_AFTER)
    assert second is not None
    assert second.id == 2
    assert await claim(sessions, NOW, STALE_AFTER) is None


async def test_a_job_to_retry_waits_for_its_time(sessions):
    await enqueue(sessions, new_job(), NOW)
    await claim(sessions, NOW, STALE_AFTER)
    await retry_later(sessions, 1, NOW, timedelta(minutes=1), "GitHub answered 502")

    waiting = await row(sessions, 1)
    assert (waiting.status, waiting.detail, waiting.locked_at) == (
        JobStatus.QUEUED,
        "GitHub answered 502",
        None,
    )
    assert await claim(sessions, NOW + timedelta(seconds=59), STALE_AFTER) is None
    again = await claim(sessions, NOW + timedelta(seconds=61), STALE_AFTER)
    assert again is not None
    assert again.attempts == 2


async def test_a_job_left_running_by_a_dead_worker_is_claimed_again(sessions):
    await enqueue(sessions, new_job(), NOW)
    await claim(sessions, NOW, STALE_AFTER)
    assert await claim(sessions, NOW + STALE_AFTER - timedelta(seconds=1), STALE_AFTER) is None
    again = await claim(sessions, NOW + STALE_AFTER + timedelta(seconds=1), STALE_AFTER)
    assert again is not None
    assert (again.id, again.attempts) == (1, 2)


async def test_a_locked_job_is_skipped_not_waited_for(sessions):
    """Two workers claiming at once each get their own job (SKIP LOCKED)."""
    await enqueue(sessions, new_job(), NOW)
    await enqueue(sessions, new_job(head="c" * 40), NOW)
    async with sessions() as other_worker, other_worker.begin():
        # Another worker is halfway through claiming job 1 and holds its lock.
        locked = select(ReviewJob).where(ReviewJob.id == 1).with_for_update()
        await other_worker.execute(locked)
        # Without SKIP LOCKED this would wait for that lock, here forever.
        job = await asyncio.wait_for(claim(sessions, NOW, STALE_AFTER), timeout=10)
    assert job is not None
    assert job.id == 2


async def test_finish_records_the_result(sessions):
    await enqueue(sessions, new_job(), NOW)
    await claim(sessions, NOW, STALE_AFTER)
    url = "https://github.com/octo-org/widgets/pull/1#pullrequestreview-9"
    later = NOW + timedelta(minutes=2)
    await finish(sessions, 1, later, Result(JobStatus.POSTED, "x" * 600, url, 1200, 340))

    done = await row(sessions, 1)
    assert (done.status, done.review_url, done.input_tokens, done.output_tokens) == (
        JobStatus.POSTED,
        url,
        1200,
        340,
    )
    assert done.detail == "x" * 500  # cut to fit the column
    assert (done.locked_at, done.updated_at) == (None, later)


async def test_reviews_posted_in_the_last_day_are_counted_per_installation(sessions):
    async def job_with(status, installation, hours_ago, head):
        await enqueue(sessions, new_job(head=head, installation=installation), NOW)
        job = await claim(sessions, NOW, STALE_AFTER)
        assert job is not None
        await finish(sessions, job.id, NOW - timedelta(hours=hours_ago), Result(status, ""))

    assert await reviews_posted_since(sessions, 7, NOW - timedelta(days=1)) == 0
    await job_with(JobStatus.POSTED, installation=7, hours_ago=2, head="1" * 40)
    await job_with(JobStatus.POSTED, installation=7, hours_ago=25, head="2" * 40)  # too old
    await job_with(JobStatus.POSTED, installation=8, hours_ago=1, head="3" * 40)  # not ours
    await job_with(JobStatus.SKIPPED, installation=7, hours_ago=1, head="4" * 40)  # no review
    assert await reviews_posted_since(sessions, 7, NOW - timedelta(days=1)) == 1


@pytest.mark.parametrize(
    ("attempt", "wait"),
    [
        (0, timedelta(seconds=30)),
        (1, timedelta(seconds=30)),
        (2, timedelta(minutes=1)),
        (3, timedelta(minutes=2)),
        (6, timedelta(minutes=16)),
        (7, timedelta(minutes=30)),  # 32 minutes, capped
        (10_000, timedelta(minutes=30)),
    ],
)
def test_backoff_doubles_up_to_a_limit(attempt, wait):
    assert backoff(attempt) == wait
