import json
import math

import httpx2
import pytest

from app import embeddings
from app.embeddings import (
    EmbeddingError,
    HashingEmbedder,
    VoyageEmbedder,
    batches,
    features,
    hash_vector,
)
from app.models import EMBEDDING_DIMENSIONS
from tests.fakes import answering, unreachable

pytestmark = pytest.mark.anyio


def distance(a: list[float], b: list[float]) -> float:
    """Cosine distance; both vectors have length 1 already."""
    return 1 - sum(x * y for x, y in zip(a, b, strict=True))


def test_features_split_identifiers_into_words():
    assert features("parseHTTPResponse = parse_http_response(x2) + 7") == [
        "parsehttpresponse",
        "parse",
        "http",
        "response",
        "parse_http_response",
        "parse",
        "http",
        "response",
        "x2",
        "x",
        "2",
    ]


def test_features_are_bounded(monkeypatch):
    monkeypatch.setattr(embeddings, "MAX_IDENTIFIERS", 2)
    assert features("a b c d") == ["a", "b"]


def test_hash_vectors_have_length_one_and_never_change():
    text = "def total(price): return price * TAX"
    vector = hash_vector(text)
    assert len(vector) == EMBEDDING_DIMENSIONS
    assert math.isclose(sum(value * value for value in vector), 1.0)
    assert vector == hash_vector(text)


def test_text_without_identifiers_is_the_zero_vector():
    assert not any(hash_vector("1 + 2 == 3; // ** ()"))


def test_code_with_the_same_names_is_closer_than_unrelated_code():
    query = hash_vector("price_with_tax(price)")
    related = hash_vector(
        "def cart_total(items): return sum(price_with_tax(i.price) for i in items)"
    )
    unrelated = hash_vector("class HttpServer: def listen(self, port): socket.bind(port)")
    assert distance(query, related) < 0.5 < distance(query, unrelated)


async def test_hashing_embedder():
    embedder = HashingEmbedder()
    assert embedder.model == "local-hashing-1024"
    assert await embedder.embed(["a b", "c"], "query") == [hash_vector("a b"), hash_vector("c")]


def test_batches_split_by_count_and_by_size():
    assert batches(["a", "b", "c"], max_texts=2, max_chars=100) == [["a", "b"], ["c"]]
    assert batches(["aaa", "bb", "c"], max_texts=10, max_chars=4) == [["aaa"], ["bb", "c"]]
    # A text over the size limit still goes, alone.
    assert batches(["x" * 10, "y"], max_texts=10, max_chars=4) == [["x" * 10], ["y"]]
    assert batches([], max_texts=2, max_chars=10) == []


def voyage_answer(request: httpx2.Request) -> httpx2.Response:
    """Like Voyage AI, but in reverse order: the client must sort by index."""
    texts = json.loads(request.content)["input"]
    data = [
        {"object": "embedding", "index": i, "embedding": [float(i + 1)] * EMBEDDING_DIMENSIONS}
        for i in reversed(range(len(texts)))
    ]
    return httpx2.Response(200, json={"object": "list", "data": data, "usage": {}})


async def test_voyage_embeds_with_the_kind_and_keeps_the_order():
    seen = []

    def handler(request):
        seen.append(request)
        return voyage_answer(request)

    async with answering(handler) as http:
        embedder = VoyageEmbedder(http, "voyage-code-3")
        vectors = await embedder.embed(["def a(): pass", "def b(): pass"], "document")
    assert embedder.model == "voyage-code-3"
    assert [vector[0] for vector in vectors] == [1.0, 2.0]
    [request] = seen
    assert request.url.path == "/v1/embeddings"
    assert json.loads(request.content) == {
        "input": ["def a(): pass", "def b(): pass"],
        "model": "voyage-code-3",
        "input_type": "document",
        "output_dimension": EMBEDDING_DIMENSIONS,
    }


async def test_voyage_sends_big_jobs_in_batches(monkeypatch):
    monkeypatch.setattr(embeddings, "VOYAGE_BATCH_TEXTS", 2)
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["input"])
        return voyage_answer(request)

    async with answering(handler) as http:
        vectors = await VoyageEmbedder(http, "voyage-code-3").embed(list("abcde"), "query")
    assert seen == [["a", "b"], ["c", "d"], ["e"]]
    assert [vector[0] for vector in vectors] == [1.0, 2.0, 1.0, 2.0, 1.0]


def vectors(*indexes: int, size: int = EMBEDDING_DIMENSIONS) -> dict:
    return {"data": [{"index": i, "embedding": [0.5] * size} for i in indexes]}


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (httpx2.Response(500), "Voyage AI answered 500"),
        (httpx2.Response(401, json={"detail": "bad key"}), "Voyage AI answered 401"),
        (httpx2.Response(200, content=b"not json"), "could not read"),
        (httpx2.Response(200, json={"data": "nope"}), "could not read"),
        (httpx2.Response(200, json=vectors(0)), "wrong number or size"),
        (httpx2.Response(200, json=vectors(0, 0)), "wrong number or size"),
        (httpx2.Response(200, json=vectors(0, 1, size=3)), "wrong number or size"),
    ],
)
async def test_voyage_answers_are_checked(answer, message):
    async with answering(lambda _: answer) as http:
        with pytest.raises(EmbeddingError, match=message):
            await VoyageEmbedder(http, "voyage-code-3").embed(["a", "b"], "query")


async def test_voyage_unreachable():
    async with answering(unreachable) as http:
        with pytest.raises(EmbeddingError, match=r"could not reach Voyage AI \(ConnectError\)"):
            await VoyageEmbedder(http, "voyage-code-3").embed(["a"], "query")
