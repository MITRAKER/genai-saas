"""Settings of the GitHub integration, read from the environment at startup.

The webhook receiver (app/main.py) and the worker (app/worker.py) run from
the same image and share these settings; each uses its own part. Every
number has bounds, so a typo in .env stops the service with a clear message
instead of misbehaving later (JPL rule 5).
"""

from typing import Final, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

# The REST API version this code was written against. GitHub keeps a version
# working for at least 24 months after the next one comes out.
GITHUB_API_VERSION: Final = "2026-03-10"
MIN_SECRET_LENGTH: Final = 32


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://app:app@localhost:5432/app"
    agent_url: str = "http://localhost:8002"
    github_api_url: str = "https://api.github.com"

    # Shared with the GitHub App's webhook settings. Empty turns webhooks off:
    # they get 503, never "accepted without checking the signature".
    github_webhook_secret: str = ""
    # Reviews cost money, and anyone can install a public GitHub App. Only pull
    # requests in repositories owned by these GitHub accounts are reviewed
    # (logins, comma-separated). Empty means none.
    github_allowed_accounts: str = ""
    # The GitHub App the worker acts as: its App ID and its private key file.
    github_app_id: str = ""
    github_private_key_path: str = ""

    # Limits (JPL rule 3: every resource has a bound).
    max_reviews_per_day: int = Field(default=50, gt=0, le=10_000)  # per installation
    max_diff_chars: int = Field(default=200_000, gt=0, le=1_000_000)
    job_max_attempts: int = Field(default=5, gt=0, le=20)
    job_timeout_seconds: float = Field(default=900, gt=0, le=3600)
    worker_poll_seconds: float = Field(default=2, gt=0, le=60)
    # JPL rule 2: even the worker's main loop has a bound. After this many
    # polls it exits, and Docker starts a fresh one.
    worker_max_polls: int = Field(default=100_000, gt=0, le=10_000_000)

    # Code embeddings: "local" runs here with no API key, "voyage" calls
    # Voyage AI (better results; needs VOYAGE_API_KEY).
    embeddings_provider: Literal["local", "voyage"] = "local"
    voyage_api_key: str = ""
    voyage_model: str = Field(default="voyage-code-3", min_length=1)
    index_max_files: int = Field(default=1_000, gt=0, le=20_000)
    index_max_file_bytes: int = Field(default=100_000, gt=0, le=1_000_000)
    # New chunks embedded per job; a bigger repository is indexed over several.
    index_max_new_chunks: int = Field(default=5_000, gt=0, le=100_000)
    context_max_snippets: int = Field(default=8, ge=0, le=20)
    context_max_chars: int = Field(default=40_000, gt=0, le=60_000)

    @field_validator("github_webhook_secret")
    @classmethod
    def strong_or_empty(cls, value: str) -> str:
        if value and len(value) < MIN_SECRET_LENGTH:
            message = f"must be empty (webhooks off) or at least {MIN_SECRET_LENGTH} characters"
            raise ValueError(message)
        return value

    def allowed_accounts(self) -> frozenset[str]:
        names = (name.strip().lower() for name in self.github_allowed_accounts.split(","))
        return frozenset(name for name in names if name)


settings = Settings()
