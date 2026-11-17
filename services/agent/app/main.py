"""AI agent service: turns a diff into a streamed code review.

Like the core API it has no public port; the gateway authenticates the
user, applies rate limits and forwards the request with X-User-Id.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anthropic
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .config import settings
from .llm import AnthropicProvider, FakeProvider, ReviewProvider
from .sse import format_sse, with_keepalive

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("agent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = None
    if settings.llm_provider == "fake":
        app.state.provider = FakeProvider(delay=settings.fake_delay_seconds)
    else:
        # Reads ANTHROPIC_API_KEY from the environment; never hard-code keys.
        # The SDK retries 429/5xx/connection errors with backoff (max_retries).
        client = anthropic.AsyncAnthropic(max_retries=2)
        app.state.provider = AnthropicProvider(
            client, settings.anthropic_model, settings.review_effort, settings.review_max_tokens
        )
    logger.info("LLM provider: %s", settings.llm_provider)
    yield
    if client:
        await client.close()


app = FastAPI(title="AI Agent Service", lifespan=lifespan)


class ReviewRequest(BaseModel):
    diff: str = Field(min_length=1, description="A unified diff, e.g. the output of `git diff`")
    notes: str | None = Field(default=None, max_length=2000,
                              description="Optional notes from the author: what to focus on")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/reviews/stream")
async def stream_review(body: ReviewRequest, request: Request, x_user_id: str = Header(...)):
    # Check limits *before* calling the model: an oversized diff would cost
    # money and probably exceed the context window anyway.
    if len(body.diff) > settings.max_diff_chars:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Diff is too large ({len(body.diff)} characters, limit {settings.max_diff_chars})",
        )
    provider: ReviewProvider = request.app.state.provider

    async def events() -> AsyncIterator[str]:
        async for event in provider.stream_review(body.diff, body.notes):
            if event.type == "done":
                # Milestone 4 turns this log line into usage-based billing.
                logger.info("review done user=%s usage=%s", x_user_id, event.data["usage"])
            yield format_sse(event.type, event.data)

    return StreamingResponse(
        with_keepalive(events(), settings.keepalive_seconds),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Tells nginx-style proxies not to buffer, so tokens arrive live.
            "X-Accel-Buffering": "no",
        },
    )
