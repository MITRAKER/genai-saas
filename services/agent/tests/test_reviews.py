import asyncio
import json
import logging
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from anthropic.types.beta import (
    BetaRawContentBlockDeltaEvent,
    BetaRawMessageStartEvent,
    BetaTextDelta,
    BetaThinkingDelta,
)
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import main
from app.config import Settings
from app.llm import AnthropicProvider, FakeProvider

DIFF = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for frame in body.strip().split("\n\n"):
        lines = frame.split("\n")
        if lines[0].startswith(":"):
            continue  # keepalive comment
        name = lines[0].removeprefix("event: ")
        events.append((name, json.loads(lines[1].removeprefix("data: "))))
    return events


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main.settings, "llm_provider", "fake")
    monkeypatch.setattr(main.settings, "fake_delay_seconds", 0)
    with TestClient(main.app) as c:
        yield c


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_stream_review_end_to_end(client):
    body = {"diff": DIFF, "notes": "check the math"}
    r = client.post("/reviews/stream", json=body, headers={"X-User-Id": "1"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    names = [name for name, _ in events]
    assert names[0] == "thinking"
    assert "text" in names
    assert names[-1] == "done"
    review = "".join(data["text"] for name, data in events if name == "text")
    assert "## Summary" in review
    assert "1 file(s)" in review
    assert "check the math" in review


def test_requires_user_id_from_gateway(client):
    assert client.post("/reviews/stream", json={"diff": DIFF}).status_code == 422


@pytest.mark.parametrize("user_id", ["abc", "0", "-1", "1 2", "7\tx", "1" * 20])
def test_user_id_must_be_a_positive_number(client, user_id):
    r = client.post("/reviews/stream", json={"diff": DIFF}, headers={"X-User-Id": user_id})
    assert r.status_code == 422


def test_usage_is_logged_with_the_user_id(client, caplog):
    with caplog.at_level(logging.INFO, logger="agent"):
        r = client.post("/reviews/stream", json={"diff": DIFF}, headers={"X-User-Id": "42"})
    assert r.status_code == 200
    assert "review done user=42 usage=" in caplog.text


def test_oversized_diff_rejected_before_calling_model(client, monkeypatch):
    monkeypatch.setattr(main.settings, "max_diff_chars", 10)
    r = client.post("/reviews/stream", json={"diff": DIFF}, headers={"X-User-Id": "1"})
    assert r.status_code == 413


def test_empty_diff_rejected(client):
    r = client.post("/reviews/stream", json={"diff": ""}, headers={"X-User-Id": "1"})
    assert r.status_code == 422


def test_anthropic_provider_is_used_when_configured(monkeypatch):
    monkeypatch.setattr(main.settings, "llm_provider", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with TestClient(main.app):
        assert isinstance(main.app.state.provider, AnthropicProvider)


@pytest.mark.parametrize(
    ("field", "value"),
    [("review_max_tokens", 0), ("keepalive_seconds", -1), ("review_effort", "extreme")],
)
def test_bad_settings_fail_at_startup(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_providers_reject_bad_arguments():
    with pytest.raises(ValueError, match="max_tokens"):
        AnthropicProvider(SimpleNamespace(), "claude-opus-5-5", "high", 0)
    with pytest.raises(ValueError, match="delay"):
        FakeProvider(delay=-1)


# --- AnthropicProvider, with a stand-in for the SDK client -----------------
# We don't call the real API in unit tests: it costs money, needs a key and
# its answers vary. Instead we check that we send the right request and
# translate the SDK's stream events correctly.


class FakeSDKStream:
    def __init__(self, events, final, error=None):
        self.events, self.final, self.error = events, final, error

    async def __aenter__(self):
        if self.error:
            raise self.error  # the real SDK raises HTTP errors when the stream opens
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        for event in self.events:
            yield event

    async def get_final_message(self):
        return self.final


def delta(value):
    return BetaRawContentBlockDeltaEvent(type="content_block_delta", index=0, delta=value)


def final_message(stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=120, output_tokens=40),
    )


def make_provider(stream):
    calls = []

    def open_stream(**kwargs):
        calls.append(kwargs)
        return stream

    sdk = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=open_stream)))
    return AnthropicProvider(sdk, "claude-opus-5-5", "high", 32000), calls


def run(provider):
    async def go():
        return [e async for e in provider.stream_review(DIFF, "check the math")]

    return asyncio.run(go())


def test_anthropic_provider_streams_and_reports_usage():
    start = BetaRawMessageStartEvent.model_construct(type="message_start")
    stream = FakeSDKStream(
        [
            start,
            delta(BetaThinkingDelta(type="thinking_delta", thinking="Looking at x.")),
            delta(BetaThinkingDelta(type="thinking_delta", thinking="")),
            delta(BetaTextDelta(type="text_delta", text="## Summary\n")),
            delta(BetaTextDelta(type="text_delta", text="Looks fine.")),
        ],
        final_message(),
    )
    provider, calls = make_provider(stream)
    events = run(provider)

    assert [e.type for e in events] == ["thinking", "text", "text", "done"]
    assert events[-1].data["usage"] == {"input_tokens": 120, "output_tokens": 40}
    assert events[-1].data["truncated"] is False

    request = calls[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["thinking"]["type"] == "adaptive"
    assert request["output_config"] == {"effort": "high"}
    assert request["fallbacks"] == "default"
    user_text = request["messages"][0]["content"]
    assert "<diff>" in user_text
    assert "check the math" in user_text


def test_truncated_review_is_flagged():
    provider, _ = make_provider(FakeSDKStream([], final_message("max_tokens")))
    assert run(provider)[-1].data["truncated"] is True


def test_refusal_becomes_error_event():
    provider, _ = make_provider(FakeSDKStream([], final_message("refusal")))
    events = run(provider)
    assert events[-1].type == "error"
    assert events[-1].data["code"] == "refused"


REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def status_error(cls, code):
    return cls("error", response=httpx2.Response(code, request=REQUEST), body=None)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (anthropic.APIConnectionError(request=REQUEST), "llm_unreachable"),
        (status_error(anthropic.AuthenticationError, 401), "llm_auth"),
        (status_error(anthropic.RateLimitError, 429), "llm_rate_limited"),
        (status_error(anthropic.InternalServerError, 500), "llm_error"),
    ],
)
def test_sdk_errors_become_error_events(error, code):
    provider, _ = make_provider(FakeSDKStream([], None, error=error))
    events = run(provider)
    assert [e.type for e in events] == ["error"]
    assert events[0].data["code"] == code


# --- The real SDK against a fake network -----------------------------------
# This runs the actual Anthropic SDK, but its HTTP transport returns a
# recorded-style SSE stream instead of calling the internet. It proves our
# request parameters are ones the SDK accepts and serializes, and that we
# parse the API's real event format.

API_STREAM = "".join(
    f"event: {json.loads(d)['type']}\ndata: {d}\n\n"
    for d in [
        '{"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant",'
        '"model":"claude-opus-5-5","content":[],"stop_reason":null,"stop_sequence":null,'
        '"usage":{"input_tokens":120,"output_tokens":1}}}',
        '{"type":"content_block_start","index":0,"content_block":{"type":"thinking",'
        '"thinking":"","signature":""}}',
        '{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta",'
        '"thinking":"Checking the change."}}',
        '{"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"s"}}',
        '{"type":"content_block_stop","index":0}',
        '{"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}',
        '{"type":"content_block_delta","index":1,'
        '"delta":{"type":"text_delta","text":"## Summary\\n"}}',
        '{"type":"content_block_delta","index":1,'
        '"delta":{"type":"text_delta","text":"Looks good."}}',
        '{"type":"content_block_stop","index":1}',
        '{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},'
        '"usage":{"output_tokens":42}}',
        '{"type":"message_stop"}',
    ]
)


def test_real_sdk_request_and_stream_parsing():
    sent = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent["body"] = json.loads(request.content)
        sent["headers"] = request.headers
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=API_STREAM.encode()
        )

    async def go():
        http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
        async with anthropic.AsyncAnthropic(api_key="test-key", http_client=http) as sdk:
            provider = AnthropicProvider(sdk, "claude-opus-5-5", "high", 32000)
            return [e async for e in provider.stream_review(DIFF, None)]

    events = asyncio.run(go())
    assert [e.type for e in events] == ["thinking", "text", "text", "done"]
    assert "".join(e.data["text"] for e in events if e.type == "text") == "## Summary\nLooks good."
    assert events[-1].data["usage"] == {"input_tokens": 120, "output_tokens": 42}

    body = sent["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["stream"] is True
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"] == {"effort": "high"}
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in sent["headers"]["anthropic-beta"]
