"""Request and response models. These are the API's contract."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

ItemT = TypeVar("ItemT")


class Page(BaseModel, Generic[ItemT]):
    """Offset-paginated envelope. ``total`` is the count before paging."""

    items: list[ItemT]
    total: int = Field(description="Total rows matching the filter, ignoring limit/offset")
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class CostRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    service: str
    usage_date: date
    amount: float
    unit: str
    zscore: float | None
    baseline_mean: float | None
    baseline_stddev: float | None
    is_anomaly: bool
    detector: str | None
    created_at: datetime

    @classmethod
    def from_record(cls, record: Any) -> CostRecordOut:
        return cls(
            id=record.id,
            service=record.service,
            usage_date=record.usage_date,
            amount=record.amount_usd,
            unit=record.unit,
            zscore=record.zscore,
            baseline_mean=record.baseline_mean,
            baseline_stddev=record.baseline_stddev,
            is_anomaly=record.is_anomaly,
            detector=record.detector,
            created_at=record.created_at,
        )


class BaselineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    service: str
    window_size: int
    sample_count: int
    mean: float
    stddev: float
    median: float
    mad: float = Field(description="Median absolute deviation, scaled to stddev units")
    p95: float
    last_amount: float
    last_evaluated_date: date
    updated_at: datetime


class AlertAttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    channel: str
    attempt_number: int
    succeeded: bool
    status_code: int | None
    error: str | None
    duration_ms: float | None
    created_at: datetime


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    cost_record_id: str
    service: str
    usage_date: date
    amount: float
    zscore: float
    baseline_mean: float
    baseline_stddev: float
    severity: str
    status: str
    message: str
    attempts: int
    delivered_at: datetime | None
    last_attempted_at: datetime | None
    next_retry_at: datetime | None
    last_error: str | None
    created_at: datetime
    delivery_attempts: list[AlertAttemptOut] = Field(default_factory=list)

    @classmethod
    def from_alert(cls, alert: Any, *, include_attempts: bool = False) -> AlertOut:
        return cls(
            id=alert.id,
            cost_record_id=alert.cost_record_id,
            service=alert.service,
            usage_date=alert.usage_date,
            amount=alert.amount_usd,
            zscore=alert.zscore,
            baseline_mean=alert.baseline_mean,
            baseline_stddev=alert.baseline_stddev,
            severity=alert.severity,
            status=alert.status,
            message=alert.message,
            attempts=alert.attempts,
            delivered_at=alert.delivered_at,
            last_attempted_at=alert.last_attempted_at,
            next_retry_at=alert.next_retry_at,
            last_error=alert.last_error,
            created_at=alert.created_at,
            delivery_attempts=(
                [AlertAttemptOut.model_validate(a) for a in alert.delivery_attempts]
                if include_attempts
                else []
            ),
        )


class DetectionSummaryOut(BaseModel):
    services_seen: int
    records_upserted: int
    days_evaluated: int
    anomalies_found: int
    ensemble_confirmations: int
    ensemble_vetoes: int
    ensemble_overrides: int
    alerts_created: int
    alerts_suppressed: int
    drops_recorded: int
    duration_seconds: float


class PollResponse(BaseModel):
    observations_fetched: int
    delivered: int
    failed: int
    swept: int
    swept_delivered: int
    duration_seconds: float
    errors: list[str]
    detection: DetectionSummaryOut


class PollRequest(BaseModel):
    lookback_days: int | None = Field(
        default=None, ge=2, le=365, description="Override POLL_LOOKBACK_DAYS for this run"
    )
    deliver: bool = Field(
        default=True, description="Set false to score and store without sending alerts"
    )


class ServiceSpend(BaseModel):
    service: str
    total: float


class DailyTotal(BaseModel):
    usage_date: date
    total: float


class SummaryResponse(BaseModel):
    window_days: int
    total_spend: float
    services_tracked: int
    anomalies: int
    alerts_by_status: dict[str, int]
    top_services: list[ServiceSpend]
    daily_totals: list[DailyTotal]


class HealthResponse(BaseModel):
    status: str
    version: str
    timestamp: datetime


class ReadinessResponse(BaseModel):
    status: str
    database: str
    cost_provider: str
    alert_channels: list[str]
    timestamp: datetime


class TestAlertRequest(BaseModel):
    """Fires a synthetic alert through the real channels to prove config."""

    service: str = "sentinel.test"
    amount: float = Field(default=1234.56, ge=0)
    baseline_mean: float = Field(default=100.0, ge=0)
    baseline_stddev: float = Field(default=10.0, gt=0)


class TestAlertResponse(BaseModel):
    delivered: bool
    channels: list[str]
    subject: str


class RetryResponse(BaseModel):
    alert_id: str
    status: str
    attempts: int
    channels: list[dict[str, Any]]
