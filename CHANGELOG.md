# Changelog

All notable changes to this project are written down here, newest first.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/): while the
version starts with `0.`, any release may change the API.

Add your change under **Unreleased** in the same pull request. Releasing moves
those notes under a new version heading (see CONTRIBUTING.md, "Releasing").

## [Unreleased]

### Added

- GitHub integration (`services/github`): a GitHub App reviews pull requests
  and posts each review as a comment. Webhooks are accepted only with a valid
  signature and only for the accounts in `GITHUB_ALLOWED_ACCOUNTS`, and are
  queued in Postgres. A worker fetches the diff, finds related code, asks the
  agent for a review and posts it: one review per commit, at most
  `MAX_REVIEWS_PER_DAY` per installation, retried with backoff when GitHub
  or the agent fails. Uninstalling the app deletes the code it indexed.
- Code embeddings in pgvector: the worker indexes each repository's code and
  gives the reviewer the code most related to the change, such as the
  callers of a changed function. Embeddings come from a local model (feature
  hashing, no API key) or from Voyage AI's `voyage-code-3`. Only files whose
  content changed are fetched and embedded again.
- The agent takes related code as context (`context` in
  `POST /reviews/stream`) and reviews for a GitHub App installation
  (`X-Installation-Id`) as well as for a user.
- `make demo`: the whole GitHub flow against a fake GitHub
  (`scripts/fake_github.py`), with no GitHub account. CI runs it too
  (`scripts/github_e2e.sh`).
- `make env` writes `.env` with fresh secrets, and `make test-db` starts the
  database the github service's tests need.
- Lessons 4 and 5 in LEARNING.md, and ADRs 0004 (Postgres as the job queue)
  and 0005 (pgvector for code embeddings).
- CI/CD quality gates on every pull request: pre-commit (ruff lint and
  format, shellcheck, hadolint, actionlint, zizmor), mypy `--strict`, tests
  with a 90% line and branch coverage gate, pip-audit, a gitleaks scan of the
  full git history, and the Docker Compose end-to-end test.
- NASA/JPL "Power of Ten" rules adapted to Python: `tools/jpl_check.py`
  checks the rules ruff and mypy don't cover; LEARNING.md lesson 3 maps
  every rule to the tool that enforces it.
- CodeQL code scanning for the Python code and the workflows.
- Release workflow: a `vX.Y.Z` tag publishes the three service images to
  GitHub Container Registry, with an SBOM and build provenance, and creates a
  GitHub release from this file.
- Dependabot for Python packages, Docker base images and GitHub Actions.
- `Makefile`, pre-commit hooks, `CONTRIBUTING.md`, `SECURITY.md`, code of
  conduct, issue and pull request templates, `CODEOWNERS`, architecture
  decision records in `docs/adr/`.
- Tests for the `scripts/review.py` command-line client.

### Changed

- Every service setting has bounds, so a bad `.env` value stops the service
  at startup with a clear message.
- Docker images use a pinned Python patch release and run as a numeric,
  unprivileged user; `.dockerignore` keeps tests and local files out.
- `scripts/review.py` reports errors in one line instead of a traceback.
- Upgraded every dependency to its current release; the gateway, the agent
  and the tests now use `httpx2`.
- Docker Compose pins the Postgres (pgvector 0.8.7) and Redis (7.4.12)
  images, and Dependabot keeps them current.
- `tools/jpl_check.py` no longer reports `re.compile()` as dynamic code; it
  still reports the built-in `compile()`.

### Fixed

- The gateway read request bodies of any size into memory. It now rejects
  bodies over `MAX_BODY_BYTES` (1 MiB) with `413`, with or without a
  `Content-Length` header.
- A rate-limit counter could be left without an expiry, blocking a user for
  good. The count and its expiry are now set in one transaction.
- Tokens without an expiry time were accepted. `exp` and `sub` are now
  required, and the claims' types are checked.
- Passwords longer than 72 bytes now get a clear `422` instead of a server
  error (bcrypt only reads 72 bytes).
- A review stream had no time limit. It now ends with a `timeout` error
  after `REVIEW_TIMEOUT_SECONDS` (600 by default).

### Security

- The gateway forwarded paths with `..` segments, so `/api/auth/../users/me`
  passed the public `/api/auth` route without a token or a rate limit and
  reached a protected route (core still refused it, for lack of a user id).
  The gateway now answers such paths with `400`.
- The agent wrote the `X-User-Id` header into its logs without checking it
  (log injection, found by CodeQL). It now accepts only a positive number
  (`422` otherwise) and logs the parsed number.
- Upgraded Starlette (through FastAPI) and PyJWT past published
  vulnerabilities that pip-audit found.
- The services refuse to start without a `JWT_SECRET` of at least 32
  characters; there is no built-in default secret anymore.
- Each container gets only the settings it uses instead of the whole `.env`:
  the gateway no longer sees the Anthropic API key, and the agent no longer
  sees the JWT secret.
- Clients can't pose as a GitHub installation: the gateway strips
  `X-Installation-Id` from incoming requests, like the other identity headers.

## [0.2.0] - 2026-11-17

### Added

- Agent service: streaming AI code reviews from Claude over Server-Sent
  Events (`POST /api/reviews/stream`), with keepalives, cancellation when the
  client disconnects, refusal fallbacks, and a fake provider for tests.
- `scripts/review.py`: `git diff | python3 scripts/review.py`.

## [0.1.0] - 2026-11-17

### Added

- Core service: sign up, log in, `GET /users/me`, with bcrypt and JWTs.
- API gateway: JWT authentication, per-plan rate limits in Redis, streaming
  reverse proxy.
- Docker Compose stack with Postgres (pgvector) and Redis, and CI.

[Unreleased]: https://github.com/MITRAKER/genai-saas/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/MITRAKER/genai-saas/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/MITRAKER/genai-saas/releases/tag/v0.1.0
