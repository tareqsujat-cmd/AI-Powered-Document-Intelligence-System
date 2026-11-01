import logging

from redis import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

PACING_KEY = "llm:next_request_at"
_KEY_LIFETIME_SECONDS = 3600
# Atomic, so that workers can never reserve the same slot. Uses Redis's clock, the one they share.
_RESERVE_SLOT = """
local time = redis.call('TIME')
local now = tonumber(time[1]) + tonumber(time[2]) / 1000000
local next_at = tonumber(redis.call('GET', KEYS[1]) or '0')
local slot = math.max(now, next_at)
redis.call('SET', KEYS[1], tostring(slot + tonumber(ARGV[1])), 'EX', ARGV[2])
return tostring(slot - now)
"""


class RedisPacer:
    """Spaces LLM requests across every worker, since a provider's quota covers them all."""

    def __init__(self, client: Redis, requests_per_minute: int) -> None:
        self._client = client
        self._interval = 60.0 / requests_per_minute

    def __call__(self) -> float | None:
        """Seconds to wait before the next request, or None to fall back to pacing locally."""
        try:
            wait = self._client.eval(
                _RESERVE_SLOT, 1, PACING_KEY, self._interval, _KEY_LIFETIME_SECONDS
            )
            return float(wait)
        except RedisError:
            logger.warning("redis is unavailable, so llm pacing is per process")
            return None
