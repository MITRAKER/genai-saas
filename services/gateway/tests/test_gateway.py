import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import fakeredis
import httpx2
import jwt
import pytest
from fastapi.testclient import TestClient

from app import main
from app.auth import Identity
from app.config import settings
from app.ratelimit import check_rate_limit, count_request


def fake_upstream(request: httpx2.Request) -> httpx2.Response:
    """Stands in for the services behind the gateway and echoes what it received."""
    body = json.dumps(
        {
            "host": request.url.host,
            "path": request.url.path,
            "query": str(request.url.query, "ascii"),
            "user": request.headers.get("x-user-id"),
        }
    )
    # A real network response is a stream, so build one the gateway can stream.
    return httpx2.Response(
        200,
        headers={"content-type": "application/json"},
        stream=httpx2.ByteStream(body.encode()),
    )


def unreachable(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("connection refused", request=request)


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        mock = httpx2.AsyncClient(transport=httpx2.MockTransport(fake_upstream))
        main.app.state.http = mock
        main.app.state.redis = fakeredis.FakeAsyncRedis()
        yield c
        asyncio.run(mock.aclose())


def token(sub="42", plan="free", minutes=5, **extra):
    claims = {"sub": sub, "plan": plan, "exp": datetime.now(UTC) + timedelta(minutes=minutes)}
    return jwt.encode({**claims, **extra}, settings.jwt_secret, algorithm="HS256")


def auth(t):
    return {"Authorization": f"Bearer {t}"}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_public_route_needs_no_token(client):
    r = client.post("/api/auth/login", json={})
    assert r.status_code == 200
    assert r.json()["path"] == "/auth/login"


def test_protected_route_requires_token(client):
    assert client.get("/api/users/me").status_code == 401


def test_valid_token_forwards_user_id_and_query(client):
    r = client.get("/api/users/me?a=1&a=2", headers=auth(token()))
    assert r.status_code == 200
    assert r.json() == {"host": "localhost", "path": "/users/me", "query": "a=1&a=2", "user": "42"}
    assert r.headers["x-ratelimit-limit"] == str(settings.rate_limit_free)


def test_client_cannot_spoof_user_id(client):
    r = client.get("/api/users/me", headers={**auth(token()), "X-User-Id": "1"})
    assert r.json()["user"] == "42"


@pytest.mark.parametrize(
    "bad_token",
    [
        token(minutes=-1),  # expired
        jwt.encode({"sub": "1", "plan": "pro"}, "x" * 40, algorithm="HS256"),  # forged
        jwt.encode({"sub": "1"}, settings.jwt_secret, algorithm="HS256"),  # no expiry
        token(plan="platinum"),  # unknown plan
        jwt.encode(  # user id must be a string
            {"sub": 7, "exp": datetime.now(UTC) + timedelta(minutes=5)},
            settings.jwt_secret,
            algorithm="HS256",
        ),
    ],
)
def test_bad_tokens_rejected(client, bad_token):
    assert client.get("/api/users/me", headers=auth(bad_token)).status_code == 401


def test_wrong_auth_scheme_rejected(client):
    r = client.get("/api/users/me", headers={"Authorization": f"Basic {token()}"})
    assert r.status_code == 401


def test_rate_limit_per_plan(client, monkeypatch):
    monkeypatch.setitem(main.check_rate_limit.__globals__["LIMITS"], "free", 3)
    t = token(sub="7")
    codes = [client.get("/api/users/me", headers=auth(t)).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_unknown_route_404(client):
    assert client.get("/api/nope").status_code == 404


@pytest.mark.parametrize(
    "path", ["/api/auth/../users/me", "/api/auth/./login", "/api/users/me/..", "/api/auth/.."]
)
def test_paths_with_dot_segments_are_refused(path):
    """A public prefix must not lead to a protected route (path traversal).

    The test client would clean up the ".." itself before sending, so this
    calls the router directly; the smoke test checks it over real HTTP.
    """
    with pytest.raises(main.HTTPException) as exc:
        main.resolve(path)
    assert exc.value.status_code == 400


def test_dots_inside_names_are_fine():
    assert main.resolve("/api/auth/login.v2") == ("http://localhost:8001/auth/login.v2", False)


def test_reviews_route_to_agent_service(client, monkeypatch):
    routes = {**main.ROUTES, "/api/reviews": ("http://agent:8002", True)}
    monkeypatch.setattr(main, "ROUTES", MappingProxyType(routes))
    r = client.post("/api/reviews/stream", json={"diff": "x"}, headers=auth(token()))
    assert r.json()["host"] == "agent"
    assert r.json()["path"] == "/reviews/stream"


def echo_headers(request: httpx2.Request) -> httpx2.Response:
    """An upstream that answers with the headers and body it received."""
    names = ("x-hub-signature-256", "x-github-event", "x-user-id", "x-installation-id")
    body = {
        "host": request.url.host,
        "path": request.url.path,
        "headers": {name: request.headers.get(name) for name in names},
        "body": request.content.decode(),
    }
    return httpx2.Response(
        200,
        headers={"content-type": "application/json"},
        stream=httpx2.ByteStream(json.dumps(body).encode()),
    )


def test_github_webhooks_are_public_and_arrive_unchanged(client, monkeypatch):
    routes = {**main.ROUTES, "/api/github/webhooks": ("http://github:8003", False)}
    monkeypatch.setattr(main, "ROUTES", MappingProxyType(routes))
    main.app.state.http = httpx2.AsyncClient(transport=httpx2.MockTransport(echo_headers))
    signed = {"X-Hub-Signature-256": "sha256=abc", "X-GitHub-Event": "pull_request"}
    r = client.post("/api/github/webhooks", content=b'{"action":"opened"}', headers=signed)
    assert r.status_code == 200
    echoed = r.json()
    assert (echoed["host"], echoed["path"]) == ("github", "/github/webhooks")
    # The GitHub service checks the signature over these exact bytes.
    assert echoed["body"] == '{"action":"opened"}'
    assert echoed["headers"]["x-hub-signature-256"] == "sha256=abc"
    assert echoed["headers"]["x-github-event"] == "pull_request"


def test_clients_cannot_claim_to_be_a_github_installation(client):
    main.app.state.http = httpx2.AsyncClient(transport=httpx2.MockTransport(echo_headers))
    headers = {**auth(token()), "X-Installation-Id": "2002"}
    r = client.post("/api/reviews/stream", json={"diff": "x"}, headers=headers)
    assert r.json()["headers"]["x-installation-id"] is None
    assert r.json()["headers"]["x-user-id"] == "42"


def test_reviews_require_token(client):
    assert client.post("/api/reviews/stream", json={"diff": "x"}).status_code == 401


def test_routes_are_read_only():
    with pytest.raises(TypeError):
        main.ROUTES["/api/admin"] = ("http://evil", False)


def test_upstream_down_returns_502(client):
    main.app.state.http = httpx2.AsyncClient(transport=httpx2.MockTransport(unreachable))
    assert client.post("/api/auth/login", json={}).status_code == 502


def test_body_over_declared_limit_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "max_body_bytes", 100)
    r = client.post("/api/auth/login", content=b"x" * 101)
    assert r.status_code == 413


def test_body_over_limit_without_length_rejected(client, monkeypatch):
    """A client can send a body in chunks with no Content-Length at all."""
    monkeypatch.setattr(settings, "max_body_bytes", 100)
    chunks = (b"x" * 60 for _ in range(3))
    assert client.post("/api/auth/login", content=chunks).status_code == 413


# --- Rate limiter internals ---------------------------------------------------


def test_counter_always_gets_an_expiry():
    """If a counter ever lost its expiry, the user would be blocked forever."""
    redis = fakeredis.FakeAsyncRedis()

    async def go():
        await redis.set("rl:9:1", 5)  # a counter left without expiry by a crash
        count = await count_request(redis, "rl:9:1")
        return count, await redis.ttl("rl:9:1")

    count, ttl = asyncio.run(go())
    assert count == 6
    assert 0 < ttl <= 60


def test_retry_after_points_to_next_window():
    redis = fakeredis.FakeAsyncRedis()
    who = Identity(user_id="5", plan="free")

    async def go():
        for _ in range(settings.rate_limit_free):
            await check_rate_limit(redis, who, now=120.0 + 45)
        await check_rate_limit(redis, who, now=120.0 + 45)

    with pytest.raises(main.HTTPException) as exc:
        asyncio.run(go())
    assert exc.value.headers["Retry-After"] == "15"


class OddPipeline:
    """A Redis pipeline that returns something INCR never should."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def incr(self, key):
        return self

    def expire(self, key, seconds, nx):
        return self

    async def execute(self):
        return ["not a number", True]


class OddRedis:
    def pipeline(self, transaction):
        return OddPipeline()


def test_unexpected_redis_reply_is_an_error():
    with pytest.raises(TypeError, match="unexpected INCR result"):
        asyncio.run(count_request(OddRedis(), "rl:1:1"))
