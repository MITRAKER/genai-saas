import hashlib
import hmac
import json
import logging
from dataclasses import replace

import httpx2
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import select

import fake_github
from app import main
from app.chunking import Chunk
from app.codeindex import CodeIndex, Limits
from app.config import Settings, settings
from app.embeddings import HashingEmbedder
from app.models import CodeChunk, JobStatus, ReviewJob
from app.webhooks import signature_is_valid
from tests.conftest import TEST_DATABASE_URL
from tests.fakes import DEMO_JOB

pytestmark = pytest.mark.anyio

SECRET = "test-only-webhook-secret-0123456789abcdef"


@pytest.fixture
async def client(sessions, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "github_allowed_accounts", "Demo-Org, someone-else")
    monkeypatch.setattr(main.app.state, "sessions", sessions, raising=False)
    transport = httpx2.ASGITransport(app=main.app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://github.test") as client:
        yield client


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def deliver(client, event: str, payload, signature: str | bytes | None = None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": "72d3162e-cc78-11e3-81ab-4c9367dc0958",
        "X-Hub-Signature-256": signature or sign(body),
        "Content-Type": "application/json",
    }
    return await client.post("/github/webhooks", content=body, headers=headers)


def pull_request_event(
    action: str = "opened",
    owner: str = "demo-org",
    draft: bool = False,
    head: str = fake_github.HEAD_SHA,
) -> dict:
    """The fields we read, among the many GitHub sends."""
    return {
        "action": action,
        "number": 7,
        "pull_request": {
            "number": 7,
            "draft": draft,
            "title": "Members get a discount",
            "head": {"sha": head, "ref": "member-discount"},
            "base": {"sha": fake_github.BASE_SHA, "ref": "main"},
        },
        "repository": {"id": 1001, "full_name": f"{owner}/shop", "owner": {"login": owner}},
        "installation": {"id": 2002},
        "sender": {"login": "someone"},
    }


async def jobs(sessions) -> list[ReviewJob]:
    async with sessions() as session:
        return list(await session.scalars(select(ReviewJob).order_by(ReviewJob.id)))


async def test_health(client):
    assert (await client.get("/health")).json() == {"status": "ok"}


async def test_a_pull_request_is_queued_once(client, sessions):
    first = await deliver(client, "pull_request", pull_request_event())
    assert first.status_code == 202
    assert first.json() == {"status": "queued", "reason": ""}
    again = await deliver(client, "pull_request", pull_request_event("synchronize"))
    assert again.json() == {
        "status": "duplicate",
        "reason": "this commit is already queued or reviewed",
    }
    [job] = await jobs(sessions)
    assert (job.installation_id, job.repository_id, job.repository) == (2002, 1001, "demo-org/shop")
    assert (job.pull_number, job.head_sha, job.base_sha) == (
        7,
        fake_github.HEAD_SHA,
        fake_github.BASE_SHA,
    )
    assert job.status == JobStatus.QUEUED


async def test_each_new_commit_gets_its_own_review(client, sessions):
    await deliver(client, "pull_request", pull_request_event())
    newer = await deliver(client, "pull_request", pull_request_event("synchronize", head="d" * 40))
    assert newer.json()["status"] == "queued"
    assert [job.head_sha for job in await jobs(sessions)] == [fake_github.HEAD_SHA, "d" * 40]


async def test_the_allowlist_ignores_case(client, sessions):
    response = await deliver(client, "pull_request", pull_request_event(owner="DEMO-ORG"))
    assert response.json()["status"] == "queued"


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (pull_request_event("closed"), "this action adds no code to review"),
        (pull_request_event("labeled"), "this action adds no code to review"),
        (pull_request_event(draft=True), "draft pull requests are reviewed once they are ready"),
        (pull_request_event(owner="stranger"), "this account is not in GITHUB_ALLOWED_ACCOUNTS"),
    ],
)
async def test_pull_requests_not_to_review(client, sessions, event, reason):
    response = await deliver(client, "pull_request", event)
    assert response.status_code == 202
    assert response.json() == {"status": "ignored", "reason": reason}
    assert await jobs(sessions) == []


async def test_an_empty_allowlist_reviews_nothing(client, sessions, monkeypatch):
    monkeypatch.setattr(settings, "github_allowed_accounts", "")
    response = await deliver(client, "pull_request", pull_request_event())
    assert response.json()["status"] == "ignored"


async def test_ping_and_events_we_do_not_use(client):
    assert (await deliver(client, "ping", {"zen": "Keep it simple."})).json()["status"] == "pong"
    other = await deliver(client, "issues", {"action": "opened"})
    assert other.json() == {"status": "ignored", "reason": "this event is not used"}


async def test_only_numbers_are_logged(client, caplog):
    with caplog.at_level(logging.INFO, logger="webhooks"):
        await deliver(client, "pull_request", pull_request_event())
    assert "queued a review of pull request 7 in repository 1001" in caplog.text
    assert "demo-org" not in caplog.text


@pytest.mark.parametrize(
    "signature",
    [
        "",  # missing
        sign(b"another body"),  # for other content: the body was changed
        sign(b"{}", secret="x" * 40),  # made with another secret
        "sha1=" + hashlib.sha1(b"{}", usedforsecurity=False).hexdigest(),  # old scheme
        "sha256=" + "0" * 64,
        "sha256=é".encode("latin-1"),  # not ASCII
    ],
)
async def test_deliveries_without_a_valid_signature_are_refused(client, sessions, signature):
    body = json.dumps(pull_request_event()).encode()
    headers = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": signature}
    response = await client.post("/github/webhooks", content=body, headers=headers)
    assert response.status_code == 401
    assert await jobs(sessions) == []


async def test_webhooks_are_off_without_a_secret(client, monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    response = await deliver(client, "ping", {}, signature=sign(b"{}", secret=""))
    assert response.status_code == 503


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        json.dumps({**pull_request_event(), "installation": None}).encode(),
        json.dumps(pull_request_event(head="not-a-sha")).encode(),
        json.dumps(
            {**pull_request_event(), "repository": {"id": 1, "full_name": "a/..", "owner": {}}}
        ).encode(),
    ],
)
async def test_signed_but_unexpected_payloads_are_refused(client, payload):
    response = await deliver(client, "pull_request", payload)
    assert response.status_code == 400
    assert response.json() == {"detail": "Unexpected payload"}


async def test_an_event_name_must_be_short(client):
    headers = {"X-GitHub-Event": "x" * 101, "X-Hub-Signature-256": sign(b"{}")}
    response = await client.post("/github/webhooks", content=b"{}", headers=headers)
    assert response.status_code == 422


async def index_three_repositories(sessions) -> None:
    """One chunk each in repositories 1001 and 1002 of installation 2002, and
    in repository 3001 of installation 3003."""
    index = CodeIndex(sessions, HashingEmbedder(), Limits(100, 100_000, 1_000, 8, 40_000))
    chunk = Chunk("a.py", "a" * 40, 1, 1, "def a(): pass")
    vectors = await index.embedder.embed([chunk.embedding_text()], "document")
    for job in (
        DEMO_JOB,
        replace(DEMO_JOB, repository_id=1002),
        replace(DEMO_JOB, repository_id=3001, installation_id=3003),
    ):
        await index.add(job, [chunk], vectors)


async def repositories_indexed(sessions) -> set[int]:
    async with sessions() as session:
        return set(await session.scalars(select(CodeChunk.repository_id)))


async def test_uninstalling_the_app_deletes_its_code(client, sessions):
    await index_three_repositories(sessions)
    event = {"action": "deleted", "installation": {"id": 2002}}
    response = await deliver(client, "installation", event)
    assert response.json() == {"status": "deleted", "reason": "2 indexed code chunks deleted"}
    assert await repositories_indexed(sessions) == {3001}


async def test_removing_repositories_deletes_their_code(client, sessions):
    await index_three_repositories(sessions)
    event = {
        "action": "removed",
        "installation": {"id": 2002},
        "repositories_removed": [{"id": 1001, "full_name": "demo-org/shop"}],
    }
    response = await deliver(client, "installation_repositories", event)
    assert response.json() == {"status": "deleted", "reason": "1 indexed code chunks deleted"}
    assert await repositories_indexed(sessions) == {1002, 3001}


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("installation", {"action": "created", "installation": {"id": 2002}}),
        ("installation", {"action": "suspend", "installation": {"id": 2002}}),
        (
            "installation_repositories",
            {"action": "added", "installation": {"id": 2002}, "repositories_added": []},
        ),
    ],
)
async def test_other_installation_changes_keep_the_code(client, sessions, event, payload):
    await index_three_repositories(sessions)
    response = await deliver(client, event, payload)
    assert response.json()["status"] == "ignored"
    assert await repositories_indexed(sessions) == {1001, 1002, 3001}


def test_signature_check():
    body = b'{"zen": "Design for failure."}'
    assert signature_is_valid(SECRET, body, sign(body))
    assert not signature_is_valid(SECRET, body, None)
    assert not signature_is_valid(SECRET, body, sign(body).upper())


def test_the_webhook_secret_must_be_strong_or_empty():
    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings(github_webhook_secret="x" * 31)
    assert Settings(github_webhook_secret="").github_webhook_secret == ""
    assert Settings(github_webhook_secret="x" * 32).github_webhook_secret == "x" * 32


def test_allowed_accounts_are_case_insensitive():
    config = Settings(github_allowed_accounts=" Demo-Org, someone-else ,, ")
    assert config.allowed_accounts() == frozenset({"demo-org", "someone-else"})
    assert Settings(github_allowed_accounts="").allowed_accounts() == frozenset()


async def test_startup_creates_the_tables_and_connections(sessions, monkeypatch):
    monkeypatch.setattr(settings, "database_url", TEST_DATABASE_URL)
    app = FastAPI()
    async with main.lifespan(app), app.state.sessions() as session:
        assert await session.scalar(select(ReviewJob.id)) is None
