"""Convenience facade over the channel/dispatcher split.

:func:`send_alert` delivers a single :class:`AnomalyResult` without a database
row behind it — useful for a smoke test of alert configuration, and the entry
point the ``/alerts/test`` endpoint uses. Everything the pipeline produces goes
through :class:`src.notifier.dispatcher.AlertDispatcher` instead, because only
that path records attempts and can be retried on a later poll.
"""

from __future__ import annotations

import logging
from datetime import date

from src.config import Settings, get_settings
from src.detector.zscore import AnomalyResult
from src.notifier.channels import (
    NotificationChannel,
    PermanentDeliveryError,
    TransientDeliveryError,
    build_channels,
)
from src.notifier.payload import AlertPayload, build_payload, render_html, render_text
from src.utils.backoff import BackoffPolicy, retry_async

logger = logging.getLogger(__name__)

__all__ = [
    "AlertPayload",
    "build_payload",
    "render_html",
    "render_text",
    "send_alert",
    "send_payload",
]


async def send_alert(
    result: AnomalyResult,
    *,
    usage_date: date | None = None,
    settings: Settings | None = None,
    channels: list[NotificationChannel] | None = None,
) -> bool:
    """Deliver one anomaly. True only if every configured channel accepted it."""
    return await send_payload(
        build_payload(result, usage_date=usage_date),
        settings=settings,
        channels=channels,
    )


async def send_payload(
    payload: AlertPayload,
    *,
    settings: Settings | None = None,
    channels: list[NotificationChannel] | None = None,
) -> bool:
    settings = settings or get_settings()
    targets = channels if channels is not None else build_channels(settings)
    if not targets:
        logger.warning("No alert channels configured; dropping alert for %s", payload.service)
        return False

    policy = BackoffPolicy(
        base_delay=settings.alert_base_delay_seconds,
        max_delay=settings.alert_max_delay_seconds,
        max_attempts=settings.alert_max_retries,
        jitter=settings.alert_backoff_jitter,
    )

    all_delivered = True
    for channel in targets:
        try:
            await retry_async(
                lambda channel=channel: channel.deliver(payload),  # type: ignore[misc]
                policy=policy,
                retry_on=(TransientDeliveryError,),
                give_up_on=(PermanentDeliveryError,),
                on_failure=lambda attempt, exc, delay, channel=channel: logger.warning(
                    "%s delivery failed (attempt %d/%d): %s — retrying in %.1fs",
                    channel.name.value,
                    attempt,
                    policy.max_attempts,
                    exc,
                    delay,
                ),
            )
            logger.info("Alert delivered to %s for %s", channel.name.value, payload.service)
        except (PermanentDeliveryError, TransientDeliveryError) as exc:
            logger.error("Alert delivery to %s failed: %s", channel.name.value, exc)
            all_delivered = False

    return all_delivered
