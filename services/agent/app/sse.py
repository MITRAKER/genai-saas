"""Server-Sent Events (SSE) helpers.

SSE is a simple text format over a normal HTTP response that stays open:

    event: text
    data: {"text": "Hello"}

    event: done
    data: {...}

Each event ends with a blank line. Browsers read it with `EventSource`
(or fetch + a stream reader); curl shows it with `curl -N`.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

KEEPALIVE = ": keepalive\n\n"  # lines starting with ":" are comments; clients ignore them


def format_sse(event: str, data: dict) -> str:
    # json.dumps escapes newlines inside the text, so each event's data is
    # always a single line, which is what the SSE format expects.
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def with_keepalive(source: AsyncIterator[str], interval: float) -> AsyncIterator[str]:
    """Pass items through, and send a keepalive comment whenever the source
    has been quiet for `interval` seconds.

    Load balancers and proxies often close connections that send nothing
    for 30-60 seconds, and a model can think for longer than that.
    """
    it = aiter(source)
    pending = asyncio.ensure_future(anext(it))
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield KEEPALIVE
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            yield item
            pending = asyncio.ensure_future(anext(it))
    finally:
        # Runs when the stream ends or the client disconnects. Cancelling the
        # pending read and closing the source also closes the request to the
        # model, so we stop paying for tokens nobody will read.
        if not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
        if hasattr(it, "aclose"):
            await it.aclose()
