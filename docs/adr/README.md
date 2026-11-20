# Architecture decision records

An architecture decision record (ADR) is a short note about one important
decision: the situation, what we chose, and what it costs us. Code shows
*what* the system does; ADRs keep *why*, so nobody has to guess (or repeat an
old debate) a year later.

Each record has a number and never changes once accepted. To change a
decision, write a new record that supersedes the old one.

| # | Decision | Status |
|---|---|---|
| [0001](0001-api-gateway-as-single-entry-point.md) | One API gateway is the only way in | Accepted |
| [0002](0002-stream-reviews-with-server-sent-events.md) | Stream reviews with Server-Sent Events | Accepted |
| [0003](0003-nasa-jpl-power-of-ten-coding-standard.md) | Follow the NASA/JPL "Power of Ten" rules, adapted to Python | Accepted |
| [0004](0004-postgres-as-the-review-job-queue.md) | Use Postgres as the job queue for GitHub reviews | Accepted |
| [0005](0005-pgvector-for-code-embeddings.md) | Store code embeddings in Postgres with pgvector | Accepted |

New record: copy the newest file, take the next number, and fill in
**Context**, **Decision** and **Consequences**.
