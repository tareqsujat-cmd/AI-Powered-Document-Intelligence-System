from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import corrections, documents, health, metrics, search
from app.api.security import (
    RequestSizeLimitMiddleware,
    SecurityHeadersMiddleware,
    UnexpectedErrorMiddleware,
    validation_error_response,
)
from app.core.config import Settings, get_settings
from app.core.logging import RequestContextMiddleware, configure_logging

API_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)

    app = FastAPI(title="Document Intelligence API", version=__version__)
    app.add_exception_handler(RequestValidationError, validation_error_response)
    # The last one added runs first. Errors are handled innermost, so that error responses get the
    # headers from every other middleware.
    app.add_middleware(UnexpectedErrorMiddleware)
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=settings.max_request_mb * 1024 * 1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "Retry-After", "Content-Disposition"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    app.include_router(documents.upload_router, prefix=API_PREFIX)
    app.include_router(documents.router, prefix=API_PREFIX)
    app.include_router(corrections.router, prefix=API_PREFIX)
    app.include_router(metrics.router, prefix=API_PREFIX)
    app.include_router(search.router, prefix=API_PREFIX)
    return app
