import asyncio
import json

from app import sse
from app.sse import KEEPALIVE, format_sse, with_keepalive


def test_format_sse_is_one_data_line():
    frame = format_sse("text", {"text": "line one\nline two"})
    assert frame.startswith("event: text\ndata: ")
    assert frame.endswith("\n\n")
    data_line = frame.split("\n")[1]
    assert json.loads(data_line.removeprefix("data: ")) == {"text": "line one\nline two"}


async def collect(gen):
    return [item async for item in gen]


async def slow_source(seconds, item="a"):
    await asyncio.sleep(seconds)
    yield item


def test_keepalive_sent_while_source_is_quiet():
    items = asyncio.run(collect(with_keepalive(slow_source(0.05), interval=0.01, max_seconds=5)))
    assert items[-1] == "a"
    assert KEEPALIVE in items


def test_items_pass_through_in_order():
    async def fast():
        for x in ["a", "b", "c"]:
            yield x

    items = asyncio.run(collect(with_keepalive(fast(), interval=1, max_seconds=5)))
    assert items == ["a", "b", "c"]


def test_deadline_ends_stream_with_timeout_error():
    """JPL rule 2: no stream may run forever, however slow the source."""
    items = asyncio.run(collect(with_keepalive(slow_source(10), interval=0.01, max_seconds=0.05)))
    assert items[-1].startswith("event: error")
    assert '"timeout"' in items[-1]


def test_step_limit_ends_stream(monkeypatch):
    monkeypatch.setattr(sse, "MAX_STEPS", 3)

    async def many():
        for i in range(10):
            yield str(i)

    items = asyncio.run(collect(with_keepalive(many(), interval=1, max_seconds=5)))
    assert items[:3] == ["0", "1", "2"]
    assert '"too_long"' in items[-1]


def test_closing_early_closes_the_source():
    """A client that disconnects must stop the model call behind it."""
    state = {"closed": False}

    async def endless():
        try:
            for _ in range(1_000_000):
                await asyncio.sleep(0.001)
                yield "x"
        finally:
            state["closed"] = True

    async def read_then_leave():
        gen = with_keepalive(endless(), interval=1, max_seconds=5)
        await anext(gen)
        await anext(gen)
        await gen.aclose()

    asyncio.run(read_then_leave())
    assert state["closed"]


def test_any_async_iterator_works_as_a_source():
    """The source doesn't have to be a generator: anything with __anext__ will do."""

    class Countdown:
        def __init__(self):
            self.left = 2

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.left == 0:
                raise StopAsyncIteration
            self.left -= 1
            return str(self.left)

    items = asyncio.run(collect(with_keepalive(Countdown(), interval=1, max_seconds=5)))
    assert items == ["1", "0"]
