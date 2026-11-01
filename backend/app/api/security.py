import json
import logging
from typing import cast

from fastapi.exceptions import RequestValidationError
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

UNEXPECTED_ERROR = "An unexpected error occurred."
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


async def validation_error_response(_: Request, exc: Exception) -> Response:
    """FastAPI's default 422 echoes the input, and crashes on text that UTF-8 cannot encode.

    json.dumps escapes such text wherever it appears, including in the names of unknown fields.
    """
    errors = cast(RequestValidationError, exc).errors()
    detail = [{"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in errors]
    return Response(json.dumps({"detail": detail}), 422, media_type="application/json")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                if headers.get("content-type", "").startswith("application/json"):
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _BodyTooLarge(Exception):
    pass


class RequestSizeLimitMiddleware:
    """Starlette spools a whole multipart body before a route runs, so the limit must act here.

    Content-Length is checked first. It can be absent or wrong, so bytes are also counted as they
    arrive, which stops a chunked upload as soon as it passes the limit.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > self.max_bytes:
            await self._reject(scope, receive, send)
            return

        received = 0
        too_large = False
        response_started = False

        async def counting_receive() -> Message:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    too_large = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            # FastAPI turns any error while reading a body into a 400. The client must not get that.
            if too_large and not response_started:
                return
            response_started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except _BodyTooLarge:
            if response_started:
                raise
        if too_large and not response_started:
            await self._reject(scope, receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit_mb = self.max_bytes // (1024 * 1024)
        response = JSONResponse({"detail": f"The request exceeds the {limit_mb} MB limit."}, 413)
        await response(scope, receive, send)


class UnexpectedErrorMiddleware:
    """Log an unhandled exception and return a 500 that reveals nothing about it.

    An exception handler would run outside all other middleware, losing the request id and CORS
    headers. This sits inside them, so the response still gets those.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            logger.exception("unhandled error", extra={"path": scope["path"]})
            if response_started:
                raise
            await JSONResponse({"detail": UNEXPECTED_ERROR}, 500)(scope, receive, send)
