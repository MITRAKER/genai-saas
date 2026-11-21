"""AI agent service: turns a diff into a streamed code review.

Like the core API it has no public port. Two services call it: the gateway,
which authenticates a person and forwards their id in X-User-Id, and the
GitHub integration's worker, which reviews pull requests for a GitHub App
installation and sends its id in X-Installation-Id.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Final

import anthropic
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from .config import settings
from .llm import AnthropicProvider, FakeProvider, ReviewProvider
from .schemas import ReviewRequest
from .sse import format_sse, with_keepalive

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("agent")

# Both ids are positive whole numbers: the user's, from their verified token,
# and the installation's, from GitHub. A request with anything else did not
# come from the gateway or the worker.
ID_PATTERN: Final = r"^[1-9][0-9]{0,18}$"


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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def who_pays(user_id: str | None, installation_id: str | None) -> str:
    """ "user=42" or "installation=7": who a review is for, and later billed to.

    Exactly one of the two: a request with both or neither did not come from
    the gateway or the worker. Only these numbers are ever logged, so no
    request can write text of its choosing into our logs.
    """
    if user_id is not None and installation_id is None:
        return f"user={int(user_id)}"
    if installation_id is not None and user_id is None:
        return f"installation={int(installation_id)}"
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT, "Send exactly one of X-User-Id and X-Installation-Id"
    )


def check_size(body: ReviewRequest) -> None:
    """Check limits *before* calling the model: an oversized request would
    cost money and probably exceed the context window anyway."""
    if len(body.diff) > settings.max_diff_chars:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"Diff is too large ({len(body.diff)} characters, limit {settings.max_diff_chars})",
        )
    context_chars = sum(len(snippet.content) for snippet in body.context)
    if context_chars > settings.max_context_chars:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"Context is too large ({context_chars} characters, "
            f"limit {settings.max_context_chars})",
        )


@app.post("/reviews/stream")
async def stream_review(
    body: ReviewRequest,
    request: Request,
    x_user_id: Annotated[str | None, Header(pattern=ID_PATTERN)] = None,
    x_installation_id: Annotated[str | None, Header(pattern=ID_PATTERN)] = None,
) -> StreamingResponse:
    # Parse the headers once, at the boundary (JPL rule 5).
    payer = who_pays(x_user_id, x_installation_id)
    check_size(body)
    provider: ReviewProvider = request.app.state.provider

    async def events() -> AsyncIterator[str]:
        async for event in provider.stream_review(body.diff, body.notes, body.context):
            if event.type == "done":
                # Milestone 4 turns this log line into usage-based billing.
                logger.info("review done %s usage=%s", payer, event.data["usage"])
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
