"""Core API: owns users and (later) projects, reviews and settings.

It trusts the X-User-Id header because only the gateway can reach it
(it is not exposed outside the Docker network).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import Base, engine, get_db
from .models import User
from .schemas import Credentials, Token, UserOut
from .security import create_access_token, hash_password, verify_password

DbSession = Annotated[Session, Depends(get_db)]


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Fine for now; we switch to Alembic migrations when the schema grows.
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Core API", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/auth/signup", status_code=status.HTTP_201_CREATED)
def signup(creds: Credentials, db: DbSession) -> Token:
    user = User(email=creds.email.lower(), password_hash=hash_password(creds.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered") from None
    db.refresh(user)
    return Token(access_token=create_access_token(user.id, user.plan))


@app.post("/auth/login")
def login(creds: Credentials, db: DbSession) -> Token:
    user = db.scalar(select(User).where(User.email == creds.email.lower()))
    # Same error for "no such user" and "wrong password" so attackers
    # can't use this endpoint to discover which emails have accounts.
    if user is None or not verify_password(creds.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    return Token(access_token=create_access_token(user.id, user.plan))


@app.get("/users/me")
def me(x_user_id: Annotated[int, Header()], db: DbSession) -> UserOut:
    user = db.get(User, x_user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return UserOut.model_validate(user)
