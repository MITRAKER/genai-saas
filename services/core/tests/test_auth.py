import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import db as db_module
from app import main
from app.config import settings
from app.db import Base, get_db
from app.security import hash_password, verify_password

# In-memory SQLite keeps unit tests fast; CI also runs against real Postgres.
engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSession = sessionmaker(bind=engine)


@pytest.fixture
def client(monkeypatch):
    Base.metadata.drop_all(engine)
    monkeypatch.setattr(main, "engine", engine)

    def override():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    main.app.dependency_overrides[get_db] = override
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


CREDS = {"email": "Dev@Example.com", "password": "s3cret-pass"}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_signup_then_login(client):
    r = client.post("/auth/signup", json=CREDS)
    assert r.status_code == 201
    claims = jwt.decode(r.json()["access_token"], settings.jwt_secret, algorithms=["HS256"])
    assert claims["sub"] == "1"
    assert claims["plan"] == "free"
    assert claims["exp"] > claims["iat"]

    r = client.post("/auth/login", json={**CREDS, "email": "dev@example.com"})
    assert r.status_code == 200


def test_duplicate_signup_rejected(client):
    client.post("/auth/signup", json=CREDS)
    assert client.post("/auth/signup", json=CREDS).status_code == 409


@pytest.mark.parametrize(
    "creds",
    [
        {**CREDS, "password": "wrong-pass"},  # wrong password
        {**CREDS, "email": "nobody@example.com"},  # unknown user: same answer
    ],
)
def test_login_failures_look_the_same(client, creds):
    client.post("/auth/signup", json=CREDS)
    r = client.post("/auth/login", json=creds)
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid email or password"


@pytest.mark.parametrize(
    "password",
    [
        "short",  # under 8 characters
        "a" * 73,  # over 72 characters
        "é" * 40,  # 40 characters but 80 bytes: over bcrypt's limit
    ],
)
def test_bad_passwords_rejected(client, password):
    r = client.post("/auth/signup", json={"email": "a@b.com", "password": password})
    assert r.status_code == 422


def test_me(client):
    client.post("/auth/signup", json=CREDS)
    r = client.get("/users/me", headers={"X-User-Id": "1"})
    assert r.status_code == 200
    assert r.json() == {"id": 1, "email": "dev@example.com", "plan": "free"}


def test_me_unknown_user(client):
    assert client.get("/users/me", headers={"X-User-Id": "99"}).status_code == 404


def test_password_hashing():
    hashed = hash_password("correct horse")
    assert hashed != hash_password("correct horse")  # random salt
    assert verify_password("correct horse", hashed)
    assert not verify_password("wrong horse", hashed)
    assert not verify_password("x" * 73, hashed)  # over the limit never matches
    with pytest.raises(ValueError, match="72 bytes"):
        hash_password("x" * 73)


def test_get_db_closes_the_session(monkeypatch):
    closed = []

    class Recorder(Session):
        def close(self):
            closed.append(True)
            super().close()

    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=engine, class_=Recorder))
    sessions = get_db()
    assert isinstance(next(sessions), Recorder)
    sessions.close()
    assert closed == [True]
