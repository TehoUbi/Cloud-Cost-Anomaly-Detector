"""Daily per-service cost retrieval.

Two providers behind one protocol: live AWS Cost Explorer, and a deterministic
synthetic generator so the pipeline, the API and the load test can all run
without an AWS account or a Cost Explorer bill ($0.01 per request adds up when
a test loop polls it).
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Protocol

from src.config import CostProvider, Settings, get_settings
from src.utils.backoff import BackoffPolicy, retry_sync

logger = logging.getLogger(__name__)

# Botocore raises these as ClientError with one of these codes. Throttling is
# the common one: Cost Explorer allows a low steady-state request rate.
RETRYABLE_ERROR_CODES = frozenset(
    {
        "ThrottlingException",
        "RequestLimitExceeded",
        "TooManyRequestsException",
        "ServiceUnavailable",
        "InternalFailure",
        "LimitExceededException",
    }
)


@dataclass(frozen=True, slots=True)
class ServiceCost:
    """One service's spend for one usage day."""

    service: str
    usage_date: date
    amount: Decimal
    unit: str = "USD"

    @property
    def amount_usd(self) -> float:
        return float(self.amount)


class CostDataProvider(Protocol):
    def fetch_daily_costs(
        self, *, days_back: int, end_date: date | None = None
    ) -> list[ServiceCost]: ...


class CostExplorerError(RuntimeError):
    """Cost Explorer could not be read after exhausting retries."""


class AwsCostExplorerProvider:
    """Reads ``GetCostAndUsage`` grouped by SERVICE at DAILY granularity."""

    def __init__(self, settings: Settings | None = None, client: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._policy = BackoffPolicy(
            base_delay=1.0,
            max_delay=20.0,
            max_attempts=self.settings.cost_explorer_max_retries,
            jitter=0.3,
        )

    @property
    def client(self) -> Any:
        """Built on first use — constructing a boto3 client resolves
        credentials, which must not happen at import time."""
        if self._client is None:
            import boto3

            self._client = boto3.client("ce", region_name=self.settings.aws_default_region)
        return self._client

    def fetch_daily_costs(
        self, *, days_back: int, end_date: date | None = None
    ) -> list[ServiceCost]:
        """Fetch ``days_back`` complete days of per-service spend.

        Cost Explorer's ``TimePeriod.End`` is *exclusive*, so passing today as
        the end yields data through yesterday — which is what we want. Today's
        figure is partial and would read as a dip, not a spike.
        """
        end = end_date or date.today()
        start = end - timedelta(days=days_back)
        metric = self.settings.cost_explorer_metric

        results: list[ServiceCost] = []
        next_token: str | None = None
        pages = 0

        while True:
            request: dict[str, Any] = {
                "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
                "Granularity": "DAILY",
                "Metrics": [metric],
                "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
                # Credits and refunds post as negative amounts on arbitrary
                # days. Including them makes a refund look like a spend cliff.
                "Filter": {
                    "Dimensions": {
                        "Key": "RECORD_TYPE",
                        "Values": ["Usage"],
                        "MatchOptions": ["EQUALS"],
                    }
                },
            }
            if next_token:
                request["NextPageToken"] = next_token

            response = self._call_with_retry(request)
            results.extend(self._parse_page(response, metric))
            pages += 1

            next_token = response.get("NextPageToken")
            if not next_token:
                break
            if pages >= 50:
                logger.warning("Stopping Cost Explorer pagination at %d pages", pages)
                break

        logger.info(
            "Fetched %d service-day records across %d page(s) for %s..%s",
            len(results),
            pages,
            start,
            end,
        )
        return results

    def _call_with_retry(self, request: dict[str, Any]) -> dict[str, Any]:
        def attempt() -> dict[str, Any]:
            return self.client.get_cost_and_usage(**request)

        def log_retry(attempt_no: int, exc: BaseException, delay: float) -> None:
            logger.warning(
                "Cost Explorer call failed (attempt %d/%d): %s — retrying in %.1fs",
                attempt_no,
                self._policy.max_attempts,
                exc,
                delay,
            )

        try:
            return retry_sync(
                attempt,
                policy=self._policy,
                retry_if=self._is_retryable,
                on_failure=log_retry,
            )
        except Exception as exc:
            if self._is_retryable(exc):
                raise CostExplorerError(
                    f"Cost Explorer unavailable after {self._policy.max_attempts} attempts: {exc}"
                ) from exc
            # AccessDenied, a bad date range, an invalid filter: retrying these
            # just delays the failure by the whole backoff ladder.
            raise CostExplorerError(f"Cost Explorer request rejected: {exc}") from exc

    @staticmethod
    def _is_retryable(exc: BaseException) -> bool:
        """True for throttling and transient server-side faults only.

        botocore surfaces these as ClientError with the code on
        ``exc.response["Error"]["Code"]``; a connection-level failure has no
        response at all and is also worth another try.
        """
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            from botocore.exceptions import ConnectionError as BotoConnectionError
            from botocore.exceptions import HTTPClientError

            return isinstance(exc, BotoConnectionError | HTTPClientError)
        code = response.get("Error", {}).get("Code")
        return code in RETRYABLE_ERROR_CODES

    @staticmethod
    def _parse_page(response: dict[str, Any], metric: str) -> list[ServiceCost]:
        parsed: list[ServiceCost] = []
        for period in response.get("ResultsByTime", []):
            usage_date = date.fromisoformat(period["TimePeriod"]["Start"])
            for group in period.get("Groups", []):
                keys = group.get("Keys") or []
                if not keys:
                    continue
                metrics = group.get("Metrics", {}).get(metric, {})
                raw_amount = metrics.get("Amount")
                if raw_amount is None:
                    continue
                amount = Decimal(str(raw_amount))
                # Services with no spend on a day come back as exact zeros for
                # every service in the account; they are noise in the baseline.
                if amount == 0:
                    continue
                parsed.append(
                    ServiceCost(
                        service=keys[0],
                        usage_date=usage_date,
                        amount=amount,
                        unit=metrics.get("Unit", "USD"),
                    )
                )
        return parsed


# Shapes chosen to look like a small production account: a dominant compute
# line, steady storage, bursty data transfer, and a couple of cheap services
# that exist mainly to prove the absolute-delta floor earns its keep.
DRIFT_EPOCH = date(2025, 1, 1)
"""Fixed anchor for the synthetic growth trend, so the series is continuous."""

SYNTHETIC_SERVICES: tuple[tuple[str, float, float, float], ...] = (
    # (service, daily mean USD, noise stddev, weekday seasonality amplitude)
    ("Amazon Elastic Compute Cloud - Compute", 420.0, 18.0, 0.12),
    ("Amazon Relational Database Service", 180.0, 6.0, 0.04),
    ("Amazon Simple Storage Service", 96.0, 3.5, 0.02),
    ("AWS Lambda", 34.0, 7.0, 0.22),
    ("Amazon CloudFront", 28.0, 9.0, 0.3),
    ("AWS Key Management Service", 1.4, 0.15, 0.0),
    ("Amazon CloudWatch", 12.0, 2.0, 0.08),
    ("Amazon DynamoDB", 44.0, 5.0, 0.1),
)


class SyntheticCostProvider:
    """Deterministic fake spend with weekly seasonality and planted spikes.

    Seeded per (service, date), so the same day always returns the same figure
    no matter how often it is polled — the upsert path and the detector are
    both testable against it.
    """

    def __init__(
        self,
        *,
        seed: int = 1337,
        spike_probability: float = 0.008,
        spike_multiplier: float = 4.5,
    ) -> None:
        self.seed = seed
        self.spike_probability = spike_probability
        self.spike_multiplier = spike_multiplier

    def fetch_daily_costs(
        self, *, days_back: int, end_date: date | None = None
    ) -> list[ServiceCost]:
        end = end_date or date.today()
        start = end - timedelta(days=days_back)

        results: list[ServiceCost] = []
        for offset in range((end - start).days):
            usage_date = start + timedelta(days=offset)
            for service, mean, noise, seasonality in SYNTHETIC_SERVICES:
                results.append(self._point(usage_date, service, mean, noise, seasonality))
        return results

    def _point(
        self, usage_date: date, service: str, mean: float, noise: float, seasonality: float
    ) -> ServiceCost:
        rng = random.Random(f"{self.seed}:{service}:{usage_date.isoformat()}")

        # Weekly cycle: weekday traffic peaks, weekends dip.
        phase = (usage_date.weekday() / 7.0) * 2 * math.pi
        seasonal = 1.0 + seasonality * math.sin(phase)
        # Slow upward drift, ~1.2% a month, like a growing account. Anchored to
        # a fixed epoch rather than a modulo of the ordinal: wrapping the day
        # count once a year put an 18% cliff in the series, which the detector
        # quite rightly reported as a sustained step change in spend.
        drift = 1.0 + (usage_date - DRIFT_EPOCH).days * 0.0004

        amount = mean * seasonal * drift + rng.gauss(0, noise)

        if rng.random() < self.spike_probability:
            amount *= self.spike_multiplier
            logger.debug("Synthetic spike planted: %s on %s", service, usage_date)

        return ServiceCost(
            service=service,
            usage_date=usage_date,
            amount=Decimal(f"{max(amount, 0.01):.6f}"),
        )


def get_cost_provider(settings: Settings | None = None) -> CostDataProvider:
    """Pick a provider from ``COST_PROVIDER``."""
    settings = settings or get_settings()
    if settings.cost_provider is CostProvider.SYNTHETIC:
        logger.info("Using synthetic cost provider (COST_PROVIDER=synthetic)")
        return SyntheticCostProvider()
    return AwsCostExplorerProvider(settings)


def fetch_daily_costs(days_back: int = 30, end_date: date | None = None) -> list[ServiceCost]:
    """Module-level convenience used by the scheduled entrypoint."""
    return get_cost_provider().fetch_daily_costs(days_back=days_back, end_date=end_date)
