from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Settings are read from environment variables (12-factor style).
    Bounds make a bad value fail at startup (JPL rule 5)."""

    database_url: str = "postgresql+psycopg2://app:app@localhost:5432/app"
    # Required, with no default: a service must never run with a secret that is
    # printed in its own source code. Generate one with: openssl rand -hex 32
    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: Literal["HS256"] = "HS256"
    jwt_ttl_minutes: int = Field(default=60, gt=0, le=24 * 60)


settings = Settings()
