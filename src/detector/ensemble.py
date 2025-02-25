"""scikit-learn confirmation stage for z-score hits.

A rolling z-score is univariate: it sees today's dollar figure and a trailing
window, nothing else. That makes it loud on series whose shape is normal but
whose distribution is not — a Lambda bill that is genuinely spiky on Mondays,
a CloudFront line that triples whenever a release ships.

This stage fits an :class:`~sklearn.ensemble.IsolationForest` on engineered
features of the same history (level, log level, ratio to the trailing median,
day-over-day change, deviation from the same weekday) and uses it to confirm
or veto a z-score hit. It is a second opinion, never the primary signal: a
veto only applies to borderline hits, and anything far past the threshold goes
through regardless of what the forest thinks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import RobustScaler

logger = logging.getLogger(__name__)

FEATURE_NAMES = (
    "amount",
    "log_amount",
    "ratio_to_trailing_median",
    "day_over_day_delta",
    "weekday_deviation",
)

# Below this many usable rows the forest has nothing to learn from and abstains.
MIN_SAMPLES = 14
# Feature rows need a trailing window to be meaningful; the first week is dropped.
WARMUP_DAYS = 7


@dataclass(frozen=True, slots=True)
class EnsembleVerdict:
    """Outcome of the confirmation stage."""

    available: bool
    """False when there was too little history to fit — callers fall back to
    the z-score alone rather than treating abstention as agreement."""
    is_outlier: bool
    score: float
    """IsolationForest decision function: negative is more anomalous."""
    reason: str

    @property
    def confirms(self) -> bool:
        return self.available and self.is_outlier

    @property
    def vetoes(self) -> bool:
        return self.available and not self.is_outlier


@dataclass(frozen=True, slots=True)
class CostPoint:
    usage_date: date
    amount: float


def _features(series: list[CostPoint], index: int) -> list[float]:
    """Feature row for ``series[index]`` using only earlier points.

    Strictly causal: a row never sees its own future, so the matrix the forest
    trains on has the same shape as the row built for a live candidate.
    """
    point = series[index]
    amount = point.amount
    past = series[max(0, index - WARMUP_DAYS) : index]

    trailing = [p.amount for p in past]
    trailing_median = float(np.median(trailing)) if trailing else amount
    ratio = amount / trailing_median if trailing_median > 0 else 1.0

    previous = series[index - 1].amount if index > 0 else amount
    delta = amount - previous

    same_weekday = [
        p.amount for p in series[:index] if p.usage_date.weekday() == point.usage_date.weekday()
    ]
    weekday_median = float(np.median(same_weekday)) if same_weekday else trailing_median
    weekday_deviation = amount / weekday_median if weekday_median > 0 else 1.0

    return [amount, float(np.log1p(max(amount, 0.0))), ratio, delta, weekday_deviation]


class IsolationForestConfirmer:
    """Fits one forest per service, on that service's own history."""

    def __init__(self, *, random_state: int = 42, contamination: float | str = "auto") -> None:
        self.random_state = random_state
        self.contamination = contamination
        self._model: IsolationForest | None = None
        self._scaler: RobustScaler | None = None
        self._series: list[CostPoint] = []
        self._sample_count = 0

    @property
    def fitted(self) -> bool:
        return self._model is not None

    def fit(self, history: list[CostPoint]) -> IsolationForestConfirmer:
        """Fit on ``history``, oldest first. Abstains if it is too short."""
        self._series = sorted(history, key=lambda p: p.usage_date)
        usable = len(self._series) - WARMUP_DAYS
        if usable < MIN_SAMPLES:
            logger.debug(
                "IsolationForest abstaining: %d usable rows, need %d", usable, MIN_SAMPLES
            )
            self._model = None
            self._scaler = None
            self._sample_count = max(usable, 0)
            return self

        matrix = np.asarray(
            [_features(self._series, i) for i in range(WARMUP_DAYS, len(self._series))],
            dtype=np.float64,
        )

        # RobustScaler, not StandardScaler: the training rows deliberately
        # include whatever spikes the history holds, and centring on a mean
        # would let them drag the scale.
        self._scaler = RobustScaler().fit(matrix)
        self._model = IsolationForest(
            n_estimators=150,
            contamination=self.contamination,
            random_state=self.random_state,
            # Cost history is a few dozen rows; bootstrapping a subsample of
            # 256 (the default) would just resample the same points.
            max_samples=min(256, matrix.shape[0]),
            n_jobs=1,
        ).fit(self._scaler.transform(matrix))
        self._sample_count = matrix.shape[0]
        return self

    def score(self, candidate: CostPoint) -> EnsembleVerdict:
        """Judge ``candidate`` as the next point after the fitted history."""
        if self._model is None or self._scaler is None:
            return EnsembleVerdict(
                available=False,
                is_outlier=False,
                score=0.0,
                reason=f"not fitted ({self._sample_count} usable rows, {MIN_SAMPLES} needed)",
            )

        extended = [*self._series, candidate]
        row = np.asarray([_features(extended, len(extended) - 1)], dtype=np.float64)
        scaled = self._scaler.transform(row)

        score = float(self._model.decision_function(scaled)[0])
        is_outlier = bool(self._model.predict(scaled)[0] == -1)
        return EnsembleVerdict(
            available=True,
            is_outlier=is_outlier,
            score=score,
            reason=(
                f"isolation forest {'flags' if is_outlier else 'accepts'} the point "
                f"(score {score:+.3f}, fitted on {self._sample_count} rows)"
            ),
        )

    def fit_score(self, history: list[CostPoint], candidate: CostPoint) -> EnsembleVerdict:
        return self.fit(history).score(candidate)
