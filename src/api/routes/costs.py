"""Cost history, baselines, the rollup, and the manual poll trigger."""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import (
    BaselineOut,
    CostRecordOut,
    DailyTotal,
    Page,
    PollRequest,
    PollResponse,
    ServiceSpend,
    SummaryResponse,
)
from src.config import Settings, get_settings
from src.db.repositories import AlertRepository, BaselineRepository, CostRepository
from src.db.session import get_db, session_scope
from src.pipeline import run_poll_cycle

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/", response_model=Page[CostRecordOut], summary="List cost records")
async def list_costs(
    service: str | None = Query(None, description="Exact AWS service name"),
    days: int = Query(30, ge=1, le=365, description="Look-back window; ignored if start/end given"),
    start: date | None = Query(None, description="Inclusive start of an explicit range"),
    end: date | None = Query(None, description="Inclusive end of an explicit range"),
    anomalies_only: bool = Query(False, description="Only rows the detector flagged"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[CostRecordOut]:
    if start and end and start > end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="start must not be after end",
        )

    records, total = await CostRepository(db).list_records(
        service=service,
        days=days,
        start=start,
        end=end,
        anomalies_only=anomalies_only,
        limit=limit,
        offset=offset,
    )
    return Page(
        items=[CostRecordOut.from_record(record) for record in records],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/services", response_model=list[str], summary="Services with recorded spend")
async def list_services(db: AsyncSession = Depends(get_db)) -> list[str]:
    return await CostRepository(db).services()


@router.get("/summary", response_model=SummaryResponse, summary="Spend and anomaly rollup")
async def summary(
    days: int = Query(30, ge=1, le=365),
    top: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> SummaryResponse:
    costs = CostRepository(db)
    daily = await costs.daily_totals(days=days)
    top_services = await costs.spend_by_service(days=days, limit=top)
    _, anomaly_count = await costs.list_records(days=days, anomalies_only=True, limit=1)
    services = await costs.services()

    return SummaryResponse(
        window_days=days,
        total_spend=round(sum(total for _, total in daily), 2),
        services_tracked=len(services),
        anomalies=anomaly_count,
        alerts_by_status=await AlertRepository(db).counts_by_status(),
        top_services=[
            ServiceSpend(service=name, total=round(total, 2)) for name, total in top_services
        ],
        daily_totals=[DailyTotal(usage_date=day, total=round(total, 2)) for day, total in daily],
    )


@router.get(
    "/{service}/baseline",
    response_model=BaselineOut,
    summary="Current rolling baseline for a service",
)
async def get_baseline(service: str, db: AsyncSession = Depends(get_db)) -> BaselineOut:
    baseline = await BaselineRepository(db).get(service)
    if baseline is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no baseline for {service!r}; it may not have been polled yet",
        )
    return BaselineOut.model_validate(baseline)


@router.get("/baselines", response_model=Page[BaselineOut], summary="All service baselines")
async def list_baselines(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[BaselineOut]:
    baselines = await BaselineRepository(db).list_all(limit=limit, offset=offset)
    return Page(
        items=[BaselineOut.model_validate(b) for b in baselines],
        total=len(baselines),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{service}/history",
    response_model=list[CostRecordOut],
    summary="Chronological history for one service",
)
async def service_history(
    service: str,
    limit: int = Query(90, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> list[CostRecordOut]:
    records = await CostRepository(db).history(service, limit=limit)
    if not records:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no history for {service!r}"
        )
    return [CostRecordOut.from_record(record) for record in records]


@router.post("/poll", response_model=PollResponse, summary="Run a poll cycle now")
async def trigger_poll(
    request: PollRequest | None = None,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PollResponse:
    """Fetch, score, store and deliver synchronously.

    EventBridge drives the scheduled run; this exists for backfills, for
    verifying credentials after a deploy, and for the demo.
    """
    request = request or PollRequest()
    result = await run_poll_cycle(
        db,
        settings=settings,
        lookback_days=request.lookback_days,
        deliver=request.deliver,
    )
    return PollResponse.model_validate(result.as_dict())


@router.post(
    "/poll/async",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a poll cycle and return immediately",
)
async def trigger_poll_async(
    background: BackgroundTasks,
    request: PollRequest | None = None,
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """A full poll can outlive a client's timeout on a large account.

    The background task opens its own session: the request-scoped one is
    closed as soon as this handler returns.
    """
    request = request or PollRequest()

    async def run() -> None:
        try:
            async with session_scope() as session:
                await run_poll_cycle(
                    session,
                    settings=settings,
                    lookback_days=request.lookback_days,
                    deliver=request.deliver,
                )
        except Exception:
            logger.exception("Background poll cycle failed")

    background.add_task(run)
    return {"status": "accepted", "detail": "poll cycle queued"}
