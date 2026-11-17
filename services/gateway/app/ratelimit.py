import time
from typing import Final

from fastapi import HTTPException, status
from redis.asyncio import Redis

from .auth import Identity
from .config import settings

WINDOW_SECONDS: Final = 60
LIMITS: Final = {"free": settings.rate_limit_free, "pro": settings.rate_limit_pro}


async def count_request(redis: Redis, key: str) -> int:
    """Add one to the counter and make sure it expires.

    Both commands run in one transaction (MULTI/EXEC). EXPIRE ... NX sets the
    expiry only if the key has none, so even if an earlier request crashed
    between the two steps, this one repairs it. A counter without an expiry
    would block the user forever.
    """
    async with redis.pipeline(transaction=True) as pipe:
        _ = pipe.incr(key)
        _ = pipe.expire(key, WINDOW_SECONDS, nx=True)
        count, _ = await pipe.execute()
    if not isinstance(count, int):  # JPL rule 7: check what we got back
        raise TypeError(f"unexpected INCR result: {count!r}")
    return count


async def check_rate_limit(
    redis: Redis, identity: Identity, now: float | None = None
) -> dict[str, str]:
    """Fixed-window limiter: one Redis counter per user per minute.

    INCR is atomic, so this stays correct with many gateway replicas sharing
    one Redis. Returns the X-RateLimit-* headers for the response.
    """
    now = time.time() if now is None else now
    limit = LIMITS[identity.plan]
    count = await count_request(redis, f"rl:{identity.user_id}:{int(now // WINDOW_SECONDS)}")
    headers = {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(max(0, limit - count)),
    }
    if count > limit:
        retry_after = WINDOW_SECONDS - int(now) % WINDOW_SECONDS
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Rate limit exceeded, try again next minute",
            headers={**headers, "Retry-After": str(retry_after)},
        )
    return headers
