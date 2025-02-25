"""Detection orchestration: history in, scored rows and pending alerts out.

Keeps three concerns apart on purpose:

* :mod:`src.detector.zscore` decides whether a number is unusual.
* :mod:`src.detector.ensemble` offers a second opinion on borderline calls.
* This module owns the database, the per-service loop and alert creation.

Delivery is deliberately *not* here. Detection writes ``pending`` alert rows
and returns; :mod:`src.notifier.dispatcher` sends them. That split is what lets
a webhook outage be retried on a later poll instead of losing the alert.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import Settings, get_settings
from src.db.repositories import AlertRepository, BaselineRepository, CostRepository
from src.detector.ensemble import CostPoint, EnsembleVerdict, IsolationForestConfirmer
from src.detector.zscore import AnomalyResult, Direction, RollingZScoreDetector, Severity
from src.models.cost import Alert, AlertStatus, CostRecord
from src.utils.cost_explorer import ServiceCost

logger = logging.getLogger(__name__)

# A z-score this far past the threshold is sent even if the forest disagrees.
# At 3 sigma the two detectors are arguing about a judgement call; at 6 the
# z-score is describing something that needs a human either way.
ENSEMBLE_OVERRIDE_FACTOR = 2.0


@dataclass(slots=True)
class DetectionSummary:
    """What one detection pass did — returned by the API and logged by Lambda."""

    services_seen: int = 0
    records_upserted: int = 0
    days_evaluated: int = 0
    anomalies_found: int = 0
    ensemble_confirmations: int = 0
    ensemble_vetoes: int = 0
    ensemble_overrides: int = 0
    alerts_created: int = 0
    alerts_suppressed: int = 0
    drops_recorded: int = 0
    duration_seconds: float = 0.0
    anomalies: list[AnomalyResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "services_seen": self.services_seen,
            "records_upserted": self.records_upserted,
            "days_evaluated": self.days_evaluated,
            "anomalies_found": self.anomalies_found,
            "ensemble_confirmations": self.ensemble_confirmations,
            "ensemble_vetoes": self.ensemble_vetoes,
            "ensemble_overrides": self.ensemble_overrides,
            "alerts_created": self.alerts_created,
            "alerts_suppressed": self.alerts_suppressed,
            "drops_recorded": self.drops_recorded,
            "duration_seconds": round(self.duration_seconds, 3),
        }


def apply_ensemble(
    result: AnomalyResult, verdict: EnsembleVerdict, *, threshold: float
) -> tuple[AnomalyResult, str]:
    """Reconcile the z-score verdict with the forest's.

    Returns the possibly-amended result and a tag describing what happened:
    ``confirmed``, ``vetoed``, ``overridden``, ``unavailable`` or ``skipped``.
    """
    if not result.is_anomaly:
        return result, "skipped"

    if not verdict.available:
        return _annotate(result, "ensemble_unavailable", verdict.reason), "unavailable"

    if verdict.is_outlier:
        return _annotate(result, "ensemble_confirmed", verdict.reason), "confirmed"

    if abs(result.zscore) >= threshold * ENSEMBLE_OVERRIDE_FACTOR:
        return (
            _annotate(
                result,
                "ensemble_overridden",
                f"{verdict.reason}; z={result.zscore:+.2f} is past the override threshold",
            ),
            "overridden",
        )

    # Borderline hit the forest does not recognize as unusual for this series.
    # Recorded on the cost row, but no alert.
    downgraded = AnomalyResult(
        service=result.service,
        amount=result.amount,
        zscore=result.zscore,
        baseline_mean=result.baseline_mean,
        baseline_stddev=result.baseline_stddev,
        is_anomaly=False,
        direction=result.direction,
        severity=Severity.INFO,
        baseline=result.baseline,
        reason=f"{result.reason} — vetoed: {verdict.reason}",
        detector="ensemble",
        notes=[*result.notes, "ensemble_vetoed"],
    )
    return downgraded, "vetoed"


def _annotate(result: AnomalyResult, note: str, detail: str) -> AnomalyResult:
    return AnomalyResult(
        service=result.service,
        amount=result.amount,
        zscore=result.zscore,
        baseline_mean=result.baseline_mean,
        baseline_stddev=result.baseline_stddev,
        is_anomaly=result.is_anomaly,
        direction=result.direction,
        severity=result.severity,
        baseline=result.baseline,
        reason=f"{result.reason} — {detail}",
        detector="ensemble",
        notes=[*result.notes, note],
    )


def format_alert_message(result: AnomalyResult, usage_date: date) -> str:
    arrow = "up" if result.direction.value == "spike" else "down"
    return (
        f"{result.service} spend on {usage_date.isoformat()} is "
        f"${result.amount:,.2f}, {arrow} ${abs(result.delta):,.2f} "
        f"({result.delta_pct:+.0f}%) from a ${result.baseline_mean:,.2f} baseline. "
        f"z={result.zscore:+.2f}, sigma=${result.baseline_stddev:,.2f}. {result.reason}."
    )


class DetectionService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.costs = CostRepository(session)
        self.baselines = BaselineRepository(session)
        self.alerts = AlertRepository(session)

    async def run(self, observations: list[ServiceCost]) -> DetectionSummary:
        """Score a batch of service-days, persist them, and queue alerts."""
        started = time.perf_counter()
        summary = DetectionSummary()

        by_service: dict[str, list[ServiceCost]] = defaultdict(list)
        for observation in observations:
            by_service[observation.service].append(observation)
        summary.services_seen = len(by_service)

        for service, batch in by_service.items():
            await self._process_service(service, batch, summary)

        summary.duration_seconds = time.perf_counter() - started
        logger.info("Detection pass complete: %s", summary.as_dict())
        return summary

    async def _process_service(
        self, service: str, batch: list[ServiceCost], summary: DetectionSummary
    ) -> None:
        batch.sort(key=lambda o: o.usage_date)
        earliest = batch[0].usage_date

        # Stored history strictly before the batch. Days inside the batch range
        # are re-scored from the batch itself, since Cost Explorer restates
        # them and the stored figure may be stale.
        stored = await self.costs.history(
            service, before=earliest, limit=self.settings.window_size * 2
        )
        series: list[CostPoint] = [
            CostPoint(record.usage_date, record.amount_usd) for record in stored
        ]

        detector = RollingZScoreDetector(service, settings=self.settings)
        detector.load_history([point.amount for point in series])

        confirmer: IsolationForestConfirmer | None = None
        if self.settings.ensemble_enabled:
            confirmer = IsolationForestConfirmer()
            confirmer.fit(series)

        refit_counter = 0
        for observation in batch:
            amount = observation.amount_usd
            candidate = CostPoint(observation.usage_date, amount)

            result = detector.evaluate(amount)
            summary.days_evaluated += 1

            if confirmer is not None and result.is_anomaly:
                # Refit lazily: only a day that the z-score already flagged is
                # worth the cost of a forest, and a 45-day backfill would
                # otherwise fit 45 of them per service.
                if refit_counter:
                    confirmer.fit(series)
                    refit_counter = 0
                result, tag = apply_ensemble(
                    result, confirmer.score(candidate), threshold=self.settings.zscore_threshold
                )
                if tag == "confirmed":
                    summary.ensemble_confirmations += 1
                elif tag == "vetoed":
                    summary.ensemble_vetoes += 1
                elif tag == "overridden":
                    summary.ensemble_overrides += 1

            record = await self.costs.upsert(
                service=service,
                usage_date=observation.usage_date,
                amount=observation.amount,
                unit=observation.unit,
                zscore=result.zscore,
                baseline_mean=result.baseline_mean,
                baseline_stddev=result.baseline_stddev,
                is_anomaly=result.is_anomaly,
                detector=result.detector,
            )
            summary.records_upserted += 1

            if result.is_anomaly:
                summary.anomalies_found += 1
                summary.anomalies.append(result)
                if result.direction is Direction.DROP and not self.settings.alert_on_drops:
                    # Still flagged on the cost row and visible in the API, just
                    # not paged. See Settings.alert_on_drops.
                    summary.drops_recorded += 1
                    logger.info(
                        "Drop recorded without alert: %s %s z=%+.2f",
                        service,
                        observation.usage_date,
                        result.zscore,
                    )
                else:
                    await self._queue_alert(record, result, summary)

            series.append(candidate)
            refit_counter += 1

        await self._persist_baseline(service, detector, batch[-1].usage_date)

    async def _queue_alert(
        self, record: CostRecord, result: AnomalyResult, summary: DetectionSummary
    ) -> None:
        """Create a pending alert unless this service is inside its cooldown.

        A cost spike usually lasts several days — a runaway instance, a
        forgotten NAT gateway. Without a cooldown the same incident alerts on
        every poll for a week, and people start filtering the channel.
        """
        cooldown = timedelta(hours=self.settings.alert_cooldown_hours)
        status = AlertStatus.PENDING
        suppressed_reason = ""

        if cooldown:
            previous = await self.alerts.last_alert_for_service(result.service)
            if previous is not None:
                created = previous.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - created < cooldown:
                    status = AlertStatus.SUPPRESSED
                    suppressed_reason = (
                        f"within {self.settings.alert_cooldown_hours:g}h cooldown of "
                        f"alert {previous.id}"
                    )

        alert = Alert(
            cost_record_id=record.id,
            service=result.service,
            usage_date=record.usage_date,
            amount=Decimal(str(result.amount)),
            zscore=result.zscore,
            baseline_mean=result.baseline_mean,
            baseline_stddev=result.baseline_stddev,
            severity=result.severity.value,
            status=status,
            message=format_alert_message(result, record.usage_date),
            last_error=suppressed_reason or None,
        )
        await self.alerts.create(alert)

        if status is AlertStatus.SUPPRESSED:
            summary.alerts_suppressed += 1
            logger.info("Alert suppressed for %s: %s", result.service, suppressed_reason)
        else:
            summary.alerts_created += 1

    async def _persist_baseline(
        self, service: str, detector: RollingZScoreDetector, last_date: date
    ) -> None:
        stats = detector.baseline()
        if stats.sample_count == 0:
            return
        await self.baselines.upsert(
            service=service,
            window_size=detector.window_size,
            sample_count=stats.sample_count,
            mean=stats.mean,
            stddev=stats.stddev,
            median=stats.median,
            mad=stats.mad,
            p95=stats.p95,
            last_amount=detector.history[-1],
            last_evaluated_date=last_date,
        )
