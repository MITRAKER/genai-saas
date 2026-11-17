from types import MappingProxyType
from typing import Final, Literal

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Read from environment variables once, at startup. Bounds on every
    number make a bad .env fail fast with a clear message (JPL rule 5)."""

    # Required, with no default: a service must never run with a secret that is
    # printed in its own source code. Generate one with: openssl rand -hex 32
    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: Literal["HS256"] = "HS256"
    redis_url: str = "redis://localhost:6379/0"
    core_url: str = "http://localhost:8001"
    agent_url: str = "http://localhost:8002"
    # Requests allowed per user per minute, by plan.
    rate_limit_free: int = Field(default=30, gt=0, le=100_000)
    rate_limit_pro: int = Field(default=300, gt=0, le=100_000)
    # JPL rule 3 (bounded memory): the largest request body we will read.
    max_body_bytes: int = Field(default=1_048_576, gt=0, le=10_485_760)
    # Connection pool size for each upstream client and for Redis.
    max_connections: int = Field(default=100, gt=0, le=1000)


settings = Settings()

# Routing table: public path prefix -> (upstream base URL, auth required?)
# MappingProxyType makes it read-only: nothing can change routes at run time.
# Milestone 4 adds "/api/billing".
ROUTES: Final = MappingProxyType(
    {
        "/api/auth": (settings.core_url, False),
        "/api/users": (settings.core_url, True),
        "/api/reviews": (settings.agent_url, True),
    }
)
