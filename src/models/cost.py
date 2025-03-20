"""ORM models for cost history, per-service baselines and alert delivery."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base, new_id

# Money is stored as NUMERIC, not float: a day of spend is an accounting figure
# that gets summed and compared, and binary floats drift when you do that.
Money = Numeric(14, 6)


class AlertStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    PARTIAL = "partial"
    """At least one channel succeeded and at least one exhausted its retries."""
    FAILED = "failed"
    SUPPRESSED = "suppressed"
    """Deliberately not sent — inside the per-service cooldown window."""


class AlertChannel(StrEnum):
    WEBHOOK = "webhook"
    EMAIL = "email"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class DetectorKind(StrEnum):
    ROBUST_ZSCORE = "robust_zscore"
    ZSCORE = "zscore"
    ENSEMBLE = "ensemble"


class CostRecord(Base):
    """One service's spend for one day, with the verdict it received."""

    __tablename__ = "cost_records"
    __table_args__ = (
        # A poll re-reads the trailing window every run, and Cost Explorer
        # restates recent days. One row per service-day, updated in place.
        UniqueConstraint("service", "usage_date", name="uq_cost_records_service_date"),
        # Covers both the service-prefix lookups and the ordering every read
        # uses ("most recent N days for a service"). Not declared DESC:
        # PostgreSQL walks a b-tree backwards at the same cost, and an
        # expression index is something `alembic check` cannot compare.
        Index("ix_cost_records_service_date", "service", "usage_date"),
        Index("ix_cost_records_anomaly", "is_anomaly", "usage_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # No index=True on service: ix_cost_records_service_date covers it as a
    # prefix. usage_date keeps its own, for the cross-service daily rollups.
    service: Mapped[str] = mapped_column(String(128), nullable=False)
    usage_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    unit: Mapped[str] = mapped_column(
        String(8), default="USD", server_default="USD", nullable=False
    )

    zscore: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_stddev: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_anomaly: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    detector: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    alerts: Mapped[list[Alert]] = relationship(
        back_populates="cost_record", cascade="all, delete-orphan"
    )

    @property
    def amount_usd(self) -> float:
        return float(self.amount)

    def __repr__(self) -> str:
        return (
            f"<CostRecord {self.service} {self.usage_date} ${self.amount_usd:.2f} z={self.zscore}>"
        )


class ServiceBaseline(Base):
    """Cached rolling baseline per service.

    Derived from ``cost_records`` and rebuildable at any time, but persisting it
    means the API can answer "what is normal for this service?" without
    recomputing a 30-day window per request.
    """

    __tablename__ = "service_baselines"

    service: Mapped[str] = mapped_column(String(128), primary_key=True)
    window_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    mean: Mapped[float] = mapped_column(Float, nullable=False)
    stddev: Mapped[float] = mapped_column(Float, nullable=False)
    median: Mapped[float] = mapped_column(Float, nullable=False)
    mad: Mapped[float] = mapped_column(Float, nullable=False)
    """Median absolute deviation, scaled to be comparable to a stddev."""
    p95: Mapped[float] = mapped_column(Float, nullable=False)
    last_amount: Mapped[float] = mapped_column(Float, nullable=False)
    last_evaluated_date: Mapped[date] = mapped_column(Date, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<ServiceBaseline {self.service} mean={self.mean:.2f} n={self.sample_count}>"


class Alert(Base):
    """An anomaly that should reach a human, plus how that went."""

    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_status_created", "status", "created_at"),
        Index("ix_alerts_service_created", "service", "created_at"),
        Index("ix_alerts_retry", "status", "next_retry_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    cost_record_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cost_records.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Denormalized from the cost record so an alert renders without a join —
    # these are an immutable snapshot of what was true when it fired.
    service: Mapped[str] = mapped_column(String(128), nullable=False)
    usage_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    zscore: Mapped[float] = mapped_column(Float, nullable=False)
    baseline_mean: Mapped[float] = mapped_column(Float, nullable=False)
    baseline_stddev: Mapped[float] = mapped_column(Float, nullable=False)

    severity: Mapped[str] = mapped_column(
        String(16), default=Severity.WARNING, server_default="warning", nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default=AlertStatus.PENDING, server_default="pending", nullable=False
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)

    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    cost_record: Mapped[CostRecord] = relationship(back_populates="alerts")
    delivery_attempts: Mapped[list[AlertAttempt]] = relationship(
        back_populates="alert", cascade="all, delete-orphan", order_by="AlertAttempt.created_at"
    )

    @property
    def amount_usd(self) -> float:
        return float(self.amount)

    def __repr__(self) -> str:
        return f"<Alert {self.service} {self.usage_date} {self.status}>"


class AlertAttempt(Base):
    """One delivery attempt on one channel — the audit trail behind the retries."""

    __tablename__ = "alert_attempts"
    __table_args__ = (Index("ix_alert_attempts_alert", "alert_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    alert_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    succeeded: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    alert: Mapped[Alert] = relationship(back_populates="delivery_attempts")

    def __repr__(self) -> str:
        return f"<AlertAttempt {self.channel} #{self.attempt_number} ok={self.succeeded}>"
