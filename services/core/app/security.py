from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from .config import settings


def hash_password(password: str) -> str:
    # bcrypt is deliberately slow and adds a random salt, so leaked hashes
    # are expensive to crack and identical passwords get different hashes.
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_access_token(user_id: int, plan: str) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": str(user_id),  # who the token is about
        "plan": plan,  # lets the gateway rate-limit without a DB call
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_ttl_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
