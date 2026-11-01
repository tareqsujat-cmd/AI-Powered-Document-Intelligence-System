import logging
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.security import (
    RequestSizeLimitMiddleware,
    SecurityHeadersMiddleware,
    UnexpectedErrorMiddleware,
)
from app.core.config import Settings
from app.core.logging import REQUEST_ID_HEADER
from app.main import create_app
from tests.api_helpers import API, Api, pdf_bytes

pytest_plugins = ["tests.api_helpers"]

MIB = 1024 * 1024
ORIGIN = "http://allowed.test"


def app_with_crash(settings: Settings) -> FastAPI:
    app = create_app(settings)

    @app.get("/crash")
    def crash() -> None:
        raise RuntimeError("postgresql://user:hunter2@internal-host/db refused")

    return app


# Security headers


def test_security_headers_on_json_responses(client: TestClient) -> None:
    response = client.get("/health")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"


def test_cache_control_is_not_added_to_other_content_types() -> None:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/page", response_class=None)
    def page() -> Any:
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse("hello")

    response = TestClient(app).get("/page")

    assert response.headers["x-frame-options"] == "DENY"
    assert "cache-control" not in response.headers


def test_security_headers_do_not_overwrite_or_repeat_a_routes_own() -> None:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/page")
    def page() -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {}, headers={"Cache-Control": "max-age=5", "X-Content-Type-Options": "nosniff"}
        )

    response = TestClient(app).get("/page")

    assert response.headers["cache-control"] == "max-age=5"
    assert response.headers.get_list("x-content-type-options") == ["nosniff"]


@pytest.mark.db
def test_file_route_has_one_of_each_protective_header(api: Api) -> None:
    [outcome] = api.upload(("a.pdf", pdf_bytes())).json()["results"]

    response = api.client.get(f"{API}/documents/{outcome['document']['id']}/file")

    assert response.headers.get_list("x-content-type-options") == ["nosniff"]
    assert response.headers.get_list("cache-control") == ["no-store"]
    assert response.headers["x-frame-options"] == "DENY"


# CORS


def test_cors_preflight_allows_the_methods_the_api_uses(client: TestClient) -> None:
    for method in ("GET", "POST", "PATCH"):
        response = client.options(
            f"{API}/documents",
            headers={"Origin": ORIGIN, "Access-Control-Request-Method": method},
        )
        assert response.status_code == 200, method
        assert response.headers["access-control-allow-origin"] == ORIGIN


def test_cors_rejects_an_unknown_origin(client: TestClient) -> None:
    response = client.options(
        f"{API}/documents",
        headers={"Origin": "http://evil.test", "Access-Control-Request-Method": "GET"},
    )

    assert "access-control-allow-origin" not in response.headers


def test_cors_lets_the_frontend_read_the_headers_it_needs(client: TestClient) -> None:
    response = client.get("/health", headers={"Origin": ORIGIN})

    exposed = {
        name.strip().lower()
        for name in response.headers["access-control-expose-headers"].split(",")
    }
    assert exposed == {"x-request-id", "retry-after", "content-disposition"}


# Unexpected errors


def test_unexpected_error_is_a_plain_500(settings: Settings) -> None:
    response = TestClient(app_with_crash(settings)).get("/crash")

    assert response.status_code == 500
    assert response.json() == {"detail": "An unexpected error occurred."}
    assert "hunter2" not in response.text
    assert "RuntimeError" not in response.text


def test_unexpected_error_keeps_the_other_middleware_headers(settings: Settings) -> None:
    client = TestClient(app_with_crash(settings))

    response = client.get(
        "/crash", headers={REQUEST_ID_HEADER: "req-123", "Origin": "http://allowed.test"}
    )

    assert response.status_code == 500
    assert response.headers[REQUEST_ID_HEADER] == "req-123"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["access-control-allow-origin"] == "http://allowed.test"


def test_unexpected_error_logs_the_traceback(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    # create_app replaces the root handlers, so this listens on the logger directly.
    client = TestClient(app_with_crash(settings))
    security_logger = logging.getLogger("app.api.security")
    security_logger.addHandler(caplog.handler)
    try:
        client.get("/crash")
    finally:
        security_logger.removeHandler(caplog.handler)

    [record] = [r for r in caplog.records if r.name == "app.api.security"]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert "hunter2" in str(record.exc_info[1])


def test_error_after_the_response_started_is_reraised() -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("too late")

    client = TestClient(UnexpectedErrorMiddleware(app))

    with pytest.raises(RuntimeError, match="too late"):
        client.get("/")


# Request size


def size_limited_app(max_bytes: int) -> tuple[TestClient, list[int]]:
    app = FastAPI()
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=max_bytes)
    received: list[int] = []

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        body = await request.body()
        received.append(len(body))
        return {"size": len(body)}

    return TestClient(app), received


def chunks(*sizes: int) -> Any:
    for size in sizes:
        yield b"x" * size


def test_declared_content_length_over_the_limit_is_413_without_reaching_the_route() -> None:
    client, received = size_limited_app(MIB)

    response = client.post("/echo", content=b"x" * (MIB + 1))

    assert response.status_code == 413
    assert response.json() == {"detail": "The request exceeds the 1 MB limit."}
    assert received == []


def test_body_exactly_at_the_limit_is_allowed() -> None:
    client, received = size_limited_app(MIB)

    response = client.post("/echo", content=b"x" * MIB)

    assert response.status_code == 200
    assert received == [MIB]


def test_chunked_body_over_the_limit_is_413() -> None:
    client, received = size_limited_app(MIB)

    response = client.post("/echo", content=chunks(MIB, 1))

    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    assert received == []


def test_chunked_body_at_the_limit_is_allowed() -> None:
    client, received = size_limited_app(MIB)

    response = client.post("/echo", content=chunks(MIB - 1, 1))

    assert response.status_code == 200
    assert received == [MIB]


async def run_with_chunks(
    middleware: RequestSizeLimitMiddleware,
    bodies: list[bytes],
    headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    pending = list(bodies)
    reads = 0

    async def receive() -> dict[str, Any]:
        nonlocal reads
        reads += 1
        body = pending.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers or []}
    await middleware(scope, receive, send)
    return sent, reads


async def _reads_everything(scope: Any, receive: Any, send: Any) -> None:
    while True:
        message = await receive()
        if not message["more_body"]:
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_reading_stops_as_soon_as_the_limit_passes() -> None:
    middleware = RequestSizeLimitMiddleware(_reads_everything, max_bytes=10)

    sent, reads = await run_with_chunks(middleware, [b"x" * 6, b"x" * 6, b"x" * 6, b"x" * 6])

    assert reads == 2
    assert sent[0]["status"] == 413


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_app_response_to_the_aborted_read_is_discarded() -> None:
    async def turns_errors_into_400(scope: Any, receive: Any, send: Any) -> None:
        try:
            await _reads_everything(scope, receive, send)
        except Exception:
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b"bad request"})

    middleware = RequestSizeLimitMiddleware(turns_errors_into_400, max_bytes=5)

    sent, _ = await run_with_chunks(middleware, [b"x" * 6, b""])

    assert [m.get("status") for m in sent if m["type"] == "http.response.start"] == [413]
    assert b"bad request" not in b"".join(m.get("body", b"") for m in sent)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_invalid_content_length_falls_back_to_counting() -> None:
    middleware = RequestSizeLimitMiddleware(_reads_everything, max_bytes=5)

    sent, _ = await run_with_chunks(
        middleware, [b"x" * 6, b""], headers=[(b"content-length", b"not-a-number")]
    )

    assert sent[0]["status"] == 413


@pytest.mark.db
def test_real_upload_over_the_request_limit_is_413_and_stores_nothing(
    make_api: Callable[..., Api],
) -> None:
    api = make_api(max_request_mb=1)

    response = api.upload(("big.pdf", b"%PDF-1.7\n" + b" " * (2 * MIB)))

    assert response.status_code == 413
    assert api.stored_files() == []


@pytest.mark.db
def test_real_chunked_upload_over_the_request_limit_is_413_not_400(
    make_api: Callable[..., Api],
) -> None:
    api = make_api(max_request_mb=1)
    boundary = "testboundary"
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="big.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode()

    def body() -> Any:
        yield head
        for _ in range(3):
            yield b"x" * MIB
        yield f"\r\n--{boundary}--\r\n".encode()

    response = api.client.post(
        f"{API}/documents",
        content=body(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Origin": ORIGIN},
    )

    assert response.status_code == 413
    assert "content-length" not in response.request.headers
    assert response.json() == {"detail": "The request exceeds the 1 MB limit."}
    assert response.headers["x-frame-options"] == "DENY"
    assert REQUEST_ID_HEADER in response.headers
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert api.stored_files() == []
