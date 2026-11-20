"""The code index: chunks of each repository's code with their embeddings,
stored in Postgres with pgvector, and the search for code related to a diff.

Files are identified by their blob sha, the hash of their content. A file
that didn't change since the last review has the same sha, so it is never
fetched or embedded again: after the first review, keeping the index current
costs a few API calls, not a few thousand.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from operator import attrgetter
from typing import Any, Final, cast

from sqlalchemy import CursorResult, Float, delete, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert

from .agent import Snippet
from .chunking import Chunk, SourceFile, chunk_file, diff_queries, fit, select_files
from .db import Sessions
from .embeddings import Embedder
from .github import RepositoryClient
from .jobs import Job
from .models import CodeChunk

FETCH_CONCURRENCY: Final = 8  # files downloaded at the same time
WRITE_BATCH: Final = 500  # rows per INSERT or DELETE
NEAREST_PER_QUERY: Final = 5
# Cosine distance runs from 0 (same direction) through 1 (unrelated) to 2.
# Past this, a "nearest" chunk is just the least unrelated one.
MAX_DISTANCE: Final = 0.9
# The HNSW index hands out the 40 nearest chunks (hnsw.ef_search) of *every*
# repository, and only then does Postgres drop the other repositories' ones.
# In a table shared by many customers, all 40 can belong to others, and a
# search finds nothing. An iterative scan keeps reading the index until
# enough rows pass the filter, up to hnsw.max_scan_tuples (20,000) rows.
ITERATIVE_SCAN: Final = text("SET LOCAL hnsw.iterative_scan = relaxed_order")


@dataclass(frozen=True)
class Limits:
    max_files: int
    max_file_bytes: int
    max_new_chunks: int
    max_snippets: int
    max_chars: int


@dataclass(frozen=True)
class IndexReport:
    files: int  # files indexed after this update
    candidates: int  # files worth indexing; more than `files` means a limit was hit
    new_files: int
    new_chunks: int
    tree_truncated: bool


async def read_chunks(
    repo: RepositoryClient, files: Sequence[SourceFile], max_chunks: int
) -> list[Chunk]:
    """Download files a few at a time and cut them into chunks, stopping once
    max_chunks is reached. Files are kept whole, so the last group can go past
    it by a little. Files left over are indexed by a later review."""
    chunks: list[Chunk] = []
    for start in range(0, len(files), FETCH_CONCURRENCY):
        if len(chunks) >= max_chunks:
            break
        group = files[start : start + FETCH_CONCURRENCY]
        contents = await asyncio.gather(*(repo.blob(file.blob_sha) for file in group))
        for file, data in zip(group, contents, strict=True):
            chunks.extend(chunk_file(file, data))
    return chunks


class CodeIndex:
    def __init__(self, sessions: Sessions, embedder: Embedder, limits: Limits) -> None:
        self.sessions = sessions
        self.embedder = embedder
        self.limits = limits

    async def update(self, repo: RepositoryClient, job: Job) -> IndexReport:
        """Make the index match the pull request's base commit."""
        tree = await repo.tree(job.base_sha)
        files, candidates = select_files(
            tree.tree, self.limits.max_files, self.limits.max_file_bytes
        )
        indexed = await self.indexed_files(job.repository_id)
        await self.remove(job.repository_id, indexed - {(f.path, f.blob_sha) for f in files})
        missing = [f for f in files if (f.path, f.blob_sha) not in indexed]
        chunks = await read_chunks(repo, missing, self.limits.max_new_chunks)
        texts = [chunk.embedding_text() for chunk in chunks]
        vectors = await self.embedder.embed(texts, "document")
        await self.add(job, chunks, vectors)
        new_files = len({chunk.path for chunk in chunks})
        return IndexReport(
            files=len(files) - len(missing) + new_files,
            candidates=candidates,
            new_files=new_files,
            new_chunks=len(chunks),
            tree_truncated=tree.truncated,
        )

    async def search(self, job: Job, diff: str) -> list[Snippet]:
        """Code from the repository that is close to what the diff changes."""
        queries = diff_queries(diff)
        if not queries or self.limits.max_snippets == 0:
            return []
        vectors = await self.embedder.embed(queries, "query")
        found: dict[tuple[str, int], Snippet] = {}
        for vector in vectors:
            for snippet in await self.nearest(job.repository_id, vector):
                key = (snippet.path, snippet.start_line)
                if key not in found or snippet.distance < found[key].distance:
                    found[key] = snippet
        ranked = sorted(found.values(), key=attrgetter("distance"))
        return fit(ranked, self.limits.max_snippets, self.limits.max_chars)

    async def nearest(self, repository_id: int, vector: list[float]) -> list[Snippet]:
        if not any(vector):  # the zero vector has no direction to compare
            return []
        # "<=>" is pgvector's cosine distance operator; the HNSW index makes
        # ORDER BY distance LIMIT k fast without comparing every row.
        distance = CodeChunk.embedding.op("<=>", return_type=Float)(vector).label("distance")
        statement = (
            select(
                CodeChunk.path,
                CodeChunk.start_line,
                CodeChunk.end_line,
                CodeChunk.content,
                distance,
            )
            .where(CodeChunk.repository_id == repository_id, CodeChunk.model == self.embedder.model)
            .order_by(distance)
            .limit(NEAREST_PER_QUERY)
        )
        async with self.sessions() as session, session.begin():
            await session.execute(ITERATIVE_SCAN)  # for this transaction only
            rows = (await session.execute(statement)).all()
        return [
            Snippet(row.path, row.start_line, row.end_line, row.content, float(row.distance))
            for row in rows
            if row.distance <= MAX_DISTANCE
        ]

    async def indexed_files(self, repository_id: int) -> set[tuple[str, str]]:
        statement = (
            select(CodeChunk.path, CodeChunk.blob_sha)
            .where(CodeChunk.repository_id == repository_id, CodeChunk.model == self.embedder.model)
            .distinct()
        )
        async with self.sessions() as session:
            rows = (await session.execute(statement)).all()
        return {(row.path, row.blob_sha) for row in rows}

    async def remove(self, repository_id: int, files: set[tuple[str, str]]) -> None:
        """Forget files that changed or disappeared since the last update."""
        pairs = sorted(files)
        async with self.sessions() as session, session.begin():
            for start in range(0, len(pairs), WRITE_BATCH):
                batch = pairs[start : start + WRITE_BATCH]
                await session.execute(
                    delete(CodeChunk).where(
                        CodeChunk.repository_id == repository_id,
                        CodeChunk.model == self.embedder.model,
                        tuple_(CodeChunk.path, CodeChunk.blob_sha).in_(batch),
                    )
                )

    async def add(self, job: Job, chunks: Sequence[Chunk], vectors: Sequence[list[float]]) -> None:
        rows = [
            {
                "installation_id": job.installation_id,
                "repository_id": job.repository_id,
                "model": self.embedder.model,
                "path": chunk.path,
                "blob_sha": chunk.blob_sha,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "content": chunk.content,
                "embedding": vector,
            }
            for chunk, vector in zip(chunks, vectors, strict=True)
            if any(vector)
        ]
        # Two workers indexing the same repository at once insert the same
        # rows; ON CONFLICT DO NOTHING keeps the first and drops the second.
        statement = insert(CodeChunk).on_conflict_do_nothing()
        async with self.sessions() as session, session.begin():
            for start in range(0, len(rows), WRITE_BATCH):
                await session.execute(statement, rows[start : start + WRITE_BATCH])


async def forget_installation(sessions: Sessions, installation_id: int) -> int:
    """Delete an installation's code. Code is our customers' property: when
    they uninstall the app, our copy goes too."""
    statement = delete(CodeChunk).where(CodeChunk.installation_id == installation_id)
    async with sessions() as session, session.begin():
        result = cast(CursorResult[Any], await session.execute(statement))
    return result.rowcount


async def forget_repositories(sessions: Sessions, repository_ids: Sequence[int]) -> int:
    """Delete the code of repositories removed from an installation."""
    statement = delete(CodeChunk).where(CodeChunk.repository_id.in_(repository_ids))
    async with sessions() as session, session.begin():
        result = cast(CursorResult[Any], await session.execute(statement))
    return result.rowcount
