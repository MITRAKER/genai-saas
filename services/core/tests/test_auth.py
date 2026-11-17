import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.db import Base, get_db

# In-memory SQLite keeps unit tests fast; CI also runs against real Postgres.
engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
TestingSession = sessionmaker(bind=engine)


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)

    def override():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[get_db] = override
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


CREDS = {"email": "Dev@Example.com", "password": "s3cret-pass"}


def test_signup_then_login(client):
    r = client.post("/auth/signup", json=CREDS)
    assert r.status_code == 201
    assert r.json()["access_token"]

    r = client.post("/auth/login", json={**CREDS, "email": "dev@example.com"})
    assert r.status_code == 200


def test_duplicate_signup_rejected(client):
    client.post("/auth/signup", json=CREDS)
    assert client.post("/auth/signup", json=CREDS).status_code == 409


def test_wrong_password(client):
    client.post("/auth/signup", json=CREDS)
    r = client.post("/auth/login", json={**CREDS, "password": "wrong-pass"})
    assert r.status_code == 401


def test_short_password_rejected(client):
    r = client.post("/auth/signup", json={"email": "a@b.com", "password": "short"})
    assert r.status_code == 422


def test_me(client):
    client.post("/auth/signup", json=CREDS)
    r = client.get("/users/me", headers={"X-User-Id": "1"})
    assert r.status_code == 200
    assert r.json() == {"id": 1, "email": "dev@example.com", "plan": "free"}
