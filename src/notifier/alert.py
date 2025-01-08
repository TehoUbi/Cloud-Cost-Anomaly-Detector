"""
Alert notifier with exponential backoff retry.

Delivers anomaly alerts via webhook (email or HTTP endpoint).
Retries failed deliveries with exponential backoff up to MAX_RETRIES.
"""

import asyncio
import logging
import os
import httpx
from dataclasses import dataclass
from src.detector.zscore import AnomalyResult

logger = logging.getLogger(__name__)

ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
MAX_RETRIES = int(os.getenv("ALERT_MAX_RETRIES", "5"))
BASE_DELAY = float(os.getenv("ALERT_BASE_DELAY_SECONDS", "1.0"))


@dataclass
class AlertPayload:
    service: str
    amount: float
    zscore: float
    baseline_mean: float
    baseline_stddev: float
    message: str


def build_payload(result: AnomalyResult) -> AlertPayload:
    return AlertPayload(
        service=result.service,
        amount=result.amount,
        zscore=round(result.zscore, 2),
        baseline_mean=round(result.baseline_mean, 2),
        baseline_stddev=round(result.baseline_stddev, 2),
        message=(
            f"⚠️ Cost spike detected for {result.service}: "
            f"${result.amount:.2f} (z={result.zscore:.2f}, "
            f"baseline avg=${result.baseline_mean:.2f})"
        ),
    )


async def send_alert(result: AnomalyResult) -> bool:
    """
    Send an anomaly alert with exponential backoff retry.
    Returns True if delivery succeeded, False after all retries exhausted.
    """
    if not ALERT_WEBHOOK_URL:
        logger.warning("ALERT_WEBHOOK_URL not set; skipping alert delivery.")
        return False

    payload = build_payload(result)

    async with httpx.AsyncClient() as client:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.post(
                    ALERT_WEBHOOK_URL,
                    json=payload.__dict__,
                    timeout=10.0,
                )
                response.raise_for_status()
                logger.info("Alert delivered on attempt %d", attempt)
                return True
            except httpx.HTTPError as e:
                delay = BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "Alert delivery failed (attempt %d/%d): %s. Retrying in %.1fs...",
                    attempt, MAX_RETRIES, e, delay,
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(delay)

    logger.error("Alert delivery failed after %d attempts.", MAX_RETRIES)
    return False
