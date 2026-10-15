"""FastAPI application factory."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from src.api.routes import alerts, costs, health
from src.config import Settings, get_settings
from src.db.session import dispose_engine, init_db
from src.utils.cost_explorer import CostExplorerError

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "x-request-id"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        force=True,
    )
    # botocore logs the full signed request at DEBUG, which is noisy and
    # includes header material we would rather not have in logs.
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings)
    logger.info(
        "Starting Sentinel %s (provider=%s, threshold=%.1f sigma, window=%dd)",
        settings.api_version,
        settings.cost_provider.value,
        settings.zscore_threshold,
        settings.window_size,
    )
    await init_db()
    try:
        yield
    finally:
        await dispose_engine()
        logger.info("Sentinel shut down")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title=settings.api_title,
        version=settings.api_version,
        lifespan=lifespan,
        description=(
            "Polls AWS Cost Explorer, models per-service spend baselines with a rolling "
            "z-score detector confirmed by an IsolationForest, and delivers webhook and "
            "email alerts with exponential backoff retry."
        ),
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[JSONResponse]]
    ) -> JSONResponse:
        """Attach a request id and server timing to every response.

        The id is echoed on error responses too, which is what makes a report
        of "it 500d" traceable to a log line.
        """
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        started = time.perf_counter()

        response = await call_next(request)

        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers["x-response-time-ms"] = f"{elapsed_ms:.1f}"
        if elapsed_ms > 1000:
            logger.warning(
                "Slow request %s %s took %.0fms (request_id=%s)",
                request.method,
                request.url.path,
                elapsed_ms,
                request_id,
            )
        return response

    @app.exception_handler(CostExplorerError)
    async def cost_explorer_error(request: Request, exc: CostExplorerError) -> JSONResponse:
        """Upstream AWS failure is 502, not 500 — it is not our bug to fix."""
        logger.error("Cost Explorer failure on %s: %s", request.url.path, exc)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "detail": str(exc),
                "error": "cost_explorer_unavailable",
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    @app.exception_handler(SQLAlchemyTimeoutError)
    async def pool_exhausted(request: Request, exc: SQLAlchemyTimeoutError) -> JSONResponse:
        """Connection pool exhausted — shed load, do not call it a server error.

        A saturated pool means the instance is over capacity, not broken. 503
        with Retry-After lets a load balancer take the instance out of
        rotation and a client back off; a 500 tells both of them to treat it
        as a bug and keep hammering. Surfaced by a load test at 5,000
        concurrent users, where this accounted for 1.57% of responses.
        """
        logger.warning("Connection pool exhausted on %s: %s", request.url.path, exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"retry-after": "2"},
            content={
                "detail": "database connection pool exhausted; retry shortly",
                "error": "capacity_exceeded",
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "detail": exc.errors(),
                "error": "validation_error",
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        # The exception text can carry a connection string or a signed URL, so
        # the client gets the request id and the detail goes to the log.
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": "internal server error",
                "error": "internal_error",
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    app.include_router(health.router, tags=["health"])
    app.include_router(costs.router, prefix="/costs", tags=["costs"])
    app.include_router(alerts.router, prefix="/alerts", tags=["alerts"])
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def root() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
