"""One poll cycle: fetch, detect, deliver, sweep.

The single place that sequences the whole job, so the API endpoint, the Lambda
handler and the long-running scheduler cannot drift apart.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import Settings, get_settings
from src.detector.service import DetectionService, DetectionSummary
from src.notifier.dispatcher import AlertDispatcher
from src.utils.cost_explorer import CostDataProvider, get_cost_provider

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PollResult:
    detection: DetectionSummary
    observations_fetched: int = 0
    delivered: int = 0
    failed: int = 0
    swept: int = 0
    swept_delivered: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        # Detection is nested rather than merged: both summaries carry a
        # duration_seconds, and flattening let the inner one silently
        # overwrite the cycle's own.
        return {
            "observations_fetched": self.observations_fetched,
            "delivered": self.delivered,
            "failed": self.failed,
            "swept": self.swept,
            "swept_delivered": self.swept_delivered,
            "duration_seconds": round(self.duration_seconds, 3),
            "errors": self.errors,
            "detection": self.detection.as_dict(),
        }


async def run_poll_cycle(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    provider: CostDataProvider | None = None,
    dispatcher: AlertDispatcher | None = None,
    lookback_days: int | None = None,
    end_date: date | None = None,
    deliver: bool = True,
) -> PollResult:
    """Fetch recent spend, score it, and deliver whatever it produced.

    Sweeping previously-failed alerts happens *first*. A caller that trips over
    a new detection error should still have flushed the retry backlog, and the
    sweep is cheap when the backlog is empty.
    """
    settings = settings or get_settings()
    provider = provider or get_cost_provider(settings)
    lookback = lookback_days if lookback_days is not None else settings.poll_lookback_days
    started = time.perf_counter()

    result = PollResult(detection=DetectionSummary())
    # Caller-supplied dispatchers are not closed here; whoever built it owns it.
    owns_dispatcher = dispatcher is None
    dispatcher = dispatcher or AlertDispatcher(session, settings)

    try:
        if deliver:
            swept = await dispatcher.sweep()
            result.swept = len(swept)
            result.swept_delivered = sum(1 for outcome in swept if outcome.delivered)

        logger.info("Fetching %d days of cost data", lookback)
        observations = provider.fetch_daily_costs(days_back=lookback, end_date=end_date)
        result.observations_fetched = len(observations)

        if not observations:
            logger.warning("Cost provider returned no data for the requested window")
            return result

        result.detection = await DetectionService(session, settings).run(
            observations, suppress_alerts=not deliver
        )

        if deliver and result.detection.alerts_created:
            outcomes = await dispatcher.dispatch_pending()
            result.delivered = sum(1 for outcome in outcomes if outcome.delivered)
            result.failed = len(outcomes) - result.delivered
    finally:
        if owns_dispatcher:
            await dispatcher.aclose()
        # In a finally block so a CostExplorerError still leaves a measured
        # duration on the result the caller logs.
        result.duration_seconds = time.perf_counter() - started

    logger.info("Poll cycle finished: %s", result.as_dict())
    return result


async def backfill(
    session: AsyncSession,
    *,
    days: int = 180,
    settings: Settings | None = None,
    provider: CostDataProvider | None = None,
    dispatcher: AlertDispatcher | None = None,
    chunk_days: int = 60,
) -> PollResult:
    """Seed history without alerting on any of it.

    A new deployment has no baseline, so every service's first scored days are
    judged against a window that is still filling. Running the backfill with
    delivery off means those early verdicts land in the database for context
    without paging anyone about spend from three months ago.

    Cost Explorer caps DAILY granularity requests, so the range is walked in
    chunks rather than asked for in one call.
    """
    settings = settings or get_settings()
    provider = provider or get_cost_provider(settings)
    combined = PollResult(detection=DetectionSummary())
    started = time.perf_counter()
    today = date.today()

    for offset in range(days, 0, -chunk_days):
        window = min(chunk_days, offset)
        chunk_end = today - timedelta(days=offset - window)
        logger.info("Backfilling %d days ending %s", window, chunk_end)
        chunk = await run_poll_cycle(
            session,
            settings=settings,
            provider=provider,
            dispatcher=dispatcher,
            lookback_days=window,
            end_date=chunk_end,
            deliver=False,
        )
        combined.observations_fetched += chunk.observations_fetched
        combined.detection.records_upserted += chunk.detection.records_upserted
        combined.detection.days_evaluated += chunk.detection.days_evaluated
        combined.detection.anomalies_found += chunk.detection.anomalies_found
        combined.detection.alerts_created += chunk.detection.alerts_created
        combined.detection.alerts_suppressed += chunk.detection.alerts_suppressed
        combined.detection.anomalies.extend(chunk.detection.anomalies)
        combined.detection.duration_seconds += chunk.detection.duration_seconds
        # Chunks cover the same account, so the service count is the widest
        # chunk's rather than the sum.
        combined.detection.services_seen = max(
            combined.detection.services_seen, chunk.detection.services_seen
        )

    combined.duration_seconds = time.perf_counter() - started
    return combined
