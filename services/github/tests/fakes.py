"""Test doubles: GitHub (scripts/fake_github.py, served in memory) and an
agent service that streams a canned review."""

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import httpx2

import fake_github
from app.jobs import Job, NewJob
from app.worker import GITHUB_HEADERS

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)

# The job a webhook queues for the fake GitHub's pull request, and the same
# job as the worker gets it from the queue.
DEMO_NEW_JOB = NewJob(
    installation_id=fake_github.INSTALLATION_ID,
    repository_id=fake_github.REPOSITORY_ID,
    repository=f"{fake_github.OWNER}/{fake_github.NAME}",
    pull_number=fake_github.PULL_NUMBER,
    head_sha=fake_github.HEAD_SHA,
    base_sha=fake_github.BASE_SHA,
)
DEMO_JOB = Job(id=1, attempts=1, **asdict(DEMO_NEW_JOB))

DONE = {
    "stop_reason": "end_turn",
    "truncated": False,
    "model": "fake-model",
    "usage": {"input_tokens": 10, "output_tokens": 20},
}


def github_client(
    fake: fake_github.FakeGitHub, seen: list[httpx2.Request] | None = None
) -> httpx2.AsyncClient:
    """A client like the worker's, whose requests reach the in-memory fake."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        headers = {name.lower(): value for name, value in request.headers.items()}
        target = request.url.raw_path.decode()
        reply = fake.handle(request.method, target, headers, request.content)
        return httpx2.Response(
            reply.status, headers={"content-type": reply.content_type}, content=reply.body
        )

    return httpx2.AsyncClient(
        base_url="https://api.github.test",
        headers=GITHUB_HEADERS,
        transport=httpx2.MockTransport(handler),
    )


def answering(
    handler: Callable[[httpx2.Request], httpx2.Response], base_url: str = "https://test"
) -> httpx2.AsyncClient:
    """A client whose every request gets `handler`'s answer."""
    return httpx2.AsyncClient(base_url=base_url, transport=httpx2.MockTransport(handler))


def sse(*events: tuple[str, dict[str, Any]]) -> bytes:
    """Events encoded the way the agent streams them (services/agent/app/sse.py)."""
    return b"".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events
    )


def agent_client(
    *events: tuple[str, dict[str, Any]],
    status: int = 200,
    seen: list[httpx2.Request] | None = None,
) -> httpx2.AsyncClient:
    """An agent service that answers every review with these events."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        body = b": keepalive\n\n" + sse(*events)
        return httpx2.Response(status, headers={"content-type": "text/event-stream"}, content=body)

    return answering(handler, "http://agent.test")


def unreachable(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("connection refused", request=request)
