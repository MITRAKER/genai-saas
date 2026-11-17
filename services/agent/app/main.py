"""AI agent service: turns a diff into a streamed code review.

Like the core API it has no public port; the gateway authenticates the
user, applies rate limits and forwards the request with X-User-Id.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Final

import anthropic
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .config import settings
from .llm import AnthropicProvider, FakeProvider, ReviewProvider
from .sse import format_sse, with_keepalive

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("agent")

# The gateway forwards the id from the user's verified token: always a positive
# whole number. A request with anything else did not come through the gateway.
USER_ID_PATTERN: Final = r"^[1-9][0-9]{0,18}$"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if settings.llm_provider == "fake":
        app.state.provider = FakeProvider(delay=settings.fake_delay_seconds)
        logger.info("LLM provider: fake")
        yield
        return
    # Reads ANTHROPIC_API_KEY from the environment; never hard-code keys.
    # The SDK retries 429/5xx/connection errors with backoff (max_retries).
    async with anthropic.AsyncAnthropic(max_retries=2) as client:
        app.state.provider = AnthropicProvider(
            client, settings.anthropic_model, settings.review_effort, settings.review_max_tokens
        )
        logger.info("LLM provider: anthropic, model %s", settings.anthropic_model)
        yield


app = FastAPI(title="AI Agent Service", lifespan=lifespan)


class ReviewRequest(BaseModel):
    diff: str = Field(min_length=1, description="A unified diff, e.g. the output of `git diff`")
    notes: str | None = Field(
        default=None, max_length=2000, description="Optional notes from the author"
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/reviews/stream")
async def stream_review(
    body: ReviewRequest,
    request: Request,
    x_user_id: Annotated[str, Header(pattern=USER_ID_PATTERN)],
) -> StreamingResponse:
    # Parse the header once, at the boundary (JPL rule 5). Only this number is
    # ever logged, so no request can write text of its choosing into our logs.
    user_id = int(x_user_id)
    # Check limits *before* calling the model: an oversized diff would cost
    # money and probably exceed the context window anyway.
    if len(body.diff) > settings.max_diff_chars:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"Diff is too large ({len(body.diff)} characters, limit {settings.max_diff_chars})",
        )
    provider: ReviewProvider = request.app.state.provider

    async def events() -> AsyncIterator[str]:
        async for event in provider.stream_review(body.diff, body.notes):
            if event.type == "done":
                # Milestone 4 turns this log line into usage-based billing.
                logger.info("review done user=%d usage=%s", user_id, event.data["usage"])
            yield format_sse(event.type, event.data)

    return StreamingResponse(
        with_keepalive(events(), settings.keepalive_seconds, settings.review_timeout_seconds),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Tells nginx-style proxies not to buffer, so tokens arrive live.
            "X-Accel-Buffering": "no",
        },
    )
