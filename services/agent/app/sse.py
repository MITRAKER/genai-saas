"""Server-Sent Events (SSE) helpers.

SSE is a simple text format over a normal HTTP response that stays open:

    event: text
    data: {"text": "Hello"}

    event: done
    data: {...}

Each event ends with a blank line. Browsers read it with `EventSource`
(or fetch and a stream reader); curl shows it with `curl -N`.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

KEEPALIVE = ": keepalive\n\n"  # lines starting with ":" are comments; clients ignore them
# JPL rule 2: every loop has a fixed upper bound. A review is a few thousand
# events at most, so a stream that needs more steps than this is a bug.
MAX_STEPS = 100_000


def format_sse(event: str, data: dict[str, Any]) -> str:
    # json.dumps escapes newlines inside the text, so each event's data is
    # always a single line, which is what the SSE format expects.
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def with_keepalive(
    source: AsyncIterator[str], interval: float, max_seconds: float
) -> AsyncIterator[str]:
    """Pass items through, send a keepalive comment whenever the source has
    been quiet for `interval` seconds, and give up after `max_seconds`.

    Load balancers and proxies often close connections that send nothing
    for 30 to 60 seconds, and a model can think for longer than that.
    """
    loop = asyncio.get_running_loop()
    stop_at = loop.time() + max_seconds
    it = aiter(source)
    pending = asyncio.ensure_future(anext(it))
    try:
        for _ in range(MAX_STEPS):
            remaining = stop_at - loop.time()
            if remaining <= 0:
                yield format_sse(
                    "error", {"code": "timeout", "message": "The review took too long."}
                )
                return
            done, _ = await asyncio.wait({pending}, timeout=min(interval, remaining))
            if not done:
                yield KEEPALIVE
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            yield item
            pending = asyncio.ensure_future(anext(it))
        yield format_sse("error", {"code": "too_long", "message": "The review was too long."})
    finally:
        # Runs when the stream ends, times out or the client disconnects.
        # Cancelling the pending read and closing the source also closes the
        # request to the model, so we stop paying for tokens nobody will read.
        if not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
        if isinstance(it, AsyncGenerator):
            await it.aclose()
