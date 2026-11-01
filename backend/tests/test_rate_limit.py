import logging
import os
import re
import socket
import time
import uuid
from collections.abc import Callable
from typing import cast

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from redis import Redis
from redis.exceptions import RedisError

from app.api import rate_limit
from app.api.rate_limit import (
    enforce_limit,
    get_rate_limit_redis,
    limit_requests,
    limit_searches,
    limit_uploads,
)
from app.core.config import Settings, get_settings
from tests.api_helpers import API, Api, ExhaustedRedis, FakeRedis, UnreachableRedis, pdf_bytes

pytest_plugins = ["tests.api_helpers"]


def fake() -> tuple[FakeRedis, Redis]:
    redis = FakeRedis()
    return redis, cast(Redis, redis)


# The counter


def test_allows_up_to_the_limit_then_refuses() -> None:
    _, client = fake()

    for _ in range(3):
        enforce_limit(client, "general", "1.2.3.4", 3, now=1000.0)

    with pytest.raises(HTTPException) as refused:
        enforce_limit(client, "general", "1.2.3.4", 3, now=1000.0)

    assert refused.value.status_code == 429
    assert refused.value.detail == "Too many requests. Try again shortly."


def test_key_names_the_scope_client_and_window_and_expires() -> None:
    redis, client = fake()

    enforce_limit(client, "upload", "1.2.3.4", 5, now=1000.0)

    key = "ratelimit:upload:1.2.3.4:16"
    assert redis.counts == {key: 1}
    assert redis.expiries == {key: rate_limit.WINDOW_SECONDS}


def test_a_new_window_starts_a_new_count() -> None:
    _, client = fake()
    enforce_limit(client, "general", "a", 1, now=119.9)
    with pytest.raises(HTTPException):
        enforce_limit(client, "general", "a", 1, now=119.9)

    enforce_limit(client, "general", "a", 1, now=120.0)


def test_clients_and_scopes_are_counted_separately() -> None:
    _, client = fake()

    enforce_limit(client, "general", "a", 1, now=0.0)
    enforce_limit(client, "general", "b", 1, now=0.0)
    enforce_limit(client, "upload", "a", 1, now=0.0)

    with pytest.raises(HTTPException):
        enforce_limit(client, "general", "a", 1, now=0.0)


def test_limit_of_zero_turns_it_off_without_contacting_redis() -> None:
    redis, client = fake()

    for _ in range(100):
        enforce_limit(client, "general", "a", 0)

    assert redis.counts == {}


@pytest.mark.parametrize(
    ("now", "retry_after"),
    [(60.0, 60), (60.5, 60), (61.0, 59), (119.0, 1), (119.9, 1)],
)
def test_retry_after_is_the_time_until_the_next_window(now: float, retry_after: int) -> None:
    _, client = fake()
    enforce_limit(client, "general", "a", 1, now=now)

    with pytest.raises(HTTPException) as refused:
        enforce_limit(client, "general", "a", 1, now=now)

    assert refused.value.headers == {"Retry-After": str(retry_after)}


def test_unreachable_redis_allows_the_request_and_logs_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.api.rate_limit"):
        enforce_limit(cast(Redis, UnreachableRedis()), "general", "a", 1)

    [record] = [r for r in caplog.records if r.name == "app.api.rate_limit"]
    assert record.levelno == logging.WARNING
    assert "unavailable" in record.getMessage()
    assert "secret-host" not in record.getMessage()


def test_a_real_client_gives_up_within_about_a_second_when_redis_stops_answering() -> None:
    # The backlog completes the connection and nothing ever replies, so only a timeout can end it.
    rate_limit._client_for.cache_clear()
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        client = rate_limit._client_for(f"redis://127.0.0.1:{silent.getsockname()[1]}/0")

        started = time.perf_counter()
        enforce_limit(client, "general", "a", 1)
        elapsed = time.perf_counter() - started

    rate_limit._client_for.cache_clear()
    assert 0.5 < elapsed < 2.5


def test_client_for_refused_connection_allows_the_request() -> None:
    rate_limit._client_for.cache_clear()

    enforce_limit(rate_limit._client_for("redis://127.0.0.1:1/0"), "general", "a", 1)

    rate_limit._client_for.cache_clear()


# Real Redis


@pytest.fixture
def real_redis() -> Redis:
    url = os.environ.get("TEST_REDIS_URL") or os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("Neither TEST_REDIS_URL nor REDIS_URL is set")
    client = Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
    try:
        client.ping()
    except RedisError:
        pytest.skip("Redis is not reachable")
    return client


def test_real_redis_counts_expires_and_refuses(real_redis: Redis) -> None:
    client_id = f"test-{uuid.uuid4()}"
    now = time.time()
    key = f"ratelimit:general:{client_id}:{int(now // rate_limit.WINDOW_SECONDS)}"
    try:
        enforce_limit(real_redis, "general", client_id, 2, now=now)
        enforce_limit(real_redis, "general", client_id, 2, now=now)
        with pytest.raises(HTTPException) as refused:
            enforce_limit(real_redis, "general", client_id, 2, now=now)

        assert refused.value.status_code == 429
        assert int(real_redis.get(key)) == 3
        assert 0 < real_redis.ttl(key) <= rate_limit.WINDOW_SECONDS
    finally:
        real_redis.delete(key)


# The dependencies, on a small app


def limited_app(limit: Callable[..., None], redis: object, **settings_values: int) -> TestClient:
    app = FastAPI()

    @app.get("/limited", dependencies=[Depends(limit)])
    def limited() -> dict[str, bool]:
        return {"ok": True}

    settings = Settings(_env_file=None, **settings_values)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_rate_limit_redis] = lambda: redis
    return TestClient(app)


@pytest.mark.parametrize(
    ("limit", "setting", "scope"),
    [
        (limit_uploads, "rate_limit_uploads_per_minute", "upload"),
        (limit_requests, "rate_limit_requests_per_minute", "general"),
        (limit_searches, "rate_limit_searches_per_minute", "search"),
    ],
)
def test_each_dependency_uses_its_own_setting_and_scope(
    limit: Callable[..., None], setting: str, scope: str
) -> None:
    redis = FakeRedis()
    values = {
        "rate_limit_uploads_per_minute": 100,
        "rate_limit_requests_per_minute": 100,
        "rate_limit_searches_per_minute": 100,
        setting: 1,
    }
    client = limited_app(limit, redis, **values)

    assert client.get("/limited").status_code == 200
    refused = client.get("/limited")

    assert refused.status_code == 429
    assert 1 <= int(refused.headers["retry-after"]) <= rate_limit.WINDOW_SECONDS
    assert refused.json() == {"detail": "Too many requests. Try again shortly."}
    [key] = redis.counts
    assert key.startswith(f"ratelimit:{scope}:")


def test_forwarded_for_header_cannot_change_the_client_key() -> None:
    redis = FakeRedis()
    client = limited_app(limit_requests, redis, rate_limit_requests_per_minute=1)

    assert client.get("/limited", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert client.get("/limited", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 429
    [key] = redis.counts
    assert "1.1.1.1" not in key
    assert "2.2.2.2" not in key
    assert "testclient" in key


# The real app


@pytest.mark.db
def test_upload_route_uses_the_upload_limit_only(make_api: Callable[..., Api]) -> None:
    api = make_api(rate_limit_uploads_per_minute=1, rate_limit_requests_per_minute=100)

    assert api.upload(("a.pdf", pdf_bytes())).status_code == 202
    refused = api.upload(("b.pdf", pdf_bytes()))

    assert refused.status_code == 429
    assert "retry-after" in refused.headers
    assert len(api.stored_files()) == 1
    # Other routes have their own count.
    assert api.client.get(f"{API}/documents").status_code == 200
    assert {key.split(":")[1] for key in api.redis.counts} == {"upload", "general"}


@pytest.mark.db
def test_other_routes_share_the_general_limit(make_api: Callable[..., Api]) -> None:
    api = make_api(rate_limit_requests_per_minute=2, rate_limit_uploads_per_minute=100)

    assert api.client.get(f"{API}/documents").status_code == 200
    assert api.client.get(f"{API}/metrics").status_code == 200
    refused = api.client.get(f"{API}/documents/{uuid.uuid4()}")

    assert refused.status_code == 429
    assert api.upload(("a.pdf", pdf_bytes())).status_code == 202


@pytest.mark.db
def test_limit_of_zero_leaves_the_real_app_unlimited(make_api: Callable[..., Api]) -> None:
    api = make_api(rate_limit_requests_per_minute=0)

    for _ in range(5):
        assert api.client.get(f"{API}/metrics").status_code == 200
    assert api.redis.counts == {}


@pytest.mark.db
def test_unreachable_redis_leaves_the_real_app_working(make_api: Callable[..., Api]) -> None:
    api = make_api(rate_limit_requests_per_minute=1)
    api.app.dependency_overrides[get_rate_limit_redis] = UnreachableRedis

    for _ in range(3):
        assert api.client.get(f"{API}/metrics").status_code == 200


@pytest.mark.db
def test_health_is_not_limited(make_api: Callable[..., Api]) -> None:
    api = make_api(rate_limit_requests_per_minute=1, rate_limit_uploads_per_minute=1)

    for _ in range(5):
        assert api.client.get("/health").status_code == 200
    assert api.redis.counts == {}


@pytest.mark.db
def test_every_api_route_has_a_rate_limit(make_api: Callable[..., Api]) -> None:
    api = make_api()
    api.app.dependency_overrides[get_rate_limit_redis] = lambda: ExhaustedRedis()
    operations = [
        (method, path)
        for path, methods in api.app.openapi()["paths"].items()
        if path.startswith(API)
        for method in methods
    ]

    assert len(operations) >= 12
    for method, path in operations:
        url = path.replace("{page_number}", "1")
        url = re.sub(r"\{\w+\}", str(uuid.uuid4()), url)
        response = api.client.request(method.upper(), url)
        assert response.status_code == 429, (method, path)
