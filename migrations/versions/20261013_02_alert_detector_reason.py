"""alerts: record which detector fired and why

Revision ID: 3c7e5a91d204
Revises: 8f2a1c4d9b01
Create Date: 2026-10-13

A swept alert is delivered from its database row long after the in-memory
AnomalyResult is gone, and the row kept only the rendered headline. The
detector's reasoning was folded into that same string, so a rendered email
printed the sentence twice and the structured reason was lost.

Nullable with no backfill: alerts written before this revision genuinely do
not have the information, and inventing a value for them would be worse than
leaving it absent. build_payload_from_alert falls back to the message.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c7e5a91d204"
down_revision: str | None = "8f2a1c4d9b01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("alerts", sa.Column("detector", sa.String(32), nullable=True))
    op.add_column("alerts", sa.Column("reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("alerts", "reason")
    op.drop_column("alerts", "detector")
