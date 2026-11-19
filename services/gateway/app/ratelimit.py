import time

from fastapi import HTTPException, status
from redis.asyncio import Redis

from .config import settings

LIMITS = {"free": settings.rate_limit_free, "pro": settings.rate_limit_pro}


async def check_rate_limit(redis: Redis, user_id: str, plan: str) -> dict:
    """Fixed-window limiter: one Redis counter per user per minute.

    INCR is atomic, so this stays correct with many gateway replicas
    sharing one Redis. The key expires on its own after the window.
    """
    limit = LIMITS.get(plan, settings.rate_limit_free)
    window = int(time.time() // 60)
    key = f"rl:{user_id}:{window}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 60)
    headers = {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(max(0, limit - count)),
    }
    if count > limit:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Rate limit exceeded, try again next minute",
            headers={**headers, "Retry-After": str(60 - int(time.time()) % 60)},
        )
    return headers
