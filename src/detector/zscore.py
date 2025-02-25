"""Rolling z-score detection over per-service daily spend.

The detector is deliberately stateless with respect to storage: it is handed a
history window and a candidate point and returns a verdict. Persistence lives
in :mod:`src.detector.service`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np

from src.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Scaling that makes the median absolute deviation a consistent estimator of
# the standard deviation for normally distributed data: 1 / Phi^-1(0.75).
MAD_TO_STDDEV = 1.4826


class Direction(StrEnum):
    SPIKE = "spike"
    DROP = "drop"
    NORMAL = "normal"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class BaselineStats:
    """What "normal" looks like for one service over the current window."""

    sample_count: int
    mean: float
    stddev: float
    median: float
    mad: float
    """Median absolute deviation, already scaled to stddev units."""
    p95: float
    minimum: float
    maximum: float

    @property
    def is_degenerate(self) -> bool:
        """A flat window — every day identical. Any deviation is infinitely
        many sigmas, so the z-score carries no information."""
        return self.stddev <= 0 and self.mad <= 0

    @classmethod
    def from_window(cls, window: list[float] | np.ndarray) -> BaselineStats:
        values = np.asarray(window, dtype=np.float64)
        if values.size == 0:
            return cls(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

        median = float(np.median(values))
        mad = float(np.median(np.abs(values - median))) * MAD_TO_STDDEV
        return cls(
            sample_count=int(values.size),
            mean=float(np.mean(values)),
            # ddof=1: this is a sample of a service's spend, not the population.
            stddev=float(np.std(values, ddof=1)) if values.size > 1 else 0.0,
            median=median,
            mad=mad,
            p95=float(np.percentile(values, 95)),
            minimum=float(np.min(values)),
            maximum=float(np.max(values)),
        )


@dataclass(frozen=True, slots=True)
class AnomalyResult:
    service: str
    amount: float
    zscore: float
    baseline_mean: float
    baseline_stddev: float
    is_anomaly: bool
    direction: Direction = Direction.NORMAL
    severity: Severity = Severity.INFO
    baseline: BaselineStats | None = None
    reason: str = ""
    """Why this verdict was reached — surfaced in the API and the alert body."""
    detector: str = "robust_zscore"
    notes: list[str] = field(default_factory=list)

    @property
    def delta(self) -> float:
        return self.amount - self.baseline_mean

    @property
    def delta_pct(self) -> float:
        if self.baseline_mean == 0:
            return 0.0
        return (self.delta / self.baseline_mean) * 100.0


class RollingZScoreDetector:
    """Scores a day's spend against the trailing window for one service.

    Two guards separate this from a textbook z-score, and both exist because
    the textbook version is unusable on real cost data:

    *Robust centre and scale.* With mean/stddev, one past spike inflates the
    baseline enough to hide the next one — exactly the case you care about.
    Median and a MAD-derived scale are unmoved by a handful of outliers.

    *An absolute floor.* A service billing $0.01/day that moves to $0.08 is a
    20-sigma event and $0.07 of money. ``min_absolute_delta_usd`` keeps it out
    of the alert stream.
    """

    def __init__(
        self,
        service: str,
        *,
        window: int | None = None,
        threshold: float | None = None,
        min_history: int | None = None,
        min_absolute_delta: float | None = None,
        robust: bool | None = None,
        settings: Settings | None = None,
    ) -> None:
        settings = settings or get_settings()
        self.service = service
        self.window_size = window if window is not None else settings.window_size
        self.threshold = threshold if threshold is not None else settings.zscore_threshold
        self.min_history = min_history if min_history is not None else settings.min_history_days
        self.min_absolute_delta = (
            min_absolute_delta
            if min_absolute_delta is not None
            else settings.min_absolute_delta_usd
        )
        self.robust = robust if robust is not None else settings.robust_zscore
        self._history: list[float] = []

    # --- window management -------------------------------------------------

    def load_history(self, amounts: list[float]) -> RollingZScoreDetector:
        """Seed with historical daily amounts, oldest first. Returns self."""
        self._history = [float(a) for a in amounts][-self.window_size :]
        return self

    def observe(self, amount: float) -> None:
        """Append a point to the window without scoring it."""
        self._history.append(float(amount))
        if len(self._history) > self.window_size:
            del self._history[: len(self._history) - self.window_size]

    @property
    def history(self) -> list[float]:
        return list(self._history)

    def baseline(self) -> BaselineStats:
        return BaselineStats.from_window(self._history)

    # --- scoring -----------------------------------------------------------

    def evaluate(self, amount: float, *, observe: bool = True) -> AnomalyResult:
        """Score ``amount`` against the current window.

        The point is appended to the window afterwards (unless ``observe`` is
        false) so it never contributes to the baseline it is judged against.
        """
        amount = float(amount)
        stats = self.baseline()

        try:
            if stats.sample_count < self.min_history:
                return self._verdict(
                    amount,
                    0.0,
                    stats,
                    is_anomaly=False,
                    reason=(
                        f"insufficient history: {stats.sample_count} of "
                        f"{self.min_history} days required"
                    ),
                )

            scale = stats.mad if self.robust else stats.stddev
            centre = stats.median if self.robust else stats.mean

            if scale <= 0:
                # A perfectly flat window. Fall back to the non-robust scale if
                # it has any spread, otherwise judge on relative change alone.
                scale = stats.stddev
                if scale <= 0:
                    return self._flat_window_verdict(amount, stats, centre)

            zscore = (amount - centre) / scale

            if abs(zscore) < self.threshold:
                return self._verdict(
                    amount,
                    zscore,
                    stats,
                    is_anomaly=False,
                    reason=f"within {self.threshold:.1f} sigma of baseline",
                )

            if abs(amount - centre) < self.min_absolute_delta:
                return self._verdict(
                    amount,
                    zscore,
                    stats,
                    is_anomaly=False,
                    reason=(
                        f"z={zscore:.2f} exceeds threshold but the move is only "
                        f"${abs(amount - centre):.2f}, below the "
                        f"${self.min_absolute_delta:.2f} floor"
                    ),
                    notes=["suppressed_by_absolute_floor"],
                )

            direction = Direction.SPIKE if zscore > 0 else Direction.DROP
            return self._verdict(
                amount,
                zscore,
                stats,
                is_anomaly=True,
                direction=direction,
                severity=self._grade(zscore, amount, centre),
                reason=(
                    f"{direction.value} of ${abs(amount - centre):.2f} "
                    f"({zscore:+.2f} sigma) against a ${centre:.2f} baseline"
                ),
            )
        finally:
            if observe:
                self.observe(amount)

    def _flat_window_verdict(
        self, amount: float, stats: BaselineStats, centre: float
    ) -> AnomalyResult:
        """Every historical day was identical, so sigma is undefined.

        Judge on relative change with a hard dollar floor instead of reporting
        an infinite z-score.
        """
        delta = amount - centre
        relative = abs(delta) / centre if centre else math.inf
        significant = relative >= 0.5 and abs(delta) >= self.min_absolute_delta
        return self._verdict(
            amount,
            0.0,
            stats,
            is_anomaly=significant,
            direction=(
                Direction.NORMAL
                if not significant
                else (Direction.SPIKE if delta > 0 else Direction.DROP)
            ),
            severity=Severity.WARNING if significant else Severity.INFO,
            reason=(
                f"flat baseline (${centre:.2f} every day); "
                f"{'significant' if significant else 'negligible'} "
                f"{relative * 100:.0f}% change"
            ),
            notes=["degenerate_baseline"],
        )

    def _grade(self, zscore: float, amount: float, centre: float) -> Severity:
        """Critical for very large sigma, or for a big move in real money."""
        magnitude = abs(zscore)
        delta = abs(amount - centre)
        if magnitude >= self.threshold * 2 or delta >= max(centre, 1.0) * 2:
            return Severity.CRITICAL
        return Severity.WARNING

    def _verdict(
        self,
        amount: float,
        zscore: float,
        stats: BaselineStats,
        *,
        is_anomaly: bool,
        direction: Direction = Direction.NORMAL,
        severity: Severity = Severity.INFO,
        reason: str = "",
        notes: list[str] | None = None,
    ) -> AnomalyResult:
        return AnomalyResult(
            service=self.service,
            amount=amount,
            zscore=zscore,
            # Reported baseline tracks whichever centre/scale actually scored
            # the point, so a z-score in the API can be recomputed from it.
            baseline_mean=stats.median if self.robust else stats.mean,
            baseline_stddev=stats.mad if self.robust else stats.stddev,
            is_anomaly=is_anomaly,
            direction=direction,
            severity=severity,
            baseline=stats,
            reason=reason,
            detector="robust_zscore" if self.robust else "zscore",
            notes=notes or [],
        )


# Retained so existing imports keep working after the rename.
ZScoreDetector = RollingZScoreDetector
