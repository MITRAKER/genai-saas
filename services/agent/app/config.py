from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # "anthropic" calls Claude (needs ANTHROPIC_API_KEY in the environment).
    # "fake" returns a canned review: no key, no cost. Used by tests and CI.
    llm_provider: Literal["anthropic", "fake"] = "anthropic"
    anthropic_model: str = "claude-opus-5-5"
    # How hard Claude works: low | medium | high | xhigh | max.
    # Code review rewards care, so we default to "high".
    review_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    review_max_tokens: int = 32000
    # ~4 characters per token, so this caps one review at roughly 50k input tokens.
    max_diff_chars: int = 200_000
    # Send an SSE comment this often so proxies don't drop a quiet connection.
    keepalive_seconds: float = 15.0
    fake_delay_seconds: float = 0.05


settings = Settings()
