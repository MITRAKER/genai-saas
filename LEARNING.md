# Learning log: how this project is built

Each milestone adds a lesson. Every lesson has the same parts: **what we
built**, **why it is designed this way**, **key concepts**, and **try it
yourself**. Read the code next to each lesson; the files are small on purpose.

---

## Lesson 1: Accounts, an API gateway, and CI

### What we built

```
services/
  core/      FastAPI app: users table, sign up, log in, /users/me
  gateway/   FastAPI app: the single front door to every other service
docker-compose.yml   Postgres, Redis, core, gateway on one private network
.github/workflows/ci.yml   lint + tests on every push, then an end-to-end run
scripts/smoke_test.sh      signs up and calls a protected route through the gateway
```

A request travels like this:

```
client ──► gateway :8000 ──► core :8001 ──► Postgres
             │
             └──► Redis (rate-limit counters)
```

1. The client calls `POST /api/auth/signup`. The gateway sees `/api/auth` is
   public, forwards it to core as `/auth/signup`.
2. Core hashes the password, stores the user, and returns a **JWT**.
3. The client calls `GET /api/users/me` with `Authorization: Bearer <jwt>`.
4. The gateway verifies the JWT, counts the request in Redis, adds
   `X-User-Id: 42`, and forwards it. Core reads the header and loads the user.

### Why it is designed this way

**Why a gateway at all?** Later we will have four services. Without a gateway,
each would need its own login check, its own rate limiter and its own public
URL. The gateway does those once, in one place (`services/gateway/app/main.py`).
Services behind it stay simple: they just read `X-User-Id`.

**Why is it safe for core to trust `X-User-Id`?** Because core has no public
port (look at `docker-compose.yml`: only the gateway has `ports:`). And the
gateway *removes* any `X-User-Id` the client sends before adding its own;
`test_client_cannot_spoof_user_id` proves it. Forget either and anyone could
pretend to be anyone.

**Why JWTs instead of sessions?** A JWT is a signed note: "user 42, plan free,
valid until 10:15". The gateway checks the signature with a shared secret and
knows who you are **without a database call**. That keeps the gateway fast and
stateless, so we can run many copies of it. The trade-off: you can't easily
revoke a JWT before it expires, which is why tokens are short-lived (60 min).

**Why Redis for rate limits?** If we counted requests in the gateway's memory,
two gateway copies would each allow 30/min, giving you 60. Redis is shared,
and `INCR` is atomic, so the count is correct no matter how many gateways run.

**Why Postgres with pgvector?** Postgres for normal data (users, projects).
The pgvector extension lets the same database store embeddings for code
search in milestone 3, so we don't need a separate vector database yet.

### Key concepts

- **Password hashing (bcrypt)** — `services/core/app/security.py`. Never store
  passwords. Store a slow, salted hash. "Slow" makes guessing expensive;
  "salted" means two people with the same password get different hashes.
- **Don't leak which emails exist** — login returns the same error for "no
  such user" and "wrong password" (`core/app/main.py`).
- **Validation at the edge** — `schemas.py` uses Pydantic: bad emails or
  passwords under 8 characters are rejected with `422` before our code runs.
- **Reverse proxy and streaming** — the gateway forwards bodies chunk by chunk
  (`StreamingResponse(upstream.aiter_raw())`). Today that's a small JSON
  response; in milestone 2 it lets AI answers appear word by word.
- **Fixed-window rate limiting** — `gateway/app/ratelimit.py`. One counter per
  user per minute, key `rl:<user>:<minute>`, auto-expiring. It returns standard
  `X-RateLimit-*` and `Retry-After` headers and status `429`.
- **12-factor config** — all settings come from environment variables
  (`config.py` + `.env`). The same image runs in dev, CI and production.
- **Testing without real infrastructure** — core tests use in-memory SQLite;
  gateway tests swap Redis for `fakeredis` and the core API for an
  `httpx.MockTransport`. Fast unit tests, then CI's integration job checks the
  real thing with Docker.
- **Containers as non-root** — each `Dockerfile` creates `appuser`, so a bug
  in our code can't modify the container's system files.

### Try it yourself

1. Run the stack and the smoke test (see README). Then call
   `/api/users/me` 31 times in a minute and watch the `429` appear.
2. Paste a token into <https://jwt.io>. You can *read* the claims; notice that
   anyone can. Never put secrets in a JWT.
3. Change one character of the token and call the API again. Why does the
   gateway reject it?
4. Exercise: add a `GET /api/users/me/usage` route that returns how many
   requests you've made this minute. Hint: it belongs in the gateway, since
   the gateway owns Redis. Write a test first.
5. Question to think about: the rate limit is a *fixed* window. How could a
   user send 60 requests in about 2 seconds? (Look up "sliding window log".)

---

*Next lesson: the AI agent service and streaming responses.*
