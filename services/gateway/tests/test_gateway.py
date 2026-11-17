import json
from datetime import UTC, datetime, timedelta

import fakeredis
import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import settings


def fake_upstream(request: httpx.Request) -> httpx.Response:
    """Stands in for the services behind the gateway and echoes what it received."""
    body = json.dumps({"host": request.url.host, "path": request.url.path,
                       "user": request.headers.get("x-user-id")})
    # A real network response is a stream, so build one the gateway can stream.
    return httpx.Response(200, headers={"content-type": "application/json"},
                          stream=httpx.ByteStream(body.encode()))


@pytest.fixture()
def client(monkeypatch):
    with TestClient(main.app) as c:
        main.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(fake_upstream))
        main.app.state.redis = fakeredis.FakeAsyncRedis()
        yield c


def token(sub="42", plan="free", minutes=5):
    now = datetime.now(UTC)
    return jwt.encode({"sub": sub, "plan": plan, "exp": now + timedelta(minutes=minutes)},
                      settings.jwt_secret, algorithm="HS256")


def auth(t):
    return {"Authorization": f"Bearer {t}"}


def test_public_route_needs_no_token(client):
    r = client.post("/api/auth/login", json={})
    assert r.status_code == 200
    assert r.json()["path"] == "/auth/login"


def test_protected_route_requires_token(client):
    assert client.get("/api/users/me").status_code == 401


def test_valid_token_forwards_user_id(client):
    r = client.get("/api/users/me", headers=auth(token()))
    assert r.status_code == 200
    assert r.json() == {"host": "localhost", "path": "/users/me", "user": "42"}


def test_client_cannot_spoof_user_id(client):
    r = client.get("/api/users/me", headers={**auth(token()), "X-User-Id": "1"})
    assert r.json()["user"] == "42"


def test_expired_token_rejected(client):
    r = client.get("/api/users/me", headers=auth(token(minutes=-1)))
    assert r.status_code == 401


def test_bad_signature_rejected(client):
    forged = jwt.encode({"sub": "1", "plan": "pro"}, "x" * 40, algorithm="HS256")
    assert client.get("/api/users/me", headers=auth(forged)).status_code == 401


def test_rate_limit_per_plan(client, monkeypatch):
    monkeypatch.setitem(main.check_rate_limit.__globals__["LIMITS"], "free", 3)
    t = token(sub="7")
    codes = [client.get("/api/users/me", headers=auth(t)).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_unknown_route_404(client):
    assert client.get("/api/nope").status_code == 404


def test_reviews_route_to_agent_service(client, monkeypatch):
    monkeypatch.setitem(main.ROUTES, "/api/reviews", ("http://agent:8002", True))
    r = client.post("/api/reviews/stream", json={"diff": "x"}, headers=auth(token()))
    assert r.json() == {"host": "agent", "path": "/reviews/stream", "user": "42"}


def test_reviews_require_token(client):
    assert client.post("/api/reviews/stream", json={"diff": "x"}).status_code == 401
