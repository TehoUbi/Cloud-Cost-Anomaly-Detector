"""
Rolling z-score anomaly detector for per-service AWS spend.

For each service, maintains a sliding window of daily costs and flags
any new data point whose z-score exceeds ZSCORE_THRESHOLD.
"""

from dataclasses import dataclass
from collections import deque
import statistics
import os

ZSCORE_THRESHOLD = float(os.getenv("ZSCORE_THRESHOLD", "3.0"))
WINDOW_SIZE = int(os.getenv("WINDOW_SIZE", "30"))  # days


@dataclass
class AnomalyResult:
    service: str
    amount: float
    zscore: float
    baseline_mean: float
    baseline_stddev: float
    is_anomaly: bool


class ZScoreDetector:
    """
    Stateful rolling z-score detector for a single AWS service.

    Usage:
        detector = ZScoreDetector(service="AmazonEC2")
        detector.load_history([12.5, 13.1, 11.8, ...])  # seed with past data
        result = detector.evaluate(45.0)                 # check new data point
    """

    def __init__(self, service: str, window: int = WINDOW_SIZE, threshold: float = ZSCORE_THRESHOLD):
        self.service = service
        self.threshold = threshold
        self._window: deque[float] = deque(maxlen=window)

    def load_history(self, historical_amounts: list[float]) -> None:
        """Seed the detector with historical daily cost data."""
        for amount in historical_amounts[-self._window.maxlen:]:
            self._window.append(amount)

    def evaluate(self, amount: float) -> AnomalyResult:
        """Evaluate a new daily cost amount and return an AnomalyResult."""
        if len(self._window) < 2:
            # Not enough data to compute a meaningful z-score
            self._window.append(amount)
            return AnomalyResult(
                service=self.service,
                amount=amount,
                zscore=0.0,
                baseline_mean=amount,
                baseline_stddev=0.0,
                is_anomaly=False,
            )

        mean = statistics.mean(self._window)
        stddev = statistics.stdev(self._window)

        zscore = (amount - mean) / stddev if stddev > 0 else 0.0
        is_anomaly = abs(zscore) >= self.threshold

        self._window.append(amount)

        return AnomalyResult(
            service=self.service,
            amount=amount,
            zscore=zscore,
            baseline_mean=mean,
            baseline_stddev=stddev,
            is_anomaly=is_anomaly,
        )
