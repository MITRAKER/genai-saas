import json

import httpx2
import pytest

from app import agent
from app.agent import AgentClient, AgentError, Snippet, collect
from tests.fakes import DONE, agent_client, answering, sse, unreachable

pytestmark = pytest.mark.anyio

DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
CONTEXT = [Snippet("shop/cart.py", 1, 6, "def cart_total(items): ...", distance=0.3)]


async def lines(*events):
    """The stream as the client reads it: one line at a time."""
    for line in (b": keepalive\n\n" + sse(*events)).decode().splitlines():
        yield line


async def test_collect_adds_up_the_text_and_reads_the_usage():
    review = await collect(
        lines(
            ("thinking", {"text": "Reading the diff."}),
            ("text", {"text": "## Summary\n"}),
            ("progress", {"anything": True}),  # a kind of event added later
            ("text", {"text": "Fine."}),
            ("done", {**DONE, "truncated": True}),
        )
    )
    assert review.text == "## Summary\nFine."
    assert review.model == "fake-model"
    assert review.truncated is True
    assert (review.input_tokens, review.output_tokens) == (10, 20)


@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        ("llm_rate_limited", True),
        ("llm_unreachable", True),
        ("llm_error", True),
        ("timeout", True),
        ("llm_auth", False),
        ("refused", False),
        ("too_long", False),
    ],
)
async def test_an_error_event_says_whether_to_retry(code, retryable):
    events = lines(("text", {"text": "Half a"}), ("error", {"code": code, "message": "Oops."}))
    with pytest.raises(AgentError, match=r"the review failed: Oops\.") as caught:
        await collect(events)
    assert caught.value.retryable is retryable


async def test_a_stream_that_ends_early_can_be_retried():
    with pytest.raises(AgentError, match="ended without a result") as caught:
        await collect(lines(("text", {"text": "Half a"})))
    assert caught.value.retryable is True


async def test_reading_a_stream_is_bounded(monkeypatch):
    monkeypatch.setattr(agent, "MAX_STREAM_LINES", 4)
    events = [("text", {"text": "x"})] * 5 + [("done", DONE)]
    with pytest.raises(AgentError, match="ended without a result"):
        await collect(lines(*events))


async def test_review_sends_the_diff_notes_context_and_installation():
    seen = []
    events = (("text", {"text": "All good."}), ("done", DONE))
    async with agent_client(*events, seen=seen) as http:
        review = await AgentClient(http).review(DIFF, "Members pay less.", CONTEXT, 2002)
    assert review.text == "All good."
    [request] = seen
    assert request.url.path == "/reviews/stream"
    assert request.headers["x-installation-id"] == "2002"
    assert "x-user-id" not in request.headers
    assert json.loads(request.content) == {
        "diff": DIFF,
        "notes": "Members pay less.",
        "context": [
            {"path": "shop/cart.py", "start_line": 1, "end_line": 6, "content": CONTEXT[0].content}
        ],
    }


async def test_empty_notes_are_not_sent():
    seen = []
    async with agent_client(("done", DONE), seen=seen) as http:
        await AgentClient(http).review(DIFF, "", [], 2002)
    body = json.loads(seen[0].content)
    assert body["notes"] is None
    assert body["context"] == []


@pytest.mark.parametrize(("status", "retryable"), [(422, False), (413, False), (503, True)])
async def test_the_agent_refusing_a_review(status, retryable):
    async with agent_client(status=status) as http:
        with pytest.raises(AgentError, match=f"the agent answered {status}") as caught:
            await AgentClient(http).review(DIFF, "", [], 2002)
    assert caught.value.retryable is retryable


async def test_the_agent_unreachable():
    async with answering(unreachable) as http:
        with pytest.raises(AgentError, match="could not reach the agent") as caught:
            await AgentClient(http).review(DIFF, "", [], 2002)
    assert caught.value.retryable is True


async def test_an_event_we_cannot_read():
    async with agent_client(("text", {"words": "?"})) as http:
        with pytest.raises(AgentError, match="could not read") as caught:
            await AgentClient(http).review(DIFF, "", [], 2002)
    assert caught.value.retryable is False


async def test_an_answer_that_is_not_a_stream():
    async with answering(lambda _: httpx2.Response(200, content=b"<html>hi</html>")) as http:
        with pytest.raises(AgentError, match="ended without a result"):
            await AgentClient(http).review(DIFF, "", [], 2002)
