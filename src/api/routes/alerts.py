"""Alert history, delivery forensics, manual retry, and a config smoke test."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.api.schemas import (
    AlertOut,
    Page,
    RetryResponse,
    TestAlertRequest,
    TestAlertResponse,
)
from src.config import Settings, get_settings
from src.db.repositories import AlertRepository
from src.db.session import get_db
from src.detector.zscore import AnomalyResult, Direction, Severity
from src.models.cost import Alert, AlertStatus
from src.notifier.alert import send_payload
from src.notifier.channels import build_channels
from src.notifier.dispatcher import AlertDispatcher
from src.notifier.payload import build_payload

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/", response_model=Page[AlertOut], summary="List alerts")
async def list_alerts(
    status_filter: AlertStatus | None = Query(
        None, alias="status", description="pending | delivered | partial | failed | suppressed"
    ),
    service: str | None = Query(None),
    delivered: bool | None = Query(
        None, description="Shorthand: true for delivered only, false for everything else"
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[AlertOut]:
    alerts, total = await AlertRepository(db).list_alerts(
        status=status_filter.value if status_filter else None,
        service=service,
        delivered=delivered,
        limit=limit,
        offset=offset,
    )
    return Page(
        items=[AlertOut.from_alert(alert) for alert in alerts],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/test",
    response_model=TestAlertResponse,
    summary="Send a synthetic alert through the real channels",
)
async def send_test_alert(
    request: TestAlertRequest | None = None,
    settings: Settings = Depends(get_settings),
) -> TestAlertResponse:
    """Proves webhook URLs, SES verification and SMTP credentials actually work.

    Nothing is persisted — this is a configuration check, not an anomaly.
    """
    request = request or TestAlertRequest()
    channels = build_channels(settings)
    if not channels:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no alert channels configured; set ALERT_WEBHOOK_URL or ALERT_EMAIL_TRANSPORT",
        )

    delta = request.amount - request.baseline_mean
    result = AnomalyResult(
        service=request.service,
        amount=request.amount,
        zscore=delta / request.baseline_stddev,
        baseline_mean=request.baseline_mean,
        baseline_stddev=request.baseline_stddev,
        is_anomaly=True,
        direction=Direction.SPIKE if delta >= 0 else Direction.DROP,
        severity=Severity.INFO,
        reason="synthetic alert from POST /alerts/test — no anomaly was detected",
        detector="manual",
    )
    payload = build_payload(result)
    delivered = await send_payload(payload, settings=settings, channels=channels)
    return TestAlertResponse(
        delivered=delivered,
        channels=[channel.name.value for channel in channels],
        subject=payload.subject,
    )


@router.get("/{alert_id}", response_model=AlertOut, summary="One alert with its attempt log")
async def get_alert(alert_id: str, db: AsyncSession = Depends(get_db)) -> AlertOut:
    # selectinload avoids a lazy load on delivery_attempts, which would raise
    # MissingGreenlet under the async session.
    alert = (
        await db.execute(
            select(Alert).where(Alert.id == alert_id).options(selectinload(Alert.delivery_attempts))
        )
    ).scalar_one_or_none()
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="alert not found")
    return AlertOut.from_alert(alert, include_attempts=True)


@router.post("/{alert_id}/retry", response_model=RetryResponse, summary="Retry delivery now")
async def retry_alert(
    alert_id: str,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RetryResponse:
    """Re-attempt a failed alert immediately, ignoring its backoff schedule."""
    repo = AlertRepository(db)
    alert = await repo.get(alert_id)
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="alert not found")
    if alert.status == AlertStatus.DELIVERED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"alert already delivered at {alert.delivered_at}",
        )

    dispatcher = AlertDispatcher(db, settings)
    try:
        outcome = await dispatcher.dispatch(alert)
    finally:
        await dispatcher.aclose()

    return RetryResponse(
        alert_id=alert.id,
        status=outcome.status.value,
        attempts=alert.attempts,
        channels=[
            {
                "channel": channel.channel,
                "succeeded": channel.succeeded,
                "attempts": channel.attempts,
                "status_code": channel.status_code,
                "error": channel.error,
            }
            for channel in outcome.channels
        ],
    )


@router.post("/sweep", response_model=list[RetryResponse], summary="Retry everything that is due")
async def sweep_alerts(
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[RetryResponse]:
    """Flush the retry backlog without waiting for the next scheduled poll."""
    dispatcher = AlertDispatcher(db, settings)
    try:
        outcomes = await dispatcher.sweep(limit=limit)
    finally:
        await dispatcher.aclose()

    return [
        RetryResponse(
            alert_id=outcome.alert_id,
            status=outcome.status.value,
            attempts=outcome.total_attempts,
            channels=[
                {
                    "channel": channel.channel,
                    "succeeded": channel.succeeded,
                    "attempts": channel.attempts,
                    "status_code": channel.status_code,
                    "error": channel.error,
                }
                for channel in outcome.channels
            ],
        )
        for outcome in outcomes
    ]
