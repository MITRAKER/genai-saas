import asyncio
import logging
import os
import signal
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import worker
from app.agent import AgentError
from app.config import Settings
from app.embeddings import HashingEmbedder, VoyageEmbedder
from app.github import GitHubError
from app.jobs import Result, claim, enqueue
from app.models import JobStatus, ReviewJob
from app.review import SkipJobError
from tests.conftest import TEST_DATABASE_URL
from tests.fakes import DEMO_JOB, DEMO_NEW_JOB, NOW, answering

pytestmark = pytest.mark.anyio

POSTED = Result(JobStatus.POSTED, "review posted with 2 related code snippet(s)", "https://x")


def reviewer(run, sessions=None, **config):
    """Just what the worker loop uses of a Reviewer."""
    return SimpleNamespace(run=run, sessions=sessions, settings=Settings(**config))


def returning(outcome):
    async def run(job, now):
        return outcome

    return run


def raising(error: Exception):
    async def run(job, now):
        raise error

    return run


async def test_attempt_returns_the_result():
    assert await worker.attempt(reviewer(returning(POSTED)), DEMO_JOB, NOW) == POSTED


def failed(reason: str) -> Result:
    return Result(JobStatus.FAILED, reason)


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (SkipJobError("it is closed"), Result(JobStatus.SKIPPED, "it is closed")),
        (GitHubError("GitHub answered 502", retryable=True), "GitHub answered 502"),
        (GitHubError("GitHub answered 404", retryable=False), failed("GitHub answered 404")),
        (AgentError("the agent answered 503", retryable=True), "the agent answered 503"),
        (AgentError("the agent answered 422", retryable=False), failed("the agent answered 422")),
    ],
)
async def test_attempt_turns_known_errors_into_outcomes(error, outcome):
    assert await worker.attempt(reviewer(raising(error)), DEMO_JOB, NOW) == outcome


async def test_a_job_that_takes_too_long_is_stopped():
    async def slow(job, now):
        await asyncio.sleep(10)

    outcome = await worker.attempt(reviewer(slow, job_timeout_seconds=0.01), DEMO_JOB, NOW)
    assert outcome == "the job took longer than 0.01 seconds"


async def test_a_bug_in_one_job_is_logged_and_retried(caplog):
    with caplog.at_level(logging.ERROR, logger="worker"):
        outcome = await worker.attempt(reviewer(raising(KeyError("x"))), DEMO_JOB, NOW)
    assert outcome == "unexpected error (KeyError)"
    assert "job 1: unexpected error" in caplog.text
    assert "Traceback" in caplog.text


async def queued_job(sessions, attempts: int = 1):
    """The demo job, claimed `attempts` times."""
    await enqueue(sessions, DEMO_NEW_JOB, NOW)
    job = await claim(sessions, NOW, timedelta(minutes=30))
    assert job is not None
    return replace(job, attempts=attempts)


async def saved(sessions, job_id: int = 1) -> ReviewJob:
    async with sessions() as session:
        found = await session.get(ReviewJob, job_id)
    assert found is not None
    return found


async def test_handle_saves_the_result(sessions):
    job = await queued_job(sessions)
    await worker.handle(reviewer(returning(POSTED), sessions), job, NOW)
    row = await saved(sessions)
    assert (row.status, row.detail, row.review_url) == (
        JobStatus.POSTED,
        POSTED.detail,
        "https://x",
    )


async def test_handle_retries_later_while_attempts_are_left(sessions):
    job = await queued_job(sessions, attempts=2)
    before = worker.utcnow()
    await worker.handle(reviewer(returning("GitHub answered 502"), sessions), job, NOW)
    row = await saved(sessions)
    assert (row.status, row.detail) == (JobStatus.QUEUED, "GitHub answered 502")
    assert row.not_before >= before + timedelta(minutes=1)  # backoff(2)


async def test_the_last_attempt_fails_for_good(sessions):
    job = await queued_job(sessions, attempts=5)
    run = returning("GitHub answered 502")
    await worker.handle(reviewer(run, sessions, job_max_attempts=5), job, NOW)
    row = await saved(sessions)
    assert (row.status, row.detail) == (JobStatus.FAILED, "GitHub answered 502")


async def test_a_job_that_kept_crashing_its_worker_is_given_up(sessions):
    job = await queued_job(sessions, attempts=6)
    run = raising(AssertionError("must not run again"))
    await worker.handle(reviewer(run, sessions, job_max_attempts=5), job, NOW)
    row = await saved(sessions)
    assert (row.status, row.detail) == (JobStatus.FAILED, "gave up after 5 attempts")


async def test_run_handles_the_queue_and_then_waits(sessions):
    await enqueue(sessions, DEMO_NEW_JOB, NOW)
    await enqueue(sessions, replace(DEMO_NEW_JOB, head_sha="d" * 40), NOW)
    config = {"worker_max_polls": 3, "worker_poll_seconds": 0.01}
    handled = await worker.run(reviewer(returning(POSTED), sessions, **config), asyncio.Event())
    assert handled == 2
    assert [(await saved(sessions, i)).status for i in (1, 2)] == [JobStatus.POSTED] * 2


async def test_run_stops_when_asked():
    stop = asyncio.Event()
    stop.set()
    assert await worker.run(reviewer(returning(POSTED)), stop) == 0


async def test_wait_wakes_up_when_asked_to_stop():
    stop = asyncio.Event()
    stop.set()
    async with asyncio.timeout(1):
        await worker.wait(stop, seconds=60)
    await worker.wait(asyncio.Event(), seconds=0.01)  # and otherwise just sleeps


async def test_the_embedder_follows_the_settings():
    async with answering(lambda _: None) as http:
        assert isinstance(worker.make_embedder(Settings(), http), HashingEmbedder)
        voyage = worker.make_embedder(Settings(embeddings_provider="voyage"), http)
    assert isinstance(voyage, VoyageEmbedder)
    assert voyage.model == "voyage-code-3"


async def test_a_worker_without_a_github_app_waits_quietly(caplog):
    stop = asyncio.Event()
    stop.set()
    with caplog.at_level(logging.WARNING, logger="worker"):
        assert await worker.serve(Settings(github_app_id=""), stop) == 0
    assert "No GitHub App configured" in caplog.text


async def test_voyage_needs_a_key():
    config = Settings(
        github_app_id="12345",
        github_private_key_path="/run/secrets/github-app.pem",
        embeddings_provider="voyage",
        voyage_api_key="",
    )
    with pytest.raises(ValueError, match="needs VOYAGE_API_KEY"):
        await worker.serve(config, asyncio.Event())


async def test_serve_connects_everything_and_polls(sessions, private_key, tmp_path, caplog):
    key_file = tmp_path / "github-app.pem"
    key_file.write_text(private_key)
    config = Settings(
        github_app_id="12345",
        github_private_key_path=str(key_file),
        database_url=TEST_DATABASE_URL,
        worker_max_polls=1,
        worker_poll_seconds=0.01,
    )
    with caplog.at_level(logging.INFO, logger="worker"):
        assert await worker.serve(config, asyncio.Event()) == 0
    assert "worker started" in caplog.text


async def test_sigterm_lets_the_worker_finish_and_stop(monkeypatch, caplog):
    async def serve(config, stop):
        os.kill(os.getpid(), signal.SIGTERM)  # what `docker stop` sends
        await asyncio.wait_for(stop.wait(), timeout=10)
        return 3

    monkeypatch.setattr(worker, "serve", serve)
    with caplog.at_level(logging.INFO, logger="worker"):
        await worker.start()
    assert "worker stopped after 3 jobs" in caplog.text
    # The handlers are gone again: the next SIGINT is a normal Ctrl-C.
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_main_runs_the_worker(monkeypatch):
    started = []

    async def start():
        started.append(True)

    monkeypatch.setattr(worker, "start", start)
    worker.main()
    assert started == [True]
