"""LLM providers: the one place that knows how to talk to a model.

The rest of the service only sees ReviewEvent objects, so we can swap
Claude for a fake in tests (no API key, no cost) without touching the
endpoint code.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import anthropic
from anthropic.types.beta import (
    BetaMessage,
    BetaRawContentBlockDeltaEvent,
    BetaTextDelta,
    BetaThinkingDelta,
)

from .config import Effort
from .prompts import SYSTEM_PROMPT, build_user_message

logger = logging.getLogger(__name__)

EventType = Literal["thinking", "text", "done", "error"]


@dataclass(frozen=True)
class ReviewEvent:
    """One step of a streamed review.

    thinking: a piece of Claude's reasoning summary (shown as "working...")
    text:     a piece of the review itself
    done:     the review finished; carries token usage for billing later
    error:    something went wrong; carries a code and a readable message
    """

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)


class ReviewProvider(Protocol):
    def stream_review(self, diff: str, notes: str | None) -> AsyncIterator[ReviewEvent]: ...


def error_event(code: str, message: str) -> ReviewEvent:
    return ReviewEvent("error", {"code": code, "message": message})


def to_review_event(event: object) -> ReviewEvent | None:
    """Translate one SDK stream event; None for events we don't forward."""
    if not isinstance(event, BetaRawContentBlockDeltaEvent):
        return None
    delta = event.delta
    if isinstance(delta, BetaThinkingDelta) and delta.thinking:
        return ReviewEvent("thinking", {"text": delta.thinking})
    if isinstance(delta, BetaTextDelta):
        return ReviewEvent("text", {"text": delta.text})
    return None


def error_event_for(error: anthropic.APIError) -> ReviewEvent:
    """Most specific first: each failure needs a different message (and only
    some are worth retrying). The SDK has already retried 429s and 5xx."""
    if isinstance(error, anthropic.AuthenticationError):
        return error_event("llm_auth", "The AI provider rejected our API key.")
    if isinstance(error, anthropic.RateLimitError):
        return error_event("llm_rate_limited", "The AI provider is busy. Try again shortly.")
    if isinstance(error, anthropic.APIConnectionError):
        return error_event("llm_unreachable", "Could not reach the AI provider.")
    logger.warning("Anthropic API error: %s", error)
    return error_event("llm_error", "The AI provider returned an error.")


def finished_event(message: BetaMessage) -> ReviewEvent:
    """Check why the model stopped instead of assuming it succeeded."""
    if message.stop_reason == "refusal":
        return error_event("refused", "The model declined to review this change.")
    return ReviewEvent(
        "done",
        {
            "stop_reason": message.stop_reason,
            # max_tokens means the review was cut off before it finished.
            "truncated": message.stop_reason == "max_tokens",
            # After a fallback this names the model that actually answered.
            "model": message.model,
            "usage": {
                "input_tokens": message.usage.input_tokens,
                "output_tokens": message.usage.output_tokens,
            },
        },
    )


class AnthropicProvider:
    """Streams a review from Claude through the Anthropic SDK."""

    def __init__(
        self, client: anthropic.AsyncAnthropic, model: str, effort: Effort, max_tokens: int
    ) -> None:
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        self.client = client
        self.model = model
        self.effort: Effort = effort
        self.max_tokens = max_tokens

    async def stream_review(self, diff: str, notes: str | None) -> AsyncIterator[ReviewEvent]:
        try:
            async with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_user_message(diff, notes)}],
                # Adaptive thinking: Claude decides how much to reason before
                # answering. "summarized" streams a readable summary of that
                # reasoning, so the user sees progress instead of a long pause.
                thinking={"type": "adaptive", "display": "summarized"},
                output_config={"effort": self.effort},
                # Safety classifiers occasionally decline legitimate code (for
                # example security tooling). With fallbacks the API retries the
                # same request on a suitable fallback model inside this call.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as stream:
                async for event in stream:
                    review_event = to_review_event(event)
                    if review_event is not None:
                        yield review_event
                message = await stream.get_final_message()
        except anthropic.APIError as error:
            yield error_event_for(error)
            return
        yield finished_event(message)


class FakeProvider:
    """A canned review, streamed in small pieces like a real model would."""

    def __init__(self, delay: float = 0.05) -> None:
        if delay < 0:
            raise ValueError("delay must not be negative")
        self.delay = delay

    async def stream_review(self, diff: str, notes: str | None) -> AsyncIterator[ReviewEvent]:
        lines = diff.splitlines()
        files = [line for line in lines if line.startswith("+++ b/")]
        added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))

        yield ReviewEvent("thinking", {"text": "Reading the diff and looking for risky changes."})
        await asyncio.sleep(self.delay)

        focus = f" Author notes: {notes}" if notes else ""
        review = (
            "## Summary\n"
            f"This is a fake review (LLM_PROVIDER=fake). The change touches "
            f"{len(files) or 'an unknown number of'} file(s) and adds {added} line(s).{focus}\n\n"
            "## Issues\n"
            "No issues found by the fake reviewer.\n\n"
            "## Suggestions\n"
            "Set LLM_PROVIDER=anthropic and ANTHROPIC_API_KEY to get a real review from Claude.\n"
        )
        for word in review.split(" "):
            yield ReviewEvent("text", {"text": word + " "})
            await asyncio.sleep(self.delay / 10)

        yield ReviewEvent(
            "done",
            {
                "stop_reason": "end_turn",
                "truncated": False,
                "model": "fake",
                "usage": {"input_tokens": len(diff) // 4, "output_tokens": len(review) // 4},
            },
        )
