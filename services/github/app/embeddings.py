"""Embeddings: turning text into vectors whose distance means "related".

Two embedders with the same interface, like the agent's two LLM providers:

- HashingEmbedder runs right here, with no API key and no cost. It counts
  the identifiers in the text ("feature hashing"), so it finds code that
  *uses the same names*. Good enough for tests, CI and a first try.
- VoyageEmbedder calls Voyage AI's voyage-code-3, a model trained on code.
  It also finds code that *does the same thing* under different names.
"""

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Final, Literal, Protocol

import httpx2
from pydantic import BaseModel

from .models import EMBEDDING_DIMENSIONS

type EmbeddingKind = Literal["document", "query"]

IDENTIFIER: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Splits "parseHTTPResponse" and "parse_http_response" into the same words.
WORD: Final = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
MAX_IDENTIFIERS: Final = 20_000  # per text (JPL rule 3)
VOYAGE_BATCH_TEXTS: Final = 128  # Voyage allows 1,000 texts per request
VOYAGE_BATCH_CHARS: Final = 200_000  # and 120K tokens: about 360K characters of code


class EmbeddingError(Exception):
    pass


class Embedder(Protocol):
    @property
    def model(self) -> str: ...

    async def embed(self, texts: Sequence[str], kind: EmbeddingKind) -> list[list[float]]: ...


def features(text: str) -> list[str]:
    """Identifiers and the words inside them, lowercased."""
    found: list[str] = []
    for identifier in IDENTIFIER.findall(text)[:MAX_IDENTIFIERS]:
        found.append(identifier.lower())
        words = WORD.findall(identifier)
        if len(words) > 1:
            found.extend(word.lower() for word in words)
    return found


def hash_vector(text: str) -> list[float]:
    """Feature hashing: each feature adds +1 or -1 to one of the dimensions
    (both picked by a hash), then the vector is scaled to length 1, so the
    cosine distance compares *which* names appear, not how long the text is.
    A text without identifiers gets the zero vector: it matches nothing."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for feature in features(text):
        digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
        vector[index] += 1.0 if digest[4] % 2 else -1.0
    length = math.sqrt(sum(value * value for value in vector))
    return [value / length for value in vector] if length else vector


class HashingEmbedder:
    @property
    def model(self) -> str:
        return f"local-hashing-{EMBEDDING_DIMENSIONS}"

    async def embed(
        self,
        texts: Sequence[str],
        kind: EmbeddingKind,  # noqa: ARG002 - documents and queries are hashed alike
    ) -> list[list[float]]:
        return [hash_vector(text) for text in texts]


def batches(texts: Sequence[str], max_texts: int, max_chars: int) -> list[list[str]]:
    """Split texts into requests of at most max_texts texts and max_chars characters."""
    result: list[list[str]] = []
    current: list[str] = []
    size = 0
    for text in texts:
        if current and (len(current) == max_texts or size + len(text) > max_chars):
            result.append(current)
            current, size = [], 0
        current.append(text)
        size += len(text)
    if current:
        result.append(current)
    return result


class VoyageItem(BaseModel):
    index: int
    embedding: list[float]


class VoyageResponse(BaseModel):
    data: list[VoyageItem]


class VoyageEmbedder:
    """POST https://api.voyageai.com/v1/embeddings (the client brings the key)."""

    def __init__(self, http: httpx2.AsyncClient, model: str) -> None:
        self.http = http
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    async def embed(self, texts: Sequence[str], kind: EmbeddingKind) -> list[list[float]]:
        vectors: list[list[float]] = []
        for batch in batches(texts, VOYAGE_BATCH_TEXTS, VOYAGE_BATCH_CHARS):
            vectors.extend(await self.embed_batch(batch, kind))
        return vectors

    async def embed_batch(self, texts: list[str], kind: EmbeddingKind) -> list[list[float]]:
        # input_type tells the model whether this is code to find or a search
        # for code; it embeds the two a little differently, which helps search.
        request = {
            "input": texts,
            "model": self._model,
            "input_type": kind,
            "output_dimension": EMBEDDING_DIMENSIONS,
        }
        try:
            response = await self.http.post("/v1/embeddings", json=request)
        except httpx2.RequestError as error:
            raise EmbeddingError(f"could not reach Voyage AI ({type(error).__name__})") from error
        if not response.is_success:
            raise EmbeddingError(f"Voyage AI answered {response.status_code}")
        return parse_vectors(response, len(texts))


def parse_vectors(response: httpx2.Response, expected: int) -> list[list[float]]:
    """Check the answer before trusting it (JPL rule 7)."""
    try:
        items = VoyageResponse.model_validate(response.json()).data
    except ValueError as error:  # not JSON, or not the shape we expect
        raise EmbeddingError("Voyage AI sent an answer we could not read") from error
    items.sort(key=lambda item: item.index)
    # One vector per text, each in its place: the indexes are exactly 0..n-1.
    if [item.index for item in items] != list(range(expected)) or any(
        len(item.embedding) != EMBEDDING_DIMENSIONS for item in items
    ):
        raise EmbeddingError("Voyage AI sent the wrong number or size of vectors")
    return [item.embedding for item in items]
