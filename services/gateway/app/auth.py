from dataclasses import dataclass
from typing import Final

import jwt
from fastapi import HTTPException, Request, status

from .config import settings

PLANS: Final = frozenset({"free", "pro"})


@dataclass(frozen=True)
class Identity:
    """Who is calling, as proven by a valid token."""

    user_id: str
    plan: str


def unauthorized(message: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, message)


def authenticate(request: Request) -> Identity:
    """Verify the Bearer JWT and return the caller's identity.

    The gateway checks the signature with the shared secret, so it never
    needs to call the core API or the database to know who you are.
    """
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise unauthorized("Missing bearer token")
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},  # no token lives forever
        )
    except jwt.ExpiredSignatureError:
        raise unauthorized("Token expired") from None
    except jwt.InvalidTokenError:
        raise unauthorized("Invalid token") from None
    # A valid signature proves who issued the token, not that its contents
    # have the shape we expect. Check them too (JPL rule 5).
    user_id, plan = claims.get("sub"), claims.get("plan", "free")
    if not isinstance(user_id, str) or plan not in PLANS:
        raise unauthorized("Invalid token claims")
    return Identity(user_id=user_id, plan=plan)
