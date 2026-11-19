# 3. Follow the NASA/JPL "Power of Ten" rules, adapted to Python

- Status: Accepted
- Date: 2026-11-17

## Context

We want code that is easy to review and to analyze automatically, and
failures that are loud and early instead of silent and late. Gerard
Holzmann's "Power of Ten" rules (NASA/JPL, 2006) were written for C in
spacecraft software, but most of them are about exactly that, whatever the
language.

## Decision

All production code (services, scripts, tools) follows the ten rules as
adapted in LEARNING.md, lesson 3. They are enforced by machines, not by
memory: ruff (complexity, branches, statements, security rules), mypy
`--strict`, pytest with warnings as errors, and `tools/jpl_check.py` for the
rules no existing tool covers (recursion, unbounded loops, function length,
`global`, dynamic code, computed attribute names). CI fails on any violation.
Tests are exempt from the size and loop rules.

## Consequences

- Every loop and input has a bound, so a bug or a hostile input runs out of
  budget instead of running forever or exhausting memory.
- Some code is a little longer: an explicit `for _ in range(LIMIT)` instead
  of `while True`, a helper function instead of one long one.
- Rule 3 (no dynamic memory after startup) can't apply literally to Python;
  we keep its intent by bounding every input and creating pools at startup.
- Exceptions to a rule need a targeted, explained `noqa` that a reviewer can
  see and question.
