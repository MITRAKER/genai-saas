import logging
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

import fake_github
from app.agent import AgentClient, AgentError, AgentReview
from app.codeindex import CodeIndex
from app.config import Settings
from app.embeddings import EmbeddingError, HashingEmbedder
from app.github import Commit, GitHubApp, PullRequest
from app.jobs import Result, claim, enqueue, finish
from app.models import JobStatus
from app.review import (
    MAX_NOTES_CHARS,
    MAX_REVIEW_CHARS,
    Reviewer,
    SkipJobError,
    author_notes,
    check_still_current,
    escape_mentions,
    render,
    review_marker,
)
from app.worker import limits
from tests.fakes import DEMO_JOB, DEMO_NEW_JOB, DONE, agent_client, github_client

pytestmark = pytest.mark.anyio

REVIEW = AgentReview(
    text="## Summary\nLooks good, @alice.\n",
    model="claude-test",
    truncated=False,
    input_tokens=1,
    output_tokens=2,
)
EVENTS = (
    ("text", {"text": "## Summary\nThe discount is subtracted, "}),
    ("text", {"text": "not multiplied."}),
    ("done", DONE),
)


@pytest.mark.parametrize(
    ("markdown", "escaped"),
    [
        ("Thanks @alice!", "Thanks `@alice`!"),
        ("cc @octo-org/security", "cc `@octo-org/security`"),
        ("(@bob)", "(`@bob`)"),
        ("mail me@example.com", "mail me@example.com"),
        ("see `@alice` there", "see `@alice` there"),
        ("```python\n@decorator\ndef f(): ...\n```", "```python\n@decorator\ndef f(): ...\n```"),
        ("@@ -1 +1 @@", "@@ -1 +1 @@"),
        ("npm i path/@scope/pkg", "npm i path/@scope/pkg"),
    ],
)
def test_mentions_outside_code_notify_nobody(markdown, escaped):
    assert escape_mentions(markdown) == escaped


def test_render_adds_a_footer_and_the_marker():
    body = render(REVIEW, fake_github.HEAD_SHA, snippets=3)
    assert body.startswith("## Summary\nLooks good, `@alice`.\n\n---\n")
    assert "AI review of c0ffeec by claude-test, with 3 related code snippet(s)" in body
    assert body.endswith(review_marker(fake_github.HEAD_SHA))
    assert "length limit" not in body


def test_render_says_when_a_review_was_cut_short():
    long_review = replace(REVIEW, text="x" * (MAX_REVIEW_CHARS + 10), truncated=True)
    body = render(long_review, fake_github.HEAD_SHA, snippets=0)
    assert "Cut here: GitHub limits the size of a review." in body
    assert "The review reached its length limit" in body
    assert len(body) < 65_536


def pull(**changes) -> PullRequest:
    fields = {
        "number": 7,
        "state": "open",
        "title": "Members get a discount",
        "body": "Members pay 10% less.",
        "head": Commit(sha=fake_github.HEAD_SHA),
        "base": Commit(sha=fake_github.BASE_SHA),
    }
    return PullRequest(**{**fields, **changes})


def test_author_notes():
    assert author_notes(pull()) == "Members get a discount\n\nMembers pay 10% less."
    assert author_notes(pull(body=None)) == "Members get a discount"
    assert len(author_notes(pull(body="x" * 5_000))) == MAX_NOTES_CHARS


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"state": "closed"}, "the pull request is closed"),
        ({"draft": True}, "the pull request is a draft now"),
        ({"head": Commit(sha="d" * 40)}, "newer commits"),
    ],
)
def test_pull_requests_that_changed_since_the_webhook_are_skipped(changes, reason):
    with pytest.raises(SkipJobError, match=reason):
        check_still_current(pull(**changes), DEMO_JOB)


def test_a_current_pull_request_passes():
    check_still_current(pull(), DEMO_JOB)


@pytest.fixture
def now() -> datetime:
    """The real time: the fake GitHub checks the app's JWT against its clock."""
    return datetime.now(UTC)


class FailingEmbedder(HashingEmbedder):
    async def embed(self, texts, kind):
        raise EmbeddingError("Voyage AI answered 503")


@pytest.fixture
async def build(sessions, private_key):
    """make(...) -> a Reviewer wired to the fake GitHub and a canned agent."""
    async with AsyncExitStack() as stack:
        fake = fake_github.FakeGitHub()
        agent_requests = []

        async def make(events=EVENTS, embedder=None, **config):
            github = await stack.enter_async_context(github_client(fake))
            agent = await stack.enter_async_context(agent_client(*events, seen=agent_requests))
            settings = Settings(**config)
            return Reviewer(
                github=GitHubApp(github, fake_github.APP_ID, private_key),
                agent=AgentClient(agent),
                index=CodeIndex(sessions, embedder or HashingEmbedder(), limits(settings)),
                sessions=sessions,
                settings=settings,
            )

        make.fake = fake
        make.agent_requests = agent_requests
        yield make


async def test_a_review_is_posted_with_related_code_as_context(build, now):
    reviewer = await build()
    result = await reviewer.run(DEMO_JOB, now)

    assert result.status == JobStatus.POSTED
    assert result.detail == "review posted with 2 related code snippet(s)"
    assert result.review_url == "https://github.com/demo-org/shop/pull/7#pullrequestreview-1"
    assert (result.input_tokens, result.output_tokens) == (10, 20)
    [review] = build.fake.reviews
    assert review["body"].startswith("## Summary\nThe discount is subtracted, not multiplied.")
    assert review["body"].endswith(review_marker(fake_github.HEAD_SHA))

    [request] = build.agent_requests
    assert request.headers["x-installation-id"] == "2002"
    sent = request.read()
    assert b"Members get a discount" in sent
    assert b"shop/cart.py" in sent  # it calls the changed function


async def test_a_commit_is_reviewed_once(build, now):
    reviewer = await build()
    await reviewer.run(DEMO_JOB, now)
    with pytest.raises(SkipJobError, match="already has our review"):
        await reviewer.run(DEMO_JOB, now)
    assert len(build.fake.reviews) == 1


async def test_an_outdated_job_is_skipped(build, now):
    reviewer = await build()
    with pytest.raises(SkipJobError, match="newer commits"):
        await reviewer.run(replace(DEMO_JOB, head_sha="d" * 40), now)


async def test_a_closed_pull_request_is_skipped(build, monkeypatch, now):
    monkeypatch.setitem(fake_github.PULL_REQUEST, "state", "closed")
    reviewer = await build()
    with pytest.raises(SkipJobError, match="closed"):
        await reviewer.run(DEMO_JOB, now)


async def test_the_daily_limit_stops_reviews(build, sessions, now):
    await enqueue(sessions, DEMO_NEW_JOB, now)
    job = await claim(sessions, now, timedelta(minutes=30))
    assert job is not None
    await finish(sessions, job.id, now, Result(JobStatus.POSTED, "an earlier review"))
    reviewer = await build(max_reviews_per_day=1)
    with pytest.raises(SkipJobError, match="daily limit of 1 reviews"):
        await reviewer.run(replace(DEMO_JOB, id=2), now)
    assert build.fake.reviews == []


async def test_a_diff_too_large_is_skipped(build, now):
    reviewer = await build(max_diff_chars=100)
    with pytest.raises(SkipJobError, match="over 100 characters"):
        await reviewer.run(DEMO_JOB, now)


async def test_the_review_goes_on_without_context_if_indexing_fails(build, caplog, now):
    reviewer = await build(embedder=FailingEmbedder())
    with caplog.at_level(logging.WARNING, logger="worker"):
        result = await reviewer.run(DEMO_JOB, now)
    assert result.detail == "review posted with 0 related code snippet(s)"
    assert "reviewing without code context: Voyage AI answered 503" in caplog.text


async def test_no_context_wanted_means_no_indexing(build, sessions, now):
    reviewer = await build(context_max_snippets=0)
    result = await reviewer.run(DEMO_JOB, now)
    assert result.detail == "review posted with 0 related code snippet(s)"
    assert await reviewer.index.indexed_files(DEMO_JOB.repository_id) == set()


async def test_agent_errors_reach_the_worker(build, now):
    events = (("error", {"code": "llm_rate_limited", "message": "Busy."}),)
    reviewer = await build(events=events)
    with pytest.raises(AgentError, match="Busy") as caught:
        await reviewer.run(DEMO_JOB, now)
    assert caught.value.retryable is True
    assert build.fake.reviews == []
