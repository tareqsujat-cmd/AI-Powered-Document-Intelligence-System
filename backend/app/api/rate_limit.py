"""Fixed-window rate limits in Redis, so that every API replica shares one count."""

import logging
import time
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from redis import Redis
from redis.exceptions import RedisError

from app.api.deps import SettingsDep

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60
_SOCKET_TIMEOUT_SECONDS = 1.0


@lru_cache
def _client_for(url: str) -> Redis:
    return Redis.from_url(
        url,
        socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
        socket_timeout=_SOCKET_TIMEOUT_SECONDS,
    )


def get_rate_limit_redis(settings: SettingsDep) -> Redis:
    return _client_for(settings.redis_url)


RedisDep = Annotated[Redis, Depends(get_rate_limit_redis)]


def enforce_limit(
    client: Redis, scope: str, client_id: str, limit: int, *, now: float | None = None
) -> None:
    """Raise 429 once a client passes its limit. Allows the request if Redis cannot be reached."""
    if limit == 0:
        return
    now = time.time() if now is None else now
    window = int(now // WINDOW_SECONDS)
    key = f"ratelimit:{scope}:{client_id}:{window}"
    try:
        # One transaction, so that a crash between the two cannot leave a key that never expires.
        with client.pipeline(transaction=True) as pipeline:
            pipeline.incr(key)
            pipeline.expire(key, WINDOW_SECONDS)
            count = int(pipeline.execute()[0])
    except (RedisError, OSError):
        logger.warning("rate limit check skipped: Redis is unavailable", extra={"scope": scope})
        return
    if count > limit:
        retry_after = max(1, WINDOW_SECONDS - int(now % WINDOW_SECONDS))
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many requests. Try again shortly.",
            headers={"Retry-After": str(retry_after)},
        )


def _client_id(request: Request) -> str:
    # X-Forwarded-For is set by the client unless a trusted proxy replaces it, so it is ignored.
    return request.client.host if request.client else "unknown"


def limit_uploads(request: Request, settings: SettingsDep, client: RedisDep) -> None:
    enforce_limit(client, "upload", _client_id(request), settings.rate_limit_uploads_per_minute)


def limit_requests(request: Request, settings: SettingsDep, client: RedisDep) -> None:
    enforce_limit(client, "general", _client_id(request), settings.rate_limit_requests_per_minute)


def limit_searches(request: Request, settings: SettingsDep, client: RedisDep) -> None:
    enforce_limit(client, "search", _client_id(request), settings.rate_limit_searches_per_minute)
