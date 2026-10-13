"""FastAPI application factory."""

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.errors import error_response, register_exception_handlers
from app.core.logging import configure_logging, request_id_ctx
from app.db.session import dispose_engine

API_V1_PREFIX = "/api/v1"

logger = logging.getLogger("app.request")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    dispose_engine()


def create_app() -> FastAPI:
    # Raises ConfigurationError naming every missing/invalid setting.
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="SIGMANIX SPACES API",
        description="Property management backend (Phase 1: Property module).",
        version=__version__,
        openapi_url=f"{API_V1_PREFIX}/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = uuid.uuid4().hex
        token = request_id_ctx.set(request_id)
        started = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception:
                # The traceback goes to the logs only; clients get a generic message.
                logger.exception("Unhandled error")
                response = error_response(500, "internal_error", "An unexpected error occurred.")
            response.headers["X-Request-ID"] = request_id
            # Path only: query strings and headers may carry personal data or tokens.
            logger.info(
                "request",
                extra={
                    "context": {
                        "method": request.method,
                        "path": request.url.path,
                        "status": response.status_code,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    }
                },
            )
            return response
        finally:
            request_id_ctx.reset(token)

    # Added last so it wraps everything, including error responses.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=API_V1_PREFIX)
    return app


app = create_app()
