"""
scripts/poll_and_detect.py

Scheduled entrypoint: fetches latest AWS costs, runs z-score detection,
persists results, and fires alerts for any anomalies found.

Run directly:
    python -m scripts.poll_and_detect

Or invoke via the /costs/poll API endpoint.
"""

import asyncio
import logging
import os
import uuid
from datetime import date

from src.db.session import SessionLocal, init_db
from src.utils.cost_explorer import fetch_daily_costs
from src.detector.zscore import ZScoreDetector
from src.notifier.alert import send_alert
from src.models.cost import CostRecord, AlertLog

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

POLL_INTERVAL_HOURS = float(os.getenv("POLL_INTERVAL_HOURS", "6"))


async def run_once():
    """Fetch costs, detect anomalies, persist, and alert."""
    logger.info("Polling AWS Cost Explorer...")
    daily_costs = fetch_daily_costs(days_back=31)  # 30 days history + today

    # Group by service
    from collections import defaultdict
    by_service: dict[str, list] = defaultdict(list)
    for entry in daily_costs:
        by_service[entry.service].append(entry)

    async with SessionLocal() as db:
        for service, entries in by_service.items():
            entries.sort(key=lambda e: e.date)
            history = [e.amount for e in entries[:-1]]  # all but latest
            latest = entries[-1]

            detector = ZScoreDetector(service=service)
            detector.load_history(history)
            result = detector.evaluate(latest.amount)

            record = CostRecord(
                id=str(uuid.uuid4()),
                service=service,
                date=date.fromisoformat(latest.date),
                amount=latest.amount,
                unit=latest.unit,
                zscore=result.zscore,
                is_anomaly=result.is_anomaly,
            )
            db.add(record)

            if result.is_anomaly:
                logger.warning("Anomaly detected: %s z=%.2f", service, result.zscore)
                delivered = await send_alert(result)
                db.add(AlertLog(
                    id=str(uuid.uuid4()),
                    cost_record_id=record.id,
                    delivered=delivered,
                    attempts=1,
                ))

        await db.commit()
    logger.info("Poll complete.")


async def main():
    await init_db()
    while True:
        try:
            await run_once()
        except Exception as e:
            logger.error("Poll failed: %s", e)
        await asyncio.sleep(POLL_INTERVAL_HOURS * 3600)


if __name__ == "__main__":
    asyncio.run(main())
