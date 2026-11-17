import asyncio
import json

from app.sse import KEEPALIVE, format_sse, with_keepalive


def test_format_sse_is_one_data_line():
    frame = format_sse("text", {"text": "line one\nline two"})
    assert frame.startswith("event: text\ndata: ")
    assert frame.endswith("\n\n")
    data_line = frame.split("\n")[1]
    assert json.loads(data_line.removeprefix("data: ")) == {"text": "line one\nline two"}


async def collect(gen):
    return [item async for item in gen]


def test_keepalive_sent_while_source_is_quiet():
    async def slow():
        await asyncio.sleep(0.05)
        yield "a"

    items = asyncio.run(collect(with_keepalive(slow(), interval=0.01)))
    assert items[-1] == "a"
    assert KEEPALIVE in items


def test_items_pass_through_in_order():
    async def fast():
        for x in ["a", "b", "c"]:
            yield x

    assert asyncio.run(collect(with_keepalive(fast(), interval=1))) == ["a", "b", "c"]


def test_closing_early_closes_the_source():
    """A client that disconnects must stop the model call behind it."""
    state = {"closed": False}

    async def endless():
        try:
            while True:
                await asyncio.sleep(0.001)
                yield "x"
        finally:
            state["closed"] = True

    async def read_two_then_leave():
        gen = with_keepalive(endless(), interval=1)
        await anext(gen)
        await anext(gen)
        await gen.aclose()

    asyncio.run(read_two_then_leave())
    assert state["closed"]
