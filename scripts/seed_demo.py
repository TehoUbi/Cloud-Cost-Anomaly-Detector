"""Seed a database with synthetic history for demos and load tests.

    python -m scripts.seed_demo --days 180

Uses the synthetic cost provider, so it needs no AWS account. Alerts are
recorded but never delivered: this is a data fixture, not a poll.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from src.api.main import configure_logging
from src.config import get_settings
from src.db.repositories import AlertRepository, CostRepository
from src.db.session import dispose_engine, init_db, session_scope
from src.pipeline import backfill
from src.utils.cost_explorer import SyntheticCostProvider

logger = logging.getLogger("sentinel.seed")


async def seed(days: int, chunk_days: int, spike_probability: float) -> None:
    settings = get_settings()
    await init_db()

    provider = SyntheticCostProvider(spike_probability=spike_probability)
    async with session_scope() as session:
        result = await backfill(
            session, days=days, settings=settings, provider=provider, chunk_days=chunk_days
        )

    async with session_scope() as session:
        costs = CostRepository(session)
        services = await costs.services()
        _, total = await costs.list_records(days=days + 1, limit=1)
        _, anomalies = await costs.list_records(days=days + 1, anomalies_only=True, limit=1)
        statuses = await AlertRepository(session).counts_by_status()

    logger.info(
        "Seeded %d service-days across %d services (%d flagged) in %.1fs",
        total,
        len(services),
        anomalies,
        result.duration_seconds,
    )
    logger.info("Alert rows by status: %s", statuses or "none")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed synthetic cost history")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--chunk-days", type=int, default=60)
    parser.add_argument(
        "--spike-probability",
        type=float,
        default=0.008,
        help="per service-day chance of a planted spike",
    )
    args = parser.parse_args(argv)

    configure_logging(get_settings())
    try:
        await seed(args.days, args.chunk_days, args.spike_probability)
    finally:
        await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
