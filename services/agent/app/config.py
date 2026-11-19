from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    """Read from environment variables once, at startup.

    Every number has bounds, so a typo in .env fails at startup with a clear
    message instead of misbehaving later (JPL rule 5: check your inputs).
    """

    # "anthropic" calls Claude (needs ANTHROPIC_API_KEY in the environment).
    # "fake" returns a canned review: no key, no cost. Used by tests and CI.
    llm_provider: Literal["anthropic", "fake"] = "anthropic"
    anthropic_model: str = Field(default="claude-opus-5-5", min_length=1)
    # How hard Claude works. Code review rewards care, so we default to "high".
    review_effort: Effort = "high"
    review_max_tokens: int = Field(default=32_000, gt=0, le=128_000)
    # ~4 characters per token, so this caps one review at roughly 50k input tokens.
    max_diff_chars: int = Field(default=200_000, gt=0, le=1_000_000)
    # Hard deadline for one review, so no stream can run (and bill) forever.
    review_timeout_seconds: float = Field(default=600, gt=0, le=3600)
    # Send an SSE comment this often so proxies don't drop a quiet connection.
    keepalive_seconds: float = Field(default=15, gt=0, le=60)
    fake_delay_seconds: float = Field(default=0.05, ge=0, le=5)


settings = Settings()
