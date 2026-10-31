import logging
from collections.abc import Callable, Mapping
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from redis import Redis
from sqlalchemy import text

from app import __version__
from app.core.config import Settings, get_settings
from app.db.session import get_engine

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/health", tags=["health"])

ReadinessCheck = Callable[[], None]


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str


class ReadinessResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    checks: dict[str, Literal["ok", "failed"]]


def check_database() -> None:
    with get_engine().connect() as connection:
        connection.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).one()


def check_redis() -> None:
    client = Redis.from_url(get_settings().redis_url, socket_connect_timeout=2, socket_timeout=2)
    try:
        client.ping()
    finally:
        client.close()


def get_readiness_checks() -> Mapping[str, ReadinessCheck]:
    return {"database": check_database, "redis": check_redis}


@router.get("", response_model=HealthResponse)
def liveness(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    """The process is up. Touches no dependencies, so it stays cheap to poll."""
    return HealthResponse(status="ok", service=settings.app_name, version=__version__)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
def readiness(
    response: Response,
    checks: Annotated[Mapping[str, ReadinessCheck], Depends(get_readiness_checks)],
) -> ReadinessResponse:
    """Postgres (with pgvector) and Redis are reachable. Failure detail goes to logs only."""
    results: dict[str, Literal["ok", "failed"]] = {}
    for name, check in checks.items():
        try:
            check()
            results[name] = "ok"
        except Exception:
            logger.exception("readiness check failed", extra={"check": name})
            results[name] = "failed"

    if all(result == "ok" for result in results.values()):
        return ReadinessResponse(status="ok", checks=results)
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status="unavailable", checks=results)
