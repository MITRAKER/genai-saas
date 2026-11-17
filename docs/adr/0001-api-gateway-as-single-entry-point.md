# 1. One API gateway is the only way in

- Status: Accepted
- Date: 2026-11-17

## Context

The product is split into services (core, agent, and later GitHub and
billing). Each needs authentication, rate limits and a public address. Doing
that in every service repeats security-critical code, and every copy is a
chance to get it wrong.

## Decision

Only the gateway publishes a port. It verifies the JWT, applies per-plan rate
limits and body-size limits, removes any identity headers the client sent,
and forwards the request with `X-User-Id` and `X-User-Plan`. The services
behind it are on a private network and trust those headers.

## Consequences

- Authentication and limits live in one small, heavily tested place.
- Services stay simple: they read a header instead of verifying tokens.
- The trust only holds while the services are unreachable from outside. Any
  new service must stay off the public network, or it must verify tokens
  itself.
- The gateway is on every request's path, so it must stay fast and stateless
  (JWTs instead of session lookups, counters in Redis).
