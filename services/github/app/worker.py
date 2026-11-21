"""The review worker: takes queued jobs from Postgres and reviews them.

    python -m app.worker

One job at a time. To review more pull requests at once, run more copies:
SKIP LOCKED in jobs.claim() makes that safe. On SIGTERM (docker stop) it
finishes the job in hand and then exits.
"""

import asyncio
import contextlib
import logging
import signal
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Final

import httpx2

from .agent import AgentClient, AgentError
from .codeindex import CodeIndex, Limits
from .config import GITHUB_API_VERSION, Settings, settings
from .db import create_schema, make_engine, make_sessions
from .embeddings import Embedder, HashingEmbedder, VoyageEmbedder
from .github import GitHubApp, GitHubError
from .jobs import Job, Result, backoff, claim, finish, retry_later
from .models import JobStatus
from .review import Reviewer, SkipJobError

logger = logging.getLogger("worker")

# A job still "running" after twice its time limit belongs to a dead worker.
STALE_FACTOR: Final = 2
VOYAGE_API_URL: Final = "https://api.voyageai.com"
GITHUB_HEADERS: Final = MappingProxyType(
    {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "genai-saas-reviewer",  # GitHub refuses requests without one
    }
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def judge(error: SkipJobError | GitHubError | AgentError) -> Result | str:
    """What an expected failure means: a Result to save, or why to try again."""
    if isinstance(error, SkipJobError):
        return Result(JobStatus.SKIPPED, str(error))
    return str(error) if error.retryable else Result(JobStatus.FAILED, str(error))


async def attempt(reviewer: Reviewer, job: Job, now: datetime) -> Result | str:
    """Run the job once: a Result to save, or the reason to try again later."""
    try:
        async with asyncio.timeout(reviewer.settings.job_timeout_seconds):
            return await reviewer.run(job, now)
    except (SkipJobError, GitHubError, AgentError) as error:
        return judge(error)
    except TimeoutError:
        return f"the job took longer than {reviewer.settings.job_timeout_seconds:g} seconds"
    except Exception as error:
        # A bug that one job runs into must not stop the worker: log it with
        # its traceback, and try the job again later.
        logger.exception("job %d: unexpected error", job.id)
        return f"unexpected error ({type(error).__name__})"


async def handle(reviewer: Reviewer, job: Job, now: datetime) -> None:
    sessions, limit = reviewer.sessions, reviewer.settings.job_max_attempts
    if job.attempts > limit:  # a job that kept crashing its worker
        await finish(
            sessions, job.id, utcnow(), Result(JobStatus.FAILED, f"gave up after {limit} attempts")
        )
        return
    outcome = await attempt(reviewer, job, now)
    if isinstance(outcome, str) and job.attempts < limit:
        await retry_later(sessions, job.id, utcnow(), backoff(job.attempts), outcome)
        logger.info("job %d: will retry: %s", job.id, outcome)
        return
    result = Result(JobStatus.FAILED, outcome) if isinstance(outcome, str) else outcome
    await finish(sessions, job.id, utcnow(), result)
    logger.info("job %d: %s: %s", job.id, result.status, result.detail)


async def wait(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, but wake up at once when asked to stop."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def run(reviewer: Reviewer, stop: asyncio.Event) -> int:
    """Handle jobs until stopped or worker_max_polls is reached (JPL rule 2:
    even this loop has a bound; Docker then starts a fresh worker). Returns
    the number of jobs handled."""
    config = reviewer.settings
    stale_after = timedelta(seconds=config.job_timeout_seconds * STALE_FACTOR)
    handled = 0
    for _ in range(config.worker_max_polls):
        if stop.is_set():
            break
        job = await claim(reviewer.sessions, utcnow(), stale_after)
        if job is None:
            await wait(stop, config.worker_poll_seconds)
            continue
        await handle(reviewer, job, utcnow())
        handled += 1
    return handled


def make_embedder(config: Settings, http: httpx2.AsyncClient) -> Embedder:
    if config.embeddings_provider == "voyage":
        return VoyageEmbedder(http, config.voyage_model)
    return HashingEmbedder()


@contextlib.asynccontextmanager
async def http_clients(
    config: Settings,
) -> AsyncIterator[tuple[httpx2.AsyncClient, httpx2.AsyncClient, httpx2.AsyncClient]]:
    """GitHub, the agent and Voyage AI, each with its own limits (JPL rule 3)."""
    # The agent sends a keepalive every 15 s, so a 120 s silence means it stalled.
    agent_timeout = httpx2.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0)
    voyage_headers = {"Authorization": f"Bearer {config.voyage_api_key}"}
    async with (
        httpx2.AsyncClient(
            base_url=config.github_api_url, headers=GITHUB_HEADERS, timeout=30
        ) as github,
        httpx2.AsyncClient(base_url=config.agent_url, timeout=agent_timeout) as agent,
        httpx2.AsyncClient(base_url=VOYAGE_API_URL, headers=voyage_headers, timeout=60) as voyage,
    ):
        yield github, agent, voyage


def limits(config: Settings) -> Limits:
    return Limits(
        max_files=config.index_max_files,
        max_file_bytes=config.index_max_file_bytes,
        max_new_chunks=config.index_max_new_chunks,
        max_snippets=config.context_max_snippets,
        max_chars=config.context_max_chars,
    )


async def serve(config: Settings, stop: asyncio.Event) -> int:
    """Run the worker until stopped. Returns the number of jobs handled."""
    if not (config.github_app_id and config.github_private_key_path):
        logger.warning(
            "No GitHub App configured (GITHUB_APP_ID, GITHUB_PRIVATE_KEY_PATH), "
            "so there is nothing to do. README.md explains the setup."
        )
        await stop.wait()
        return 0
    if config.embeddings_provider == "voyage" and not config.voyage_api_key:
        raise ValueError("EMBEDDINGS_PROVIDER=voyage needs VOYAGE_API_KEY")
    # Read once at startup, in a thread so the event loop never blocks on disk.
    key_file = Path(config.github_private_key_path)
    private_key = await asyncio.to_thread(key_file.read_text, encoding="utf-8")
    engine = make_engine(config.database_url)
    try:
        await create_schema(engine)
        sessions = make_sessions(engine)
        async with http_clients(config) as (github, agent, voyage):
            reviewer = Reviewer(
                github=GitHubApp(github, config.github_app_id, private_key),
                agent=AgentClient(agent),
                index=CodeIndex(sessions, make_embedder(config, voyage), limits(config)),
                sessions=sessions,
                settings=config,
            )
            logger.info("worker started")
            return await run(reviewer, stop)
    finally:
        await engine.dispose()


async def start() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGTERM, signal.SIGINT)
    for signal_number in signals:
        loop.add_signal_handler(signal_number, stop.set)
    try:
        handled = await serve(settings, stop)
    finally:
        for signal_number in signals:
            loop.remove_signal_handler(signal_number)
    logger.info("worker stopped after %d jobs", handled)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(start())


if __name__ == "__main__":
    main()
