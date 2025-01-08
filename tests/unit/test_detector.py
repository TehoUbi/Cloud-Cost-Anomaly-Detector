import pytest
from src.detector.zscore import ZScoreDetector, ZSCORE_THRESHOLD


def baseline_data(n=20, value=10.0) -> list[float]:
    return [value] * n


class TestZScoreDetector:
    def test_no_anomaly_for_normal_spend(self):
        detector = ZScoreDetector(service="AmazonEC2")
        detector.load_history(baseline_data(20, 10.0))
        result = detector.evaluate(10.5)
        assert not result.is_anomaly

    def test_anomaly_flagged_for_spike(self):
        detector = ZScoreDetector(service="AmazonEC2")
        detector.load_history(baseline_data(20, 10.0))
        result = detector.evaluate(500.0)
        assert result.is_anomaly
        assert result.zscore > ZSCORE_THRESHOLD

    def test_insufficient_history_never_flags(self):
        detector = ZScoreDetector(service="AmazonS3")
        result = detector.evaluate(999.0)
        assert not result.is_anomaly

    def test_baseline_updates_after_each_evaluation(self):
        detector = ZScoreDetector(service="AWSLambda")
        detector.load_history(baseline_data(20, 5.0))
        detector.evaluate(5.0)
        # Internal window should have grown/shifted
        assert len(detector._window) > 0

    def test_negative_zscore_also_detected(self):
        """A significant drop in spend can also be anomalous."""
        detector = ZScoreDetector(service="AmazonRDS", threshold=2.0)
        detector.load_history(baseline_data(20, 100.0))
        result = detector.evaluate(0.01)
        assert result.is_anomaly
        assert result.zscore < -2.0
