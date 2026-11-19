from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Settings are read from environment variables (12-factor style)."""

    database_url: str = "postgresql+psycopg2://app:app@localhost:5432/app"
    jwt_secret: str = "dev-only-secret-change-me-in-production-0000"
    jwt_algorithm: str = "HS256"
    jwt_ttl_minutes: int = 60


settings = Settings()
