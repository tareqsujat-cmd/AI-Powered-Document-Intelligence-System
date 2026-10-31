from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine

from app import __version__
from app.api import health
from app.core.config import Settings
from app.main import create_app


def _ok() -> None:
    return None


def _fail() -> None:
    raise ConnectionError("postgresql://user:hunter2@internal-host/db unreachable")


def _client_with_checks(settings: Settings, **checks: object) -> TestClient:
    app = create_app(settings)
    app.dependency_overrides[health.get_readiness_checks] = lambda: checks
    return TestClient(app)


def test_liveness(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "docintel", "version": __version__}


def test_readiness_ok_when_all_checks_pass(settings: Settings) -> None:
    response = _client_with_checks(settings, database=_ok, redis=_ok).get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


@pytest.mark.parametrize(
    ("database", "redis", "expected"),
    [
        (_fail, _ok, {"database": "failed", "redis": "ok"}),
        (_ok, _fail, {"database": "ok", "redis": "failed"}),
        (_fail, _fail, {"database": "failed", "redis": "failed"}),
    ],
)
def test_readiness_503_when_a_check_fails(
    settings: Settings, database: object, redis: object, expected: dict[str, str]
) -> None:
    response = _client_with_checks(settings, database=database, redis=redis).get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "checks": expected}


def test_readiness_does_not_leak_failure_details(settings: Settings) -> None:
    response = _client_with_checks(settings, database=_fail).get("/health/ready")

    assert "hunter2" not in response.text
    assert "internal-host" not in response.text


def test_redis_check_against_unreachable_server_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        health,
        "get_settings",
        lambda: Settings(_env_file=None, redis_url="redis://127.0.0.1:1/0"),
    )

    with pytest.raises(Exception, match=r"(?i)connect"):
        health.check_redis()


def test_cors_allows_configured_origin_only(client: TestClient) -> None:
    allowed = client.get("/health", headers={"Origin": "http://allowed.test"})
    denied = client.get("/health", headers={"Origin": "http://evil.test"})

    assert allowed.headers["access-control-allow-origin"] == "http://allowed.test"
    assert "access-control-allow-origin" not in denied.headers


@pytest.fixture
def patched_engine(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[None]:
    engine: Engine = request.getfixturevalue(request.param)
    monkeypatch.setattr(health, "get_engine", lambda: engine)
    yield


@pytest.mark.db
@pytest.mark.parametrize("patched_engine", ["migrated_engine"], indirect=True)
def test_database_check_passes_against_real_postgres(patched_engine: None) -> None:
    health.check_database()


@pytest.mark.db
def test_database_check_fails_when_pgvector_is_missing(
    empty_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine(empty_database_url)
    monkeypatch.setattr(health, "get_engine", lambda: engine)
    try:
        with pytest.raises(Exception, match="No row was found"):
            health.check_database()
    finally:
        engine.dispose()
