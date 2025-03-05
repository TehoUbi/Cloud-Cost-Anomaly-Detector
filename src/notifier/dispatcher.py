"""Alert delivery: retries, per-attempt audit trail, and status bookkeeping.

Two layers of retry, because they fail for different reasons:

*In-process* — a channel's attempts inside one dispatch call, on the
exponential ladder from ``ALERT_BASE_DELAY_SECONDS``. This covers a blip: a
timeout, a 503, a dropped connection.

*Cross-poll* — an alert that exhausts its in-process budget is left with a
``next_retry_at`` and picked up by :meth:`AlertDispatcher.sweep` on a later
poll. This covers an outage longer than one alert's retry budget, which the
in-process loop alone would simply lose.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import Settings, get_settings
from src.db.repositories import AlertRepository
from src.models.cost import Alert, AlertAttempt, AlertStatus
from src.notifier.channels import (
    ChannelResponse,
    NotificationChannel,
    PermanentDeliveryError,
    TransientDeliveryError,
    build_channels,
)
from src.notifier.payload import AlertPayload
from src.utils.backoff import BackoffPolicy, retry_async

logger = logging.getLogger(__name__)

# Cross-poll ladder, deliberately coarser than the in-process one: a poll runs
# every few hours, so retrying one second later would just fail again.
SWEEP_BASE_DELAY = timedelta(minutes=15)
SWEEP_MAX_DELAY = timedelta(hours=6)
SWEEP_MAX_ATTEMPTS = 25
"""Total attempts across all sweeps before an alert is abandoned. At the ladder
above that is roughly three days of trying."""


@dataclass(slots=True)
class ChannelOutcome:
    channel: str
    succeeded: bool
    attempts: int
    status_code: int | None = None
    error: str | None = None
    permanent: bool = False


@dataclass(slots=True)
class DeliveryOutcome:
    alert_id: str
    service: str
    status: AlertStatus
    channels: list[ChannelOutcome] = field(default_factory=list)

    @property
    def delivered(self) -> bool:
        return self.status is AlertStatus.DELIVERED

    @property
    def total_attempts(self) -> int:
        return sum(channel.attempts for channel in self.channels)


class AlertDispatcher:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings | None = None,
        channels: list[NotificationChannel] | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.repo = AlertRepository(session)
        self._channels = channels if channels is not None else build_channels(self.settings)
        self.policy = BackoffPolicy(
            base_delay=self.settings.alert_base_delay_seconds,
            max_delay=self.settings.alert_max_delay_seconds,
            max_attempts=self.settings.alert_max_retries,
            jitter=self.settings.alert_backoff_jitter,
        )

    @property
    def channels(self) -> list[NotificationChannel]:
        return list(self._channels)

    # --- public API --------------------------------------------------------

    async def dispatch(self, alert: Alert) -> DeliveryOutcome:
        """Deliver one alert to every configured channel."""
        payload = build_payload_from_alert(alert)
        outcome = DeliveryOutcome(alert_id=alert.id, service=alert.service, status=alert.status)  # type: ignore[arg-type]

        if not self._channels:
            # Nothing to deliver to. Left pending rather than failed so that
            # configuring a channel later picks these up instead of writing
            # them off as undeliverable.
            alert.last_error = "no alert channels configured"
            alert.next_retry_at = None
            alert.status = AlertStatus.PENDING
            outcome.status = AlertStatus.PENDING
            return outcome

        attempt_rows: list[AlertAttempt] = []
        for channel in self._channels:
            channel_outcome = await self._deliver_to_channel(channel, payload, alert, attempt_rows)
            outcome.channels.append(channel_outcome)

        await self.repo.record_attempts(attempt_rows)
        self._finalize(alert, outcome)
        return outcome

    async def dispatch_pending(self, *, limit: int = 50) -> list[DeliveryOutcome]:
        """Deliver alerts the detector has just queued."""
        pending, _ = await self.repo.list_alerts(status=AlertStatus.PENDING, limit=limit)
        return [await self.dispatch(alert) for alert in pending]

    async def sweep(self, *, limit: int = 50, now: datetime | None = None) -> list[DeliveryOutcome]:
        """Re-attempt alerts whose cross-poll backoff has elapsed."""
        now = now or datetime.now(UTC)
        due = await self.repo.due_for_retry(now=now, limit=limit)
        if due:
            logger.info("Sweeping %d alert(s) due for retry", len(due))
        return [await self.dispatch(alert) for alert in due]

    async def aclose(self) -> None:
        for channel in self._channels:
            closer = getattr(channel, "aclose", None)
            if closer is not None:
                await closer()

    # --- internals ---------------------------------------------------------

    async def _deliver_to_channel(
        self,
        channel: NotificationChannel,
        payload: AlertPayload,
        alert: Alert,
        attempt_rows: list[AlertAttempt],
    ) -> ChannelOutcome:
        attempt_counter = 0
        permanent = False

        async def attempt() -> ChannelResponse:
            nonlocal attempt_counter, permanent
            attempt_counter += 1
            number = alert.attempts + attempt_counter
            try:
                response = await channel.deliver(payload)
            except PermanentDeliveryError as exc:
                permanent = True
                attempt_rows.append(
                    AlertAttempt(
                        alert_id=alert.id,
                        channel=channel.name.value,
                        attempt_number=number,
                        succeeded=False,
                        status_code=exc.status_code,
                        error=str(exc)[:1000],
                    )
                )
                raise
            except TransientDeliveryError as exc:
                attempt_rows.append(
                    AlertAttempt(
                        alert_id=alert.id,
                        channel=channel.name.value,
                        attempt_number=number,
                        succeeded=False,
                        status_code=exc.status_code,
                        error=str(exc)[:1000],
                    )
                )
                raise
            attempt_rows.append(
                AlertAttempt(
                    alert_id=alert.id,
                    channel=channel.name.value,
                    attempt_number=number,
                    succeeded=True,
                    status_code=response.status_code,
                    duration_ms=response.duration_ms,
                )
            )
            return response

        def log_retry(number: int, exc: BaseException, delay: float) -> None:
            logger.warning(
                "%s delivery for alert %s failed (attempt %d/%d): %s — retrying in %.1fs",
                channel.name.value,
                alert.id,
                number,
                self.policy.max_attempts,
                exc,
                delay,
            )

        try:
            response = await retry_async(
                attempt,
                policy=self.policy,
                retry_on=(TransientDeliveryError,),
                give_up_on=(PermanentDeliveryError,),
                on_failure=log_retry,
            )
        except PermanentDeliveryError as exc:
            logger.error(
                "%s delivery for alert %s failed permanently: %s",
                channel.name.value,
                alert.id,
                exc,
            )
            return ChannelOutcome(
                channel=channel.name.value,
                succeeded=False,
                attempts=attempt_counter,
                status_code=exc.status_code,
                error=str(exc)[:1000],
                permanent=True,
            )
        except TransientDeliveryError as exc:
            logger.error(
                "%s delivery for alert %s exhausted %d attempts: %s",
                channel.name.value,
                alert.id,
                attempt_counter,
                exc,
            )
            return ChannelOutcome(
                channel=channel.name.value,
                succeeded=False,
                attempts=attempt_counter,
                status_code=exc.status_code,
                error=str(exc)[:1000],
            )

        return ChannelOutcome(
            channel=channel.name.value,
            succeeded=True,
            attempts=attempt_counter,
            status_code=response.status_code,
        )

    def _finalize(self, alert: Alert, outcome: DeliveryOutcome) -> None:
        """Fold the per-channel results into the alert's stored state."""
        now = datetime.now(UTC)
        succeeded = [channel for channel in outcome.channels if channel.succeeded]
        failed = [channel for channel in outcome.channels if not channel.succeeded]

        alert.attempts += outcome.total_attempts
        alert.last_attempted_at = now

        if not failed:
            alert.status = AlertStatus.DELIVERED
            alert.delivered_at = now
            alert.next_retry_at = None
            alert.last_error = None
        else:
            alert.status = AlertStatus.PARTIAL if succeeded else AlertStatus.FAILED
            alert.last_error = "; ".join(
                f"{channel.channel}: {channel.error}" for channel in failed
            )[:2000]
            if succeeded:
                alert.delivered_at = alert.delivered_at or now

            # A permanent failure on every remaining channel, or an exhausted
            # total budget, means stop. Otherwise schedule a cross-poll retry.
            all_permanent = all(channel.permanent for channel in failed)
            if all_permanent or alert.attempts >= SWEEP_MAX_ATTEMPTS:
                alert.next_retry_at = None
                logger.error(
                    "Alert %s abandoned after %d attempts (%s)",
                    alert.id,
                    alert.attempts,
                    "permanent failure" if all_permanent else "retry budget exhausted",
                )
            else:
                alert.next_retry_at = now + self._sweep_delay(alert.attempts)

        outcome.status = AlertStatus(alert.status)

    @staticmethod
    def _sweep_delay(attempts: int) -> timedelta:
        """Coarse exponential backoff between polls, capped."""
        exponent = max(attempts - 1, 0)
        delay = SWEEP_BASE_DELAY * (2 ** min(exponent, 10))
        return min(delay, SWEEP_MAX_DELAY)


def build_payload_from_alert(alert: Alert) -> AlertPayload:
    """Rebuild a payload from a stored alert.

    A swept alert is delivered from the row, not from the in-memory
    AnomalyResult that produced it — that object is long gone by then.
    """
    baseline_mean = alert.baseline_mean
    delta = alert.amount_usd - baseline_mean
    return AlertPayload(
        service=alert.service,
        usage_date=alert.usage_date.isoformat(),
        amount=round(alert.amount_usd, 2),
        zscore=round(alert.zscore, 2),
        baseline_mean=round(baseline_mean, 2),
        baseline_stddev=round(alert.baseline_stddev, 2),
        delta=round(delta, 2),
        delta_pct=round((delta / baseline_mean * 100) if baseline_mean else 0.0, 1),
        direction="spike" if delta >= 0 else "drop",
        severity=alert.severity,
        detector="robust_zscore",
        reason=alert.message,
        message=alert.message,
        alert_id=alert.id,
        generated_at=(alert.created_at or datetime.now(UTC)).isoformat(),
    )
