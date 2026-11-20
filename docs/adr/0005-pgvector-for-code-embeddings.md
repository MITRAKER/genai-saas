# 5. Store code embeddings in Postgres with pgvector

- Status: Accepted
- Date: 2026-11-19

## Context

A diff shows what changed, not what depends on it. A reviewer who sees a
function change but not its callers misses the bugs that matter most. The
whole repository doesn't fit in a prompt, and would cost too much if it did,
so the worker must find the few pieces of code related to each change.

That is a nearest-neighbour search over embeddings (vectors that place
related code close together), always limited to one repository: one
customer's code must never appear in another customer's review.

The options: a dedicated vector database (Pinecone, Qdrant, Weaviate), one
more system to run, secure, back up and keep in sync with our own data; or
the pgvector extension in the Postgres we already have.

## Decision

Embeddings live in Postgres, in the `code_chunks` table, with pgvector.

- Code is cut into chunks of 60 lines that overlap by 10. Each chunk is
  stored with its repository, its file's blob SHA, and the name of the
  embedding model.
- An HNSW index over cosine distance finds the nearest chunks without
  comparing the query with every row. Every search filters by repository and
  model, with pgvector's iterative index scans turned on, so other
  repositories' chunks can't crowd out this one's.
- The index follows each pull request's base commit, so it holds merged
  code only. A file whose blob SHA hasn't changed is never fetched or
  embedded again.
- Two embedders share one interface: local feature hashing (no API key, the
  default and what CI uses) and Voyage AI's `voyage-code-3`.

## Consequences

- One database to run and back up. A repository's code and its vectors are
  deleted in one transaction when the app is uninstalled.
- Tenant isolation is a `WHERE` clause that every query has, and that the
  tests check.
- A filtered HNSW search needs pgvector 0.8 or later (iterative scans), so
  the pgvector version is pinned in `docker-compose.yml`.
- Vectors from different models can't be compared. Switching models means
  embedding every repository again; the `model` column keeps the two sets
  apart until then.
- HNSW keeps its graph in memory to be fast. At tens of millions of chunks,
  this decision should be revisited.
