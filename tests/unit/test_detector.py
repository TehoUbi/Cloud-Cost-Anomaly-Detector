"""The z-score detector, including the cases that made it change shape."""

from __future__ import annotations

import pytest

from src.detector.zscore import (
    MAD_TO_STDDEV,
    BaselineStats,
    Direction,
    RollingZScoreDetector,
    Severity,
)


def flat(n: int = 30, value: float = 10.0) -> list[float]:
    return [value] * n


def noisy(n: int = 30, mean: float = 420.0, stddev: float = 12.0) -> list[float]:
    import numpy as np

    rng = np.random.default_rng(7)
    return [float(v) for v in rng.normal(mean, stddev, n)]


def detector(**kwargs) -> RollingZScoreDetector:
    """A detector with explicit parameters, independent of the environment."""
    defaults = {
        "service": "AmazonEC2",
        "window": 30,
        "threshold": 3.0,
        "min_history": 14,
        "min_absolute_delta": 1.0,
        "robust": True,
    }
    return RollingZScoreDetector(**{**defaults, **kwargs})


class TestBaselineStats:
    def test_empty_window_is_all_zeros(self):
        stats = BaselineStats.from_window([])
        assert stats.sample_count == 0
        assert stats.is_degenerate

    def test_mad_is_scaled_to_stddev_units(self):
        """MAD * 1.4826 estimates sigma for normal data, so the two should be
        close on a clean normal sample and comparable against one threshold."""
        stats = BaselineStats.from_window(noisy(400, 100.0, 10.0))
        assert stats.mad == pytest.approx(stats.stddev, rel=0.15)

    def test_stddev_uses_the_sample_denominator(self):
        import numpy as np

        values = [1.0, 2.0, 3.0, 4.0]
        stats = BaselineStats.from_window(values)
        assert stats.stddev == pytest.approx(float(np.std(values, ddof=1)))

    def test_single_sample_has_no_spread(self):
        stats = BaselineStats.from_window([42.0])
        assert stats.stddev == 0.0
        assert stats.median == 42.0

    def test_flat_window_is_degenerate(self):
        assert BaselineStats.from_window(flat()).is_degenerate

    def test_mad_constant_is_the_documented_value(self):
        assert pytest.approx(1.4826, abs=1e-4) == MAD_TO_STDDEV


class TestInsufficientHistory:
    def test_no_history_never_flags(self):
        result = detector().evaluate(99999.0)
        assert not result.is_anomaly
        assert "insufficient history" in result.reason

    def test_below_min_history_never_flags(self):
        det = detector(min_history=14).load_history(flat(13, 10.0))
        assert not det.evaluate(5000.0).is_anomaly

    def test_flags_once_min_history_is_reached(self):
        det = detector(min_history=14).load_history(noisy(14))
        assert det.evaluate(5000.0).is_anomaly

    def test_points_still_accumulate_while_warming_up(self):
        det = detector()
        for value in flat(5, 10.0):
            det.evaluate(value)
        assert len(det.history) == 5


class TestSpikeAndDropDetection:
    def test_normal_spend_is_not_flagged(self):
        det = detector().load_history(noisy())
        assert not det.evaluate(425.0).is_anomaly

    def test_spike_is_flagged_with_direction(self):
        det = detector().load_history(noisy())
        result = det.evaluate(2000.0)
        assert result.is_anomaly
        assert result.direction is Direction.SPIKE
        assert result.zscore > 3.0

    def test_drop_is_flagged_with_direction(self):
        det = detector().load_history(noisy())
        result = det.evaluate(20.0)
        assert result.is_anomaly
        assert result.direction is Direction.DROP
        assert result.zscore < -3.0

    def test_threshold_is_respected(self):
        """A 5-sigma point clears a threshold of 3 and not one of 10.

        The amount is derived from the window's own robust scale rather than
        hardcoded: MAD is not the generator's sigma, so a fixed dollar figure
        would be testing the sample, not the threshold.
        """
        history = noisy()
        scale = BaselineStats.from_window(history)
        five_sigma = scale.median + 5 * scale.mad

        assert detector(threshold=3.0).load_history(history).evaluate(five_sigma).is_anomaly
        lenient = detector(threshold=10.0).load_history(history).evaluate(five_sigma)
        assert not lenient.is_anomaly
        assert lenient.zscore == pytest.approx(5.0, abs=0.01)

    def test_severity_escalates_with_magnitude(self):
        """Warning just past the threshold, critical at twice it."""
        history = noisy()
        scale = BaselineStats.from_window(history)

        moderate = (
            detector(threshold=3.0).load_history(history).evaluate(scale.median + 3.5 * scale.mad)
        )
        extreme = (
            detector(threshold=3.0).load_history(history).evaluate(scale.median + 7.0 * scale.mad)
        )
        assert moderate.severity is Severity.WARNING
        assert extreme.severity is Severity.CRITICAL

    def test_a_huge_dollar_move_is_critical_regardless_of_sigma(self):
        """Tripling a $420/day service matters even at a modest z-score."""
        det = detector(threshold=3.0).load_history(noisy())
        assert det.evaluate(1500.0).severity is Severity.CRITICAL

    def test_delta_and_percentage_are_reported(self):
        det = detector().load_history(flat(30, 100.0))
        result = det.evaluate(250.0)
        assert result.delta == pytest.approx(150.0)
        assert result.delta_pct == pytest.approx(150.0)

    def test_reason_is_populated_either_way(self):
        det = detector().load_history(noisy())
        assert detector().load_history(noisy()).evaluate(2000.0).reason
        assert det.evaluate(425.0).reason


class TestRobustness:
    """The reason the detector does not use mean and stddev."""

    def test_classic_zscore_is_blinded_by_a_past_spike(self):
        poisoned = [*noisy(29), 50_000.0]
        classic = detector(robust=False).load_history(poisoned)
        result = classic.evaluate(2000.0)
        assert not result.is_anomaly, "mean/stddev should be shown to fail here"

    def test_robust_zscore_still_sees_it(self):
        poisoned = [*noisy(29), 50_000.0]
        result = detector(robust=True).load_history(poisoned).evaluate(2000.0)
        assert result.is_anomaly
        assert result.zscore > 3.0

    def test_robust_survives_several_outliers(self):
        """MAD's breakdown point is 50%; a handful of bad days is well inside it."""
        poisoned = [*noisy(26), 50_000.0, 48_000.0, 61_000.0, 55_000.0]
        result = detector(robust=True).load_history(poisoned).evaluate(2000.0)
        assert result.is_anomaly

    def test_reported_baseline_matches_the_scale_that_scored_it(self):
        """A client must be able to recompute the z-score from what it is given."""
        history = noisy()
        robust = detector(robust=True).load_history(history).evaluate(2000.0)
        recomputed = (robust.amount - robust.baseline_mean) / robust.baseline_stddev
        assert recomputed == pytest.approx(robust.zscore)

        classic = detector(robust=False).load_history(history).evaluate(2000.0)
        assert classic.baseline_mean == pytest.approx(classic.baseline.mean)
        assert classic.baseline_stddev == pytest.approx(classic.baseline.stddev)


class TestAbsoluteFloor:
    def test_tiny_spend_spike_is_suppressed(self):
        """KMS at a cent a day going to eight cents is 144 sigma and $0.07."""
        det = detector(min_absolute_delta=1.0)
        det.load_history([0.01] * 20 + [0.012, 0.009, 0.011, 0.01])
        result = det.evaluate(0.08)
        assert abs(result.zscore) > 3.0
        assert not result.is_anomaly
        assert "suppressed_by_absolute_floor" in result.notes

    def test_floor_does_not_mask_real_money(self):
        det = detector(min_absolute_delta=1.0).load_history(noisy())
        assert det.evaluate(2000.0).is_anomaly

    def test_floor_of_zero_disables_the_guard(self):
        det = detector(min_absolute_delta=0.0)
        det.load_history([0.01] * 24)
        assert det.evaluate(0.08).is_anomaly


class TestDegenerateWindow:
    def test_identical_history_does_not_divide_by_zero(self):
        det = detector().load_history(flat(30, 10.0))
        result = det.evaluate(10.0)
        assert not result.is_anomaly
        assert result.zscore == 0.0

    def test_large_relative_change_on_a_flat_window_is_flagged(self):
        det = detector().load_history(flat(30, 10.0))
        result = det.evaluate(80.0)
        assert result.is_anomaly
        assert "degenerate_baseline" in result.notes

    def test_small_change_on_a_flat_window_is_not(self):
        det = detector().load_history(flat(30, 10.0))
        assert not det.evaluate(11.0).is_anomaly

    def test_flat_window_still_respects_the_dollar_floor(self):
        det = detector(min_absolute_delta=5.0).load_history(flat(30, 0.5))
        result = det.evaluate(2.0)
        assert not result.is_anomaly


class TestWindowManagement:
    def test_window_is_bounded(self):
        det = detector(window=10).load_history(flat(50, 1.0))
        assert len(det.history) == 10

    def test_evaluate_appends_the_point(self):
        det = detector().load_history(flat(20, 10.0))
        det.evaluate(11.0)
        assert det.history[-1] == 11.0

    def test_observe_false_leaves_the_window_alone(self):
        det = detector().load_history(flat(20, 10.0))
        det.evaluate(9999.0, observe=False)
        assert det.history[-1] == 10.0

    def test_point_is_not_scored_against_itself(self):
        """Appending before scoring would drag the baseline toward the spike."""
        det = detector().load_history(flat(29, 10.0) + [10.0])
        first = det.evaluate(1000.0)
        det2 = detector().load_history(flat(30, 10.0))
        assert first.baseline_mean == pytest.approx(det2.baseline().median)

    def test_window_slides_past_the_cap(self):
        det = detector(window=5).load_history(flat(5, 1.0))
        for value in (2.0, 3.0, 4.0):
            det.observe(value)
        assert det.history == [1.0, 1.0, 2.0, 3.0, 4.0]

    def test_load_history_keeps_only_the_newest(self):
        det = detector(window=3).load_history([1.0, 2.0, 3.0, 4.0, 5.0])
        assert det.history == [3.0, 4.0, 5.0]
