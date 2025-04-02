"""The scikit-learn confirmation stage and the policy that reconciles it."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from src.detector.ensemble import (
    MIN_SAMPLES,
    WARMUP_DAYS,
    CostPoint,
    EnsembleVerdict,
    IsolationForestConfirmer,
    _features,
)
from src.detector.service import ENSEMBLE_OVERRIDE_FACTOR, apply_ensemble
from src.detector.zscore import AnomalyResult, Direction, Severity

START = date(2026, 8, 1)


def series(amounts: list[float], start: date = START) -> list[CostPoint]:
    return [CostPoint(start + timedelta(days=i), amount) for i, amount in enumerate(amounts)]


def steady(n: int = 40, mean: float = 420.0, stddev: float = 12.0) -> list[CostPoint]:
    rng = np.random.default_rng(11)
    return series([float(v) for v in rng.normal(mean, stddev, n)])


def bursty(n: int = 60) -> list[CostPoint]:
    """A Lambda-shaped line: routine multi-fold bursts twice a week."""
    rng = np.random.default_rng(5)
    amounts = []
    for i in range(n):
        value = float(rng.normal(34.0, 6.0))
        if i % 7 in (0, 3):
            value *= float(rng.uniform(2.5, 4.0))
        amounts.append(max(value, 1.0))
    return series(amounts)


class TestFeatures:
    def test_row_is_strictly_causal(self):
        """A feature row must not see its own future, or the matrix the forest
        trains on has a different shape from a live candidate's row."""
        points = series([10.0, 20.0, 30.0, 40.0, 50.0])
        row = _features(points, 2)
        extended = [*points, CostPoint(START + timedelta(days=5), 999.0)]
        assert _features(extended, 2) == row

    def test_day_over_day_delta(self):
        points = series([10.0, 25.0])
        assert _features(points, 1)[3] == pytest.approx(15.0)

    def test_first_point_has_no_delta(self):
        assert _features(series([10.0]), 0)[3] == 0.0

    def test_ratio_to_trailing_median(self):
        points = series([10.0] * 7 + [30.0])
        assert _features(points, 7)[2] == pytest.approx(3.0)

    def test_weekday_deviation_uses_matching_weekdays(self):
        # 14 days: index 7 is the same weekday as index 0.
        amounts = [100.0 if i % 7 == 0 else 10.0 for i in range(14)]
        row = _features(series(amounts), 7)
        assert row[4] == pytest.approx(1.0)

    def test_zero_amounts_do_not_blow_up(self):
        row = _features(series([0.0] * 8), 7)
        assert all(np.isfinite(value) for value in row)

    def test_row_width_matches_the_declared_feature_names(self):
        from src.detector.ensemble import FEATURE_NAMES

        assert len(_features(steady(20), 10)) == len(FEATURE_NAMES)


class TestConfirmerFitting:
    def test_abstains_on_short_history(self):
        verdict = IsolationForestConfirmer().fit_score(
            steady(WARMUP_DAYS + MIN_SAMPLES - 1), CostPoint(START, 9999.0)
        )
        assert not verdict.available
        assert not verdict.confirms
        assert not verdict.vetoes

    def test_fits_once_there_is_enough(self):
        confirmer = IsolationForestConfirmer().fit(steady(WARMUP_DAYS + MIN_SAMPLES))
        assert confirmer.fitted

    def test_abstention_is_neither_confirmation_nor_veto(self):
        """Treating "no opinion" as agreement would silently disable the veto."""
        verdict = EnsembleVerdict(available=False, is_outlier=False, score=0.0, reason="")
        assert not verdict.confirms and not verdict.vetoes

    def test_unfitted_confirmer_scores_as_unavailable(self):
        verdict = IsolationForestConfirmer().score(CostPoint(START, 1.0))
        assert not verdict.available
        assert "not fitted" in verdict.reason

    def test_fitting_is_deterministic(self):
        history, candidate = steady(), CostPoint(START + timedelta(days=40), 1800.0)
        first = IsolationForestConfirmer().fit_score(history, candidate)
        second = IsolationForestConfirmer().fit_score(history, candidate)
        assert first.score == pytest.approx(second.score)
        assert first.is_outlier == second.is_outlier

    def test_history_order_does_not_matter(self):
        """fit() sorts by date, so a provider returning rows out of order is fine."""
        history = steady()
        candidate = CostPoint(START + timedelta(days=40), 1800.0)
        ordered = IsolationForestConfirmer().fit_score(history, candidate)
        shuffled = IsolationForestConfirmer().fit_score(list(reversed(history)), candidate)
        assert ordered.score == pytest.approx(shuffled.score)


class TestConfirmerJudgement:
    def test_accepts_a_normal_point_on_a_steady_series(self):
        verdict = IsolationForestConfirmer().fit_score(
            steady(), CostPoint(START + timedelta(days=40), 425.0)
        )
        assert verdict.available
        assert not verdict.is_outlier

    def test_flags_a_blowout_on_a_steady_series(self):
        verdict = IsolationForestConfirmer().fit_score(
            steady(), CostPoint(START + timedelta(days=40), 4000.0)
        )
        assert verdict.is_outlier
        assert verdict.score < 0

    def test_score_orders_by_how_unusual_a_point_is(self):
        confirmer = IsolationForestConfirmer().fit(steady())
        normal = confirmer.score(CostPoint(START + timedelta(days=40), 425.0))
        extreme = confirmer.score(CostPoint(START + timedelta(days=40), 9000.0))
        assert extreme.score < normal.score

    def test_tolerates_a_routine_burst_on_a_bursty_series(self):
        """The whole point of the stage: a series that is genuinely spiky
        should not have every burst treated as an incident."""
        history = bursty()
        confirmer = IsolationForestConfirmer().fit(history)
        burst_day = next(
            point
            for point in history[-14:]
            if point.usage_date.weekday() == history[0].usage_date.weekday()
        )
        typical_burst = float(np.median([p.amount for p in history if p.amount > 80]))
        verdict = confirmer.score(
            CostPoint(burst_day.usage_date + timedelta(days=7), typical_burst)
        )
        assert verdict.available


class TestApplyEnsemblePolicy:
    def make_result(self, *, zscore: float, is_anomaly: bool = True) -> AnomalyResult:
        return AnomalyResult(
            service="AmazonEC2",
            amount=2000.0,
            zscore=zscore,
            baseline_mean=420.0,
            baseline_stddev=12.0,
            is_anomaly=is_anomaly,
            direction=Direction.SPIKE,
            severity=Severity.WARNING,
            reason="spike",
        )

    def verdict(self, *, available: bool, is_outlier: bool) -> EnsembleVerdict:
        return EnsembleVerdict(
            available=available, is_outlier=is_outlier, score=-0.1, reason="forest says so"
        )

    def test_non_anomaly_is_left_alone(self):
        result = self.make_result(zscore=0.5, is_anomaly=False)
        amended, tag = apply_ensemble(
            result, self.verdict(available=True, is_outlier=True), threshold=3.0
        )
        assert tag == "skipped"
        assert amended is result

    def test_agreement_confirms(self):
        amended, tag = apply_ensemble(
            self.make_result(zscore=4.0),
            self.verdict(available=True, is_outlier=True),
            threshold=3.0,
        )
        assert tag == "confirmed"
        assert amended.is_anomaly
        assert "ensemble_confirmed" in amended.notes

    def test_borderline_disagreement_is_vetoed(self):
        amended, tag = apply_ensemble(
            self.make_result(zscore=3.2),
            self.verdict(available=True, is_outlier=False),
            threshold=3.0,
        )
        assert tag == "vetoed"
        assert not amended.is_anomaly
        assert amended.severity is Severity.INFO
        assert "ensemble_vetoed" in amended.notes

    def test_extreme_zscore_overrides_a_veto(self):
        """At many sigma the z-score is describing something real whatever the
        forest thinks, so the veto must not be able to swallow it."""
        amended, tag = apply_ensemble(
            self.make_result(zscore=3.0 * ENSEMBLE_OVERRIDE_FACTOR + 0.1),
            self.verdict(available=True, is_outlier=False),
            threshold=3.0,
        )
        assert tag == "overridden"
        assert amended.is_anomaly
        assert "ensemble_overridden" in amended.notes

    def test_veto_applies_just_below_the_override_boundary(self):
        amended, tag = apply_ensemble(
            self.make_result(zscore=3.0 * ENSEMBLE_OVERRIDE_FACTOR - 0.1),
            self.verdict(available=True, is_outlier=False),
            threshold=3.0,
        )
        assert tag == "vetoed"
        assert not amended.is_anomaly

    def test_a_negative_zscore_is_judged_on_magnitude(self):
        amended, tag = apply_ensemble(
            self.make_result(zscore=-7.0),
            self.verdict(available=True, is_outlier=False),
            threshold=3.0,
        )
        assert tag == "overridden"
        assert amended.is_anomaly

    def test_abstention_leaves_the_anomaly_standing(self):
        amended, tag = apply_ensemble(
            self.make_result(zscore=3.2),
            self.verdict(available=False, is_outlier=False),
            threshold=3.0,
        )
        assert tag == "unavailable"
        assert amended.is_anomaly
        assert "ensemble_unavailable" in amended.notes

    def test_amended_results_keep_the_original_numbers(self):
        original = self.make_result(zscore=4.0)
        amended, _ = apply_ensemble(
            original, self.verdict(available=True, is_outlier=True), threshold=3.0
        )
        assert amended.zscore == original.zscore
        assert amended.amount == original.amount
        assert amended.baseline_mean == original.baseline_mean
