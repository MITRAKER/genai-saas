"""Asks the agent service for a review and collects its event stream.

The agent streams Server-Sent Events (see services/agent/app/sse.py):
"thinking" and "text" pieces, then "done" with the token usage, or "error".
A person watches that stream token by token; the worker just needs the
finished text, so it adds the pieces up.
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Final

import httpx2
from pydantic import BaseModel, ValidationError

# Error codes from the agent that may go away if we try again later.
RETRYABLE_CODES: Final = frozenset({"llm_rate_limited", "llm_unreachable", "llm_error", "timeout"})
# JPL rule 2: a review is a few thousand lines of events at most.
MAX_STREAM_LINES: Final = 500_000


class AgentError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class Snippet:
    """A piece of related code from the repository, sent along as context."""

    path: str
    start_line: int
    end_line: int
    content: str
    distance: float = 0.0  # cosine distance to the closest search query


@dataclass(frozen=True)
class AgentReview:
    text: str
    model: str
    truncated: bool
    input_tokens: int
    output_tokens: int


class TextEvent(BaseModel):
    text: str


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int


class DoneEvent(BaseModel):
    model: str
    truncated: bool
    usage: Usage


class ErrorEvent(BaseModel):
    code: str
    message: str


def on_event(name: str, data: str, texts: list[str]) -> AgentReview | None:
    """Handle one event; return the review once it is done."""
    if name == "text":
        texts.append(TextEvent.model_validate_json(data).text)
    elif name == "error":
        error = ErrorEvent.model_validate_json(data)
        raise AgentError(
            f"the review failed: {error.message}", retryable=error.code in RETRYABLE_CODES
        )
    elif name == "done":
        done = DoneEvent.model_validate_json(data)
        return AgentReview(
            text="".join(texts),
            model=done.model,
            truncated=done.truncated,
            input_tokens=done.usage.input_tokens,
            output_tokens=done.usage.output_tokens,
        )
    return None  # "thinking", or a kind of event this code doesn't know yet


async def collect(lines: AsyncIterator[str]) -> AgentReview:
    texts: list[str] = []
    name = ""
    for _ in range(MAX_STREAM_LINES):
        line = await anext(lines, None)
        if line is None:
            break
        if line.startswith("event: "):
            name = line.removeprefix("event: ")
        elif line.startswith("data: "):
            review = on_event(name, line.removeprefix("data: "), texts)
            if review is not None:
                return review
    raise AgentError("the review stream ended without a result", retryable=True)


class AgentClient:
    def __init__(self, http: httpx2.AsyncClient) -> None:
        self.http = http

    async def review(
        self, diff: str, notes: str, context: Sequence[Snippet], installation_id: int
    ) -> AgentReview:
        body = {
            "diff": diff,
            "notes": notes or None,
            "context": [
                {
                    "path": s.path,
                    "start_line": s.start_line,
                    "end_line": s.end_line,
                    "content": s.content,
                }
                for s in context
            ],
        }
        # Who pays for this review: the installation, not a logged-in user.
        headers = {"X-Installation-Id": str(installation_id)}
        try:
            async with self.http.stream(
                "POST", "/reviews/stream", json=body, headers=headers
            ) as response:
                if response.status_code != HTTPStatus.OK:
                    message = f"the agent answered {response.status_code}"
                    retryable = response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR
                    raise AgentError(message, retryable=retryable)
                return await collect(aiter(response.aiter_lines()))
        except httpx2.RequestError as error:
            raise AgentError("could not reach the agent service", retryable=True) from error
        except ValidationError as error:
            raise AgentError(
                "the agent sent an event we could not read", retryable=False
            ) from error
