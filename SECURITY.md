# Security policy

## Reporting a vulnerability

Please **don't** open a public issue for a security problem. Report it
privately through GitHub instead:
[Security → Report a vulnerability](https://github.com/MITRAKER/genai-saas/security/advisories/new).

Include what an attacker could do, the steps to reproduce it, and the version
or commit you tested. You can expect a first answer within a week. Once a fix
is released, the advisory is published with credit to you, unless you'd
rather stay anonymous.

## Supported versions

This project is in early development (`0.x`). Only the latest release and the
`main` branch get security fixes.

## How the project protects itself

- Every pull request is checked by CodeQL code scanning, a secret scan of the
  full git history (gitleaks), a dependency audit against known
  vulnerabilities (pip-audit), and Dockerfile and workflow linters.
- Dependabot proposes dependency, base image and GitHub Action updates every
  week, after a 7-day cooldown.
- GitHub Actions are pinned to full commit SHAs and run with the minimum
  permissions they need.
- Only the API gateway is reachable from outside; it authenticates every
  request, limits its rate and size, and strips identity headers sent by
  clients. Containers run as an unprivileged user.
- Secrets come only from the environment, and the services refuse to start
  with a missing or short JWT secret.
