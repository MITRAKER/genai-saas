# 2. Stream reviews with Server-Sent Events

- Status: Accepted
- Date: 2026-11-17

## Context

A careful AI review takes from a few seconds to several minutes. Waiting for
the whole answer looks frozen to users and risks HTTP timeouts in proxies.
Data only needs to flow one way, from the server to the client.

## Decision

`POST /api/reviews/stream` answers with Server-Sent Events: `thinking`,
`text`, then `done` or `error`. The agent sends a keepalive comment after 15
seconds of silence and ends any review after `REVIEW_TIMEOUT_SECONDS`. When
the client disconnects, the request to the model is cancelled.

## Consequences

- The first words appear in about a second; proxies see regular traffic.
- SSE is plain HTTP, so the gateway, `curl -N` and browsers handle it as is;
  WebSockets would add a second protocol for no benefit.
- Errors that happen after the `200 OK` must travel inside the stream as an
  `error` event, so clients have to read events, not just status codes.
- Every hop must pass chunks through unbuffered (`X-Accel-Buffering: no`).
