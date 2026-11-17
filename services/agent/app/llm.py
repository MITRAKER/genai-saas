"""LLM providers: the one place that knows how to talk to a model.

The rest of the service only sees ReviewEvent objects, so we can swap
Claude for a fake in tests (no API key, no cost) without touching the
endpoint code.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

import anthropic

from .prompts import SYSTEM_PROMPT, build_user_message

logger = logging.getLogger(__name__)

EventType = Literal["thinking", "text", "done", "error"]


@dataclass
class ReviewEvent:
    """One step of a streamed review.

    thinking: a piece of Claude's reasoning summary (shown as "working...")
    text:     a piece of the review itself
    done:     the review finished; carries token usage for billing later
    error:    something went wrong; carries a code and a readable message
    """

    type: EventType
    data: dict = field(default_factory=dict)


class ReviewProvider(Protocol):
    def stream_review(self, diff: str, notes: str | None) -> AsyncIterator[ReviewEvent]: ...


def _error(code: str, message: str) -> ReviewEvent:
    return ReviewEvent("error", {"code": code, "message": message})


class AnthropicProvider:
    """Streams a review from Claude through the Anthropic SDK."""

    def __init__(self, client: anthropic.AsyncAnthropic, model: str, effort: str, max_tokens: int):
        self.client = client
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    async def stream_review(self, diff: str, notes: str | None) -> AsyncIterator[ReviewEvent]:
        failure: ReviewEvent | None = None
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
                    if event.type != "content_block_delta":
                        continue
                    if event.delta.type == "thinking_delta" and event.delta.thinking:
                        yield ReviewEvent("thinking", {"text": event.delta.thinking})
                    elif event.delta.type == "text_delta":
                        yield ReviewEvent("text", {"text": event.delta.text})
                message = await stream.get_final_message()
        # Most specific first: each failure needs a different message (and
        # only some of them are worth retrying).
        except anthropic.AuthenticationError:
            failure = _error("llm_auth", "The AI provider rejected our API key.")
        except anthropic.RateLimitError:
            failure = _error("llm_rate_limited", "The AI provider is busy. Try again shortly.")
        except anthropic.APIStatusError as e:
            logger.warning("Anthropic API error %s: %s", e.status_code, e.message)
            failure = _error("llm_error", "The AI provider returned an error.")
        except anthropic.APIConnectionError:
            failure = _error("llm_unreachable", "Could not reach the AI provider.")

        if failure:
            yield failure
            return

        if message.stop_reason == "refusal":
            yield _error("refused", "The model declined to review this change.")
            return

        yield ReviewEvent("done", {
            "stop_reason": message.stop_reason,
            # max_tokens means the review was cut off before it finished.
            "truncated": message.stop_reason == "max_tokens",
            # After a fallback this names the model that actually answered.
            "model": message.model,
            "usage": {
                "input_tokens": message.usage.input_tokens,
                "output_tokens": message.usage.output_tokens,
            },
        })


class FakeProvider:
    """A canned review, streamed in small pieces like a real model would."""

    def __init__(self, delay: float = 0.05):
        self.delay = delay

    async def stream_review(self, diff: str, notes: str | None) -> AsyncIterator[ReviewEvent]:
        files = [line[6:] for line in diff.splitlines() if line.startswith("+++ b/")]
        added = sum(1 for line in diff.splitlines()
                    if line.startswith("+") and not line.startswith("+++"))

        yield ReviewEvent("thinking", {"text": "Reading the diff and looking for risky changes."})
        await asyncio.sleep(self.delay)

        review = (
            "## Summary\n"
            f"This is a fake review (LLM_PROVIDER=fake). The change touches "
            f"{len(files) or 'an unknown number of'} file(s) and adds {added} line(s).\n\n"
            "## Issues\n"
            "No issues found by the fake reviewer.\n\n"
            "## Suggestions\n"
            "Set LLM_PROVIDER=anthropic and ANTHROPIC_API_KEY to get a real review from Claude.\n"
        )
        for word in review.split(" "):
            yield ReviewEvent("text", {"text": word + " "})
            await asyncio.sleep(self.delay / 10)

        yield ReviewEvent("done", {
            "stop_reason": "end_turn",
            "truncated": False,
            "model": "fake",
            "usage": {"input_tokens": len(diff) // 4, "output_tokens": len(review) // 4},
        })
