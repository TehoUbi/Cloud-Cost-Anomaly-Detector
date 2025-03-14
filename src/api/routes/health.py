"""Liveness and readiness.

Split deliberately: ``/health`` must not touch the database. A load balancer
that health-checks the DB will take every replica out of service the moment
Postgres hiccups, turning a degraded dependency into a total outage.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import HealthResponse, ReadinessResponse
from src.config import Settings, get_settings
from src.db.session import get_db
from src.notifier.channels import build_channels

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    """Is the process up? No dependencies consulted."""
    return HealthResponse(
        status="ok",
        version=settings.api_version,
        timestamp=datetime.now(UTC),
    )


@router.get("/ready", response_model=ReadinessResponse, summary="Readiness probe")
async def ready(
    response: Response,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ReadinessResponse:
    """Can this instance actually serve? Returns 503 if the database is gone."""
    database = "ok"
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        logger.error("Readiness check failed: %s", exc)
        database = f"error: {type(exc).__name__}"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ready" if database == "ok" else "degraded",
        database=database,
        cost_provider=settings.cost_provider.value,
        alert_channels=[channel.name.value for channel in build_channels(settings)],
        timestamp=datetime.now(UTC),
    )
