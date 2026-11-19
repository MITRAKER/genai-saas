"""API gateway: the only service exposed to the internet.

For every request it: 1) finds the upstream service from the path,
2) checks the JWT if the route needs it, 3) applies the plan's rate
limit, 4) forwards the request and streams the response back.
"""
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from starlette.background import BackgroundTask

from .auth import authenticate
from .config import ROUTES, settings
from .ratelimit import check_rate_limit

# Headers that describe a single hop and must not be forwarded.
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "upgrade", "host",
               "content-length", "x-user-id", "x-user-plan"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0))
    app.state.redis = Redis.from_url(settings.redis_url)
    yield
    await app.state.http.aclose()
    await app.state.redis.aclose()


app = FastAPI(title="API Gateway", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


def resolve(path: str):
    for prefix, (upstream, needs_auth) in ROUTES.items():
        if path == prefix or path.startswith(prefix + "/"):
            # /api/auth/login -> http://core:8001/auth/login
            return upstream + path.removeprefix("/api"), needs_auth
    raise HTTPException(404, "No route")


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(request: Request, rest: str):
    target, needs_auth = resolve(request.url.path)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    extra_headers = {}

    if needs_auth:
        claims = authenticate(request)
        plan = claims.get("plan", "free")
        extra_headers = await check_rate_limit(request.app.state.redis, claims["sub"], plan)
        # Upstreams trust these because only the gateway can reach them.
        headers["X-User-Id"] = claims["sub"]
        headers["X-User-Plan"] = plan

    upstream_req = request.app.state.http.build_request(
        request.method, target, params=request.query_params,
        headers=headers, content=await request.body(),
    )
    try:
        upstream = await request.app.state.http.send(upstream_req, stream=True)
    except httpx.RequestError:
        raise HTTPException(502, "Upstream service unavailable") from None

    # Stream the body through chunk by chunk, so AI responses (milestone 2)
    # reach the browser token by token instead of all at the end.
    resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_HEADERS}
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers={**resp_headers, **extra_headers},
        background=BackgroundTask(upstream.aclose),
    )
