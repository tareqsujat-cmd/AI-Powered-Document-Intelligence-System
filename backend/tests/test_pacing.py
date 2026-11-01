import os
import time
from collections.abc import Iterator

import httpx
import pytest
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

from app.core.config import Settings
from app.extraction.llm_client import GeminiClient
from app.extraction.pacing import PACING_KEY, RedisPacer


@pytest.fixture
def real_redis() -> Iterator["Redis"]:
    url = os.environ.get("REDIS_URL", "")
    if not url:
        pytest.skip("REDIS_URL is not set")
    client = Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
    try:
        client.ping()
    except RedisError:
        pytest.skip("Redis is not reachable")
    client.delete(PACING_KEY)
    yield client
    client.delete(PACING_KEY)
    client.close()


def test_two_workers_share_one_schedule(real_redis: "Redis") -> None:
    first, second = RedisPacer(real_redis, 60), RedisPacer(real_redis, 60)

    waits = [first(), second(), first(), second()]

    assert waits[0] == pytest.approx(0.0, abs=0.05)
    assert waits[1] == pytest.approx(1.0, abs=0.1)
    assert waits[2] == pytest.approx(2.0, abs=0.1)
    assert waits[3] == pytest.approx(3.0, abs=0.1)


def test_the_schedule_catches_up_after_a_quiet_spell(real_redis: "Redis") -> None:
    pacer = RedisPacer(real_redis, 600)  # 0.1 s apart
    pacer()
    time.sleep(0.3)

    assert pacer() == pytest.approx(0.0, abs=0.05)


def test_the_key_expires_so_it_cannot_linger(real_redis: "Redis") -> None:
    RedisPacer(real_redis, 60)()

    assert 0 < real_redis.ttl(PACING_KEY) <= 3600


class BrokenRedis:
    def eval(self, *_args: object) -> None:
        raise RedisConnectionError("redis is down")


def test_a_redis_outage_falls_back_rather_than_failing() -> None:
    assert RedisPacer(BrokenRedis(), 60)() is None  # type: ignore[arg-type]


def make_client(pacer: object, **overrides: object) -> tuple[GeminiClient, list[float]]:
    sleeps: list[float] = []
    values: dict[str, object] = {"llm_api_key": "k", "llm_requests_per_minute": 6}
    client = GeminiClient(
        Settings(_env_file=None, **{**values, **overrides}),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": [{"text": "{}"}]}}]},
            )
        ),
        sleep=sleeps.append,
        pacer=pacer,  # type: ignore[arg-type]
    )
    return client, sleeps


def test_the_client_waits_for_as_long_as_the_shared_pacer_says() -> None:
    client, sleeps = make_client(lambda: 4.5)

    client.generate_json("p", {"type": "object"})

    assert sleeps == [4.5]


def test_no_wait_means_no_sleep() -> None:
    client, sleeps = make_client(lambda: 0.0)

    client.generate_json("p", {"type": "object"})

    assert sleeps == []


def test_the_client_falls_back_to_local_pacing_when_the_pacer_cannot_help() -> None:
    client, sleeps = make_client(lambda: None)

    client.generate_json("p", {"type": "object"})
    client.generate_json("p", {"type": "object"})

    assert len(sleeps) == 1
    assert 9 < sleeps[0] <= 10


def test_pacing_turned_off_ignores_the_pacer() -> None:
    def must_not_run() -> float:
        raise AssertionError("pacing is off")

    client, sleeps = make_client(must_not_run, llm_requests_per_minute=0)

    client.generate_json("p", {"type": "object"})

    assert sleeps == []
