from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    jwt_secret: str = "dev-only-secret-change-me-in-production-0000"
    jwt_algorithm: str = "HS256"
    redis_url: str = "redis://localhost:6379/0"
    core_url: str = "http://localhost:8001"
    agent_url: str = "http://localhost:8002"
    # Requests allowed per user per minute, by plan.
    rate_limit_free: int = 30
    rate_limit_pro: int = 300


settings = Settings()

# Routing table: public path prefix -> (upstream base URL, auth required?)
# Milestone 4 adds "/api/billing".
ROUTES = {
    "/api/auth": (settings.core_url, False),
    "/api/users": (settings.core_url, True),
    "/api/reviews": (settings.agent_url, True),
}
