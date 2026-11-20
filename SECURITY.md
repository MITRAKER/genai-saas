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
  with a missing or short JWT secret. Each container gets only the settings
  it uses.

### The GitHub integration

- A webhook is read only after its HMAC-SHA256 signature checks out
  (compared in constant time), and a review is queued only for repositories
  of the accounts in `GITHUB_ALLOWED_ACCOUNTS`.
- The GitHub App asks for the least it needs. For each review it gets a token
  that expires within an hour and works on one repository only, with read
  access to code and write access to pull requests.
- Reviews are posted as comments only: the app never approves a pull request
  or requests changes, so a prompt injection hidden in a diff can't merge
  code.
- The model's output is untrusted text: `@mentions` in it are escaped, so a
  review can't notify people. Related code goes to the model marked as
  background, not as instructions.
- Every code search is filtered by repository, so one customer's code never
  appears in another customer's review. Uninstalling the app, or removing a
  repository from it, deletes the code indexed for it.
- Reviews per installation per day are capped (`MAX_REVIEWS_PER_DAY`), and
  every input has a size limit: webhook bodies, diffs, files, snippets.
