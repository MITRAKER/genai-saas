# Contributing

Thanks for helping! This guide is short on purpose: set up once, then every
change follows the same loop.

## Set up

You need Python 3.12, Docker, `make` and `openssl`.

```bash
python3.12 -m venv .venv && . .venv/bin/activate
make install          # tools, every service's dependencies, and the git hooks
make test-db          # Postgres with pgvector, for the github service's tests
make up smoke         # write .env, start the stack, run the end-to-end test
```

`make` on its own lists every command.

## The loop

1. Branch from `main`: `git switch -c fix-rate-limit-header`.
2. Write a failing test first, then the code that makes it pass.
3. `make check` runs what CI runs: lint and format (ruff), types
   (mypy `--strict`), the NASA/JPL rules, and the tests with the coverage gate.
   `make format` fixes most lint and format findings for you.
4. Commit. The pre-commit hooks check the files you changed in seconds.
5. Add a line to `CHANGELOG.md` under **Unreleased** if a user would notice
   the change.
6. Open a pull request. The template asks what changed, why, and how you
   tested it. It merges once CI is green and the code owner has reviewed it.

Write commit messages in the imperative, like the changes they describe:
"Add a usage endpoint", "Fix the Retry-After header". One logical change per
commit makes `git log` and reverts easy.

## The coding standard

The code follows the NASA/JPL "Power of Ten" rules, adapted to Python.
LEARNING.md (lesson 3) explains each rule and the tool that enforces it. In
short:

- Simple control flow: no recursion, few branches per function.
- Every loop has a fixed upper bound: no `while`, use `for _ in range(LIMIT)`.
- Bounded memory: limit every input (body size, diff size, list lengths).
- Short functions: about one screen, 60 lines at most.
- Check inputs at the boundary (Pydantic models with bounds) and raise a
  clear exception when something impossible happens. Don't use `assert` for
  this: `python -O` removes it.
- Smallest possible scope: no `global`, constants are `Final` or read-only.
- Never ignore a result or an error: no bare `except`, no unused awaitables.
- No code generated at run time: no `eval`, `exec` or dynamic imports.
- No `getattr` / `setattr` with computed names.
- Zero warnings: every tool runs in its strictest useful mode, and a warning
  fails the build.

If a rule is wrong for a specific line, don't silence it quietly. Add a
targeted `# noqa: CODE - reason` (or `# type: ignore[code]`) that explains why,
so a reviewer can agree or push back.

## Project layout

```
services/<name>/app     the service code (FastAPI)
services/<name>/tests   its tests (pytest)
scripts/                command-line helpers: the review client, the end-to-end
                        tests, and the fake GitHub they run against
tools/                  repository tools (the NASA/JPL checker)
docs/adr/               architecture decision records: why things are the way they are
```

## Releasing

1. Move the **Unreleased** notes in `CHANGELOG.md` under a new heading,
   `## [X.Y.Z] - YYYY-MM-DD`, and update the links at the bottom. Merge that.
2. Tag the merge commit and push the tag:
   `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z`.
3. The release workflow runs CI again, publishes the images to
   `ghcr.io/mitraker/genai-saas/<service>:X.Y.Z` and creates the GitHub
   release from the changelog.

Use [Semantic Versioning](https://semver.org/): bump the patch number for
fixes, the minor number for new features, the major number for breaking API
changes (after 1.0.0).

## Reporting problems

Bugs and ideas: open an issue with the matching template. Security problems:
please don't open an issue; follow SECURITY.md instead.
