"""API gateway: the only service exposed to the internet.

For every request it: 1) finds the upstream service from the path,
2) checks the JWT if the route needs it, 3) applies the plan's rate
limit, 4) reads the body (up to a size limit), 5) forwards the request
and streams the response back.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

import httpx2
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from starlette.background import BackgroundTask

from .auth import Identity, authenticate
from .config import ROUTES, settings
from .ratelimit import check_rate_limit

# Headers that describe a single hop and must not be forwarded.
HOP_HEADERS: Final = frozenset(
    {"connection", "keep-alive", "transfer-encoding", "upgrade", "host", "content-length"}
)
# Identity headers only the gateway (or, for X-Installation-Id, the GitHub
# worker inside our network) may set. Clients' copies are dropped.
IDENTITY_HEADERS: Final = frozenset({"x-user-id", "x-user-plan", "x-installation-id"})
# Path segments that mean "this folder" and "the folder above".
DOT_SEGMENTS: Final = frozenset({".", ".."})


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Clients are created once and reused (JPL rule 3: allocate up front).
    # read= is the longest silence allowed between two chunks of a response.
    # AI reviews stream for minutes; the agent sends a keepalive every 15s,
    # so 120s only trips when an upstream has really stalled.
    timeout = httpx2.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0)
    limits = httpx2.Limits(max_connections=settings.max_connections)
    async with httpx2.AsyncClient(timeout=timeout, limits=limits) as http:
        redis = Redis.from_url(settings.redis_url, max_connections=settings.max_connections)
        app.state.http, app.state.redis = http, redis
        yield
        await redis.aclose()


app = FastAPI(title="API Gateway", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def resolve(path: str) -> tuple[str, bool]:
    """/api/auth/login -> ("http://core:8001/auth/login", False)."""
    # "/api/auth/../users/me" starts with the public /api/auth prefix, but the
    # HTTP client resolves the ".." and would call /users/me: a protected
    # route, reached without a token or a rate limit. Never forward those.
    if any(segment in DOT_SEGMENTS for segment in path.split("/")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid path")
    for prefix, (upstream, needs_auth) in ROUTES.items():
        if path == prefix or path.startswith(prefix + "/"):
            return upstream + path.removeprefix("/api"), needs_auth
    raise HTTPException(status.HTTP_404_NOT_FOUND, "No route")


def too_large() -> HTTPException:
    return HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Request body too large")


async def read_body(request: Request) -> bytes:
    """Read the body, refusing anything over max_body_bytes (JPL rule 3).

    We check the declared Content-Length first, then count the bytes that
    actually arrive, because a client can lie or send no length at all.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > settings.max_body_bytes:
        raise too_large()
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > settings.max_body_bytes:
            raise too_large()
    return bytes(body)


def forward_headers(request: Request, identity: Identity | None) -> dict[str, str]:
    skip = HOP_HEADERS | IDENTITY_HEADERS
    headers = {k: v for k, v in request.headers.items() if k.lower() not in skip}
    if identity is not None:
        # Upstreams trust these because only the gateway can reach them.
        headers["x-user-id"] = identity.user_id
        headers["x-user-plan"] = identity.plan
    return headers


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(request: Request) -> StreamingResponse:
    target, needs_auth = resolve(request.url.path)
    identity = authenticate(request) if needs_auth else None
    rate_headers = await check_rate_limit(request.app.state.redis, identity) if identity else {}

    http: httpx2.AsyncClient = request.app.state.http
    upstream_request = http.build_request(
        request.method,
        target,
        params=list(request.query_params.multi_items()),
        headers=forward_headers(request, identity),
        content=await read_body(request),
    )
    try:
        upstream = await http.send(upstream_request, stream=True)
    except httpx2.RequestError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Upstream service unavailable") from None

    # Stream the body through chunk by chunk, so AI reviews reach the
    # browser token by token instead of all at the end.
    response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_HEADERS}
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers={**response_headers, **rate_headers},
        background=BackgroundTask(upstream.aclose),
    )
