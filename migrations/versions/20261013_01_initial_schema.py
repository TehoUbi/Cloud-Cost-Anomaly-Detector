"""initial schema: cost records, service baselines, alerts, alert attempts

Revision ID: 8f2a1c4d9b01
Revises:
Create Date: 2026-10-13

Written against PostgreSQL, the deployed target. Types are kept generic so the
same revision applies to the SQLite database the test suite uses.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8f2a1c4d9b01"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Daily spend is an accounting figure that gets summed and compared, so it is
# NUMERIC rather than a float. 14,6 covers a seven-figure daily line with the
# sub-cent precision Cost Explorer actually returns.
MONEY = sa.Numeric(14, 6)


def upgrade() -> None:
    op.create_table(
        "cost_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("service", sa.String(128), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("unit", sa.String(8), nullable=False, server_default="USD"),
        sa.Column("zscore", sa.Float(), nullable=True),
        sa.Column("baseline_mean", sa.Float(), nullable=True),
        sa.Column("baseline_stddev", sa.Float(), nullable=True),
        sa.Column("is_anomaly", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("detector", sa.String(32), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # The poll re-reads the trailing window every run and Cost Explorer
        # restates recent days, so the writer upserts on this constraint.
        sa.UniqueConstraint("service", "usage_date", name="uq_cost_records_service_date"),
    )
    op.create_index("ix_cost_records_usage_date", "cost_records", ["usage_date"])
    # Covers the service-prefix lookups and the ordering every read uses.
    op.create_index("ix_cost_records_service_date", "cost_records", ["service", "usage_date"])
    op.create_index("ix_cost_records_anomaly", "cost_records", ["is_anomaly", "usage_date"])

    op.create_table(
        "service_baselines",
        sa.Column("service", sa.String(128), primary_key=True),
        sa.Column("window_size", sa.Integer(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("mean", sa.Float(), nullable=False),
        sa.Column("stddev", sa.Float(), nullable=False),
        sa.Column("median", sa.Float(), nullable=False),
        sa.Column("mad", sa.Float(), nullable=False),
        sa.Column("p95", sa.Float(), nullable=False),
        sa.Column("last_amount", sa.Float(), nullable=False),
        sa.Column("last_evaluated_date", sa.Date(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "alerts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cost_record_id", sa.String(36), nullable=False),
        sa.Column("service", sa.String(128), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("zscore", sa.Float(), nullable=False),
        sa.Column("baseline_mean", sa.Float(), nullable=False),
        sa.Column("baseline_stddev", sa.Float(), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False, server_default="warning"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["cost_record_id"],
            ["cost_records.id"],
            name="fk_alerts_cost_record_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_alerts_cost_record_id", "alerts", ["cost_record_id"])
    op.create_index("ix_alerts_status_created", "alerts", ["status", "created_at"])
    op.create_index("ix_alerts_service_created", "alerts", ["service", "created_at"])
    # Drives the retry sweeper's "what is due?" query.
    op.create_index("ix_alerts_retry", "alerts", ["status", "next_retry_at"])

    op.create_table(
        "alert_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("alert_id", sa.String(36), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["alert_id"], ["alerts.id"], name="fk_alert_attempts_alert_id", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_alert_attempts_alert", "alert_attempts", ["alert_id", "created_at"])


def downgrade() -> None:
    op.drop_table("alert_attempts")
    op.drop_table("alerts")
    op.drop_table("service_baselines")
    op.drop_table("cost_records")
