from datetime import UTC, datetime, timedelta
from typing import Final

import bcrypt
import jwt

from .config import settings

# bcrypt only looks at the first 72 bytes of a password. Since bcrypt 5 it
# raises an error instead of silently ignoring the rest, so we enforce the
# limit ourselves with a clear message (JPL rule 5: check inputs).
BCRYPT_MAX_BYTES: Final = 72


def hash_password(password: str) -> str:
    # bcrypt is deliberately slow and adds a random salt, so leaked hashes
    # are expensive to crack and identical passwords get different hashes.
    encoded = password.encode()
    if len(encoded) > BCRYPT_MAX_BYTES:
        raise ValueError("password is longer than 72 bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    encoded = password.encode()
    if len(encoded) > BCRYPT_MAX_BYTES:
        return False
    return bcrypt.checkpw(encoded, password_hash.encode())


def create_access_token(user_id: int, plan: str) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": str(user_id),  # who the token is about
        "plan": plan,  # lets the gateway rate-limit without a DB call
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_ttl_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
