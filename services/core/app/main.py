"""Core API: owns users and (later) projects, reviews and settings.

It trusts the X-User-Id header because only the gateway can reach it
(it is not exposed outside the Docker network).
"""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import Base, engine, get_db
from .models import User
from .schemas import Credentials, Token, UserOut
from .security import create_access_token, hash_password, verify_password


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fine for milestone 1; we switch to Alembic migrations later.
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Core API", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/auth/signup", response_model=Token, status_code=status.HTTP_201_CREATED)
def signup(creds: Credentials, db: Session = Depends(get_db)):
    user = User(email=creds.email.lower(), password_hash=hash_password(creds.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered") from None
    db.refresh(user)
    return Token(access_token=create_access_token(user.id, user.plan))


@app.post("/auth/login", response_model=Token)
def login(creds: Credentials, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == creds.email.lower()))
    # Same error for "no such user" and "wrong password" so attackers
    # can't use this endpoint to discover which emails have accounts.
    if not user or not verify_password(creds.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    return Token(access_token=create_access_token(user.id, user.plan))


@app.get("/users/me", response_model=UserOut)
def me(x_user_id: int = Header(...), db: Session = Depends(get_db)):
    user = db.get(User, x_user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user
