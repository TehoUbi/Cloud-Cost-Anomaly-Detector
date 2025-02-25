"""
Centralized application settings.

Everything configurable lives here so that the API, the Lambda handler and the
test suite all read the same values from the same place. Loaded from the
environment (or a local ``.env``) once at import time and cached.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CostProvider(StrEnum):
    """Where daily cost data comes from."""

    AWS = "aws"
    """Live AWS Cost Explorer (requires ``ce:GetCostAndUsage``)."""

    SYNTHETIC = "synthetic"
    """Deterministic generated data — for local dev and demos without AWS."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Database ---
    database_url: str = "postgresql+asyncpg://sentinel:sentinel@localhost:5432/sentinel"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    # --- AWS ---
    cost_provider: CostProvider = CostProvider.AWS
    aws_default_region: str = "us-east-1"
    cost_explorer_metric: Literal["UnblendedCost", "BlendedCost", "AmortizedCost"] = (
        "UnblendedCost"
    )
    cost_explorer_max_retries: int = 4

    # --- Detection ---
    zscore_threshold: float = Field(default=3.0, gt=0)
    window_size: int = Field(default=30, ge=3)
    min_history_days: int = Field(default=14, ge=2)
    """Days of history before a service is scored at all.

    MAD tolerates up to 50% contamination, so a short window holding two or
    three spikes has no trustworthy centre. Fourteen days is the point where
    a couple of bad days stop dominating.
    """
    min_absolute_delta_usd: float = Field(default=1.0, ge=0)
    """Spikes smaller than this in dollar terms are ignored regardless of z-score.

    Without it a service that costs $0.01/day and jumps to $0.08 produces a
    z-score of 20 and a meaningless page at 3am.
    """
    robust_zscore: bool = True
    """Use median/MAD instead of mean/stddev so one past spike cannot inflate
    the baseline and mask the next one."""
    ensemble_enabled: bool = True
    """Cross-check z-score hits against a scikit-learn novelty detector."""
    alert_on_drops: bool = False
    """Whether a sudden *fall* in spend should page someone.

    Off by default. A drop is recorded on the cost row either way — it is
    real signal, often a broken ingest pipeline — but the product promise
    here is catching spikes, and the day spend falls back to normal after a
    spike is itself a drop. Alerting on both doubles every incident.
    """

    # --- Alerting ---
    alert_webhook_url: str = ""
    alert_webhook_timeout_seconds: float = 10.0
    alert_email_from: str = ""
    alert_email_to: list[str] = Field(default_factory=list)
    alert_email_transport: Literal["ses", "smtp", "disabled"] = "disabled"
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True

    alert_max_retries: int = Field(default=5, ge=1)
    alert_base_delay_seconds: float = Field(default=1.0, gt=0)
    alert_max_delay_seconds: float = Field(default=60.0, gt=0)
    alert_backoff_jitter: float = Field(default=0.25, ge=0, le=1)
    alert_cooldown_hours: float = Field(default=24.0, ge=0)
    """Suppress repeat alerts for the same service inside this window."""

    # --- Polling ---
    poll_interval_hours: float = Field(default=6.0, gt=0)
    poll_lookback_days: int = Field(default=45, ge=10)

    # --- Service ---
    log_level: str = "INFO"
    api_title: str = "Sentinel — Cloud Cost Anomaly Detection"
    api_version: str = "1.0.0"

    @field_validator("alert_email_to", mode="before")
    @classmethod
    def _split_emails(cls, value: object) -> object:
        """Accept ``ALERT_EMAIL_TO=a@x.com,b@y.com`` as well as a JSON list."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return value
            return [part.strip() for part in stripped.split(",") if part.strip()]
        return value

    @property
    def email_enabled(self) -> bool:
        return (
            self.alert_email_transport != "disabled"
            and bool(self.alert_email_from)
            and bool(self.alert_email_to)
        )

    @property
    def webhook_enabled(self) -> bool:
        return bool(self.alert_webhook_url)


@lru_cache
def get_settings() -> Settings:
    """Cached accessor — call this rather than instantiating ``Settings``."""
    return Settings()
