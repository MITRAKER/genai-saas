import math
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

import fake_github
from app import codeindex
from app.chunking import Chunk, SourceFile, chunk_file, diff_queries
from app.codeindex import (
    MAX_DISTANCE,
    CodeIndex,
    Limits,
    forget_installation,
    forget_repositories,
)
from app.db import make_engine, make_sessions
from app.embeddings import HashingEmbedder
from app.github import Tree, TreeEntry
from app.models import EMBEDDING_DIMENSIONS, CodeChunk
from tests.conftest import TEST_DATABASE_URL
from tests.fakes import DEMO_JOB

pytestmark = pytest.mark.anyio

LIMITS = Limits(
    max_files=100, max_file_bytes=100_000, max_new_chunks=1_000, max_snippets=8, max_chars=40_000
)
CODE = {path: text for path, text in fake_github.FILES.items() if path.endswith(".py") and text}


class FakeRepo:
    """The two calls the index makes, answered from a dict of files."""

    def __init__(self, files: dict[str, str], truncated: bool = False) -> None:
        self.truncated = truncated
        self.fetched: list[str] = []
        self.change(files)

    def change(self, files: dict[str, str]) -> None:
        contents = {path: text.encode() for path, text in files.items()}
        self.blobs = {fake_github.git_blob_sha(data): data for data in contents.values()}
        self.entries = [
            TreeEntry(path=path, type="blob", sha=fake_github.git_blob_sha(data), size=len(data))
            for path, data in contents.items()
        ]

    async def tree(self, commit_sha: str) -> Tree:
        return Tree(tree=self.entries, truncated=self.truncated)

    async def blob(self, sha: str) -> bytes:
        self.fetched.append(sha)
        return self.blobs[sha]


class OtherModel(HashingEmbedder):
    @property
    def model(self) -> str:
        return "another-model"


class ZeroEmbedder(HashingEmbedder):
    """Every text without meaning: the zero vector."""

    async def embed(self, texts, kind):
        return [[0.0] * EMBEDDING_DIMENSIONS for _ in texts]


async def stored(sessions) -> list[tuple[int, str, str]]:
    statement = select(CodeChunk.repository_id, CodeChunk.path, CodeChunk.blob_sha).order_by(
        CodeChunk.repository_id, CodeChunk.path
    )
    async with sessions() as session:
        return [tuple(r) for r in (await session.execute(statement)).all()]


async def test_the_first_update_indexes_every_code_file(sessions):
    repo = FakeRepo(fake_github.FILES)
    report = await CodeIndex(sessions, HashingEmbedder(), LIMITS).update(repo, DEMO_JOB)
    # README.md is not code, and shop/__init__.py is empty.
    assert (report.files, report.candidates, report.new_files, report.new_chunks) == (2, 2, 2, 2)
    assert report.tree_truncated is False
    assert [path for _, path, _ in await stored(sessions)] == ["shop/cart.py", "shop/pricing.py"]
    async with sessions() as session:
        chunk = await session.scalar(select(CodeChunk).where(CodeChunk.path == "shop/cart.py"))
    assert chunk is not None
    assert (chunk.installation_id, chunk.model) == (2002, "local-hashing-1024")
    assert (chunk.start_line, chunk.end_line) == (1, 6)
    assert chunk.content == CODE["shop/cart.py"].rstrip("\n")


async def test_unchanged_files_are_never_fetched_again(sessions):
    repo = FakeRepo(fake_github.FILES)
    index = CodeIndex(sessions, HashingEmbedder(), LIMITS)
    await index.update(repo, DEMO_JOB)
    report = await index.update(repo, DEMO_JOB)
    assert (report.files, report.new_files, report.new_chunks) == (2, 0, 0)
    assert len(repo.fetched) == 2


async def test_changed_and_deleted_files_are_replaced_and_forgotten(sessions):
    repo = FakeRepo(CODE)
    index = CodeIndex(sessions, HashingEmbedder(), LIMITS)
    await index.update(repo, DEMO_JOB)
    changed = CODE["shop/pricing.py"] + "\n\ndef refund(price):\n    return -price\n"
    repo.change({"shop/pricing.py": changed, "shop/orders.py": "def order_total(): ...\n"})

    report = await index.update(repo, DEMO_JOB)
    assert (report.files, report.new_files) == (2, 2)
    assert await stored(sessions) == [
        (1001, "shop/orders.py", fake_github.git_blob_sha(b"def order_total(): ...\n")),
        (1001, "shop/pricing.py", fake_github.git_blob_sha(changed.encode())),
    ]


async def test_a_big_repository_is_indexed_over_several_reviews(sessions, monkeypatch):
    monkeypatch.setattr(codeindex, "FETCH_CONCURRENCY", 1)
    repo = FakeRepo(CODE)
    index = CodeIndex(sessions, HashingEmbedder(), replace(LIMITS, max_new_chunks=1))
    first = await index.update(repo, DEMO_JOB)
    assert (first.files, first.candidates, first.new_files) == (1, 2, 1)
    second = await index.update(repo, DEMO_JOB)
    assert (second.files, second.candidates, second.new_files) == (2, 2, 1)


async def test_files_past_the_limits_are_counted_not_indexed(sessions):
    repo = FakeRepo(CODE, truncated=True)
    report = await CodeIndex(sessions, HashingEmbedder(), replace(LIMITS, max_files=1)).update(
        repo, DEMO_JOB
    )
    assert (report.files, report.candidates) == (1, 2)
    assert report.tree_truncated is True


async def test_search_finds_the_code_that_uses_the_changed_function(sessions):
    index = CodeIndex(sessions, HashingEmbedder(), LIMITS)
    await index.update(FakeRepo(fake_github.FILES), DEMO_JOB)
    snippets = await index.search(DEMO_JOB, fake_github.DIFF)
    assert {snippet.path for snippet in snippets} == {"shop/cart.py", "shop/pricing.py"}
    distances = [snippet.distance for snippet in snippets]
    assert distances == sorted(distances)
    assert all(distance <= MAX_DISTANCE for distance in distances)


async def test_code_found_by_several_queries_counts_once_at_its_closest(sessions):
    index = CodeIndex(sessions, HashingEmbedder(), LIMITS)
    await index.update(FakeRepo(fake_github.FILES), DEMO_JOB)
    cart_change = [
        "--- a/shop/cart.py",
        "+++ b/shop/cart.py",
        "@@ -4 +4 @@",
        "-def cart_total(items):",
        "+def cart_total(items, member=False):",
        "",
    ]
    diff = fake_github.DIFF + "\n".join(cart_change)
    snippets = await index.search(DEMO_JOB, diff)

    closest: dict[str, float] = {}
    for vector in await index.embedder.embed(diff_queries(diff), "query"):
        for found in await index.nearest(DEMO_JOB.repository_id, vector):
            closest[found.path] = min(closest.get(found.path, 2.0), found.distance)
    assert len(closest) == 2
    assert {snippet.path: snippet.distance for snippet in snippets} == closest


async def test_search_never_returns_another_repositorys_code(sessions):
    index = CodeIndex(sessions, HashingEmbedder(), LIMITS)
    await index.update(FakeRepo(fake_github.FILES), DEMO_JOB)
    other_repository = replace(DEMO_JOB, repository_id=9999)
    assert await index.search(other_repository, fake_github.DIFF) == []


async def test_search_never_mixes_vectors_of_two_models(sessions):
    await CodeIndex(sessions, OtherModel(), LIMITS).update(FakeRepo(fake_github.FILES), DEMO_JOB)
    index = CodeIndex(sessions, HashingEmbedder(), LIMITS)
    assert await index.search(DEMO_JOB, fake_github.DIFF) == []


def direction(*leading: float) -> list[float]:
    """A vector of length 1 that starts with these numbers, zeros after."""
    vector = [*leading, *[0.0] * (EMBEDDING_DIMENSIONS - len(leading))]
    length = math.sqrt(sum(value * value for value in vector))
    return [value / length for value in vector]


def chunk(path: str) -> Chunk:
    return Chunk(path=path, blob_sha="b" * 40, start_line=1, end_line=1, content="x = 1")


async def test_a_small_repository_is_found_next_to_a_big_one(sessions):
    """In a big table, Postgres reads the HNSW index first and filters by
    repository after. With sequential scans and sorts turned off, it does
    that in this small test table too."""
    url = make_url(TEST_DATABASE_URL).update_query_dict(
        {"options": "-c enable_seqscan=off -c enable_sort=off"}
    )
    engine = make_engine(url.render_as_string(hide_password=False))
    index = CodeIndex(make_sessions(engine), HashingEmbedder(), LIMITS)
    try:
        # A big repository with 100 chunks that are all closer to the query
        # than the small repository's only chunk.
        big = replace(DEMO_JOB, repository_id=9999)
        chunks = [chunk(f"big/{n}.py") for n in range(100)]
        await index.add(big, chunks, [direction(1.0, 0.001 * n) for n in range(100)])
        await index.add(DEMO_JOB, [chunk("small.py")], [direction(1.0, 0.8)])

        found = await index.nearest(DEMO_JOB.repository_id, direction(1.0))
    finally:
        await engine.dispose()
    assert [snippet.path for snippet in found] == ["small.py"]


async def test_unrelated_code_is_not_context(sessions):
    index = CodeIndex(sessions, HashingEmbedder(), LIMITS)
    await index.update(FakeRepo(fake_github.FILES), DEMO_JOB)
    diff = "--- a/net/server.go\n+++ b/net/server.go\n@@ -1 +1 @@\n-listen(socket)\n+bind(port)\n"
    assert await index.search(DEMO_JOB, diff) == []


async def test_search_with_nothing_to_search_for(sessions):
    index = CodeIndex(sessions, HashingEmbedder(), replace(LIMITS, max_snippets=0))
    assert await index.search(DEMO_JOB, fake_github.DIFF) == []
    assert await CodeIndex(sessions, HashingEmbedder(), LIMITS).search(DEMO_JOB, "") == []


async def test_zero_vectors_are_neither_stored_nor_searched(sessions):
    index = CodeIndex(sessions, ZeroEmbedder(), LIMITS)
    report = await index.update(FakeRepo(CODE), DEMO_JOB)
    assert report.new_chunks == 2
    assert await stored(sessions) == []
    assert await index.search(DEMO_JOB, fake_github.DIFF) == []


async def test_two_workers_indexing_at_once_keep_one_copy(sessions):
    index = CodeIndex(sessions, HashingEmbedder(), LIMITS)
    chunks = chunk_file(SourceFile("a.py", "a" * 40, 10), b"def a():\n    return 1\n")
    vectors = await index.embedder.embed([c.embedding_text() for c in chunks], "document")
    await index.add(DEMO_JOB, chunks, vectors)
    await index.add(DEMO_JOB, chunks, vectors)
    assert await stored(sessions) == [(1001, "a.py", "a" * 40)]


async def test_uninstalling_deletes_the_code(sessions):
    index = CodeIndex(sessions, HashingEmbedder(), LIMITS)
    repo = FakeRepo(CODE)
    await index.update(repo, DEMO_JOB)  # repository 1001, installation 2002
    await index.update(repo, replace(DEMO_JOB, repository_id=1002))
    await index.update(repo, replace(DEMO_JOB, repository_id=3001, installation_id=3003))

    assert await forget_repositories(sessions, [1001]) == 2
    assert {repository for repository, _, _ in await stored(sessions)} == {1002, 3001}
    assert await forget_installation(sessions, 2002) == 2
    assert {repository for repository, _, _ in await stored(sessions)} == {3001}
