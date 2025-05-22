"""Delivery against the database: status transitions, audit trail, sweeps."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from src.db.repositories import AlertRepository, CostRepository
from src.models.cost import Alert, AlertAttempt, AlertChannel, AlertStatus
from src.notifier.channels import (
    ChannelResponse,
    PermanentDeliveryError,
    TransientDeliveryError,
)
from src.notifier.dispatcher import (
    SWEEP_MAX_ATTEMPTS,
    AlertDispatcher,
    build_payload_from_alert,
)

pytestmark = pytest.mark.integration

DAY = date(2026, 10, 1)


class ScriptedChannel:
    """Replays outcomes; anything past the script succeeds."""

    def __init__(self, name: AlertChannel = AlertChannel.WEBHOOK, outcomes: list | None = None):
        self.name = name
        self.outcomes = list(outcomes or [])
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return True

    async def deliver(self, payload):
        self.calls += 1
        outcome = (
            self.outcomes.pop(0)
            if self.outcomes
            else ChannelResponse(succeeded=True, status_code=200)
        )
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


async def make_alert(session, *, service: str = "AmazonEC2", usage_date: date = DAY, **overrides):
    record = await CostRepository(session).upsert(
        service=service, usage_date=usage_date, amount=900.0
    )
    alert = Alert(
        cost_record_id=record.id,
        service=service,
        usage_date=usage_date,
        amount=record.amount,
        zscore=7.0,
        baseline_mean=180.0,
        baseline_stddev=8.0,
        severity="warning",
        message=f"{service} spike",
        **{"status": AlertStatus.PENDING, **overrides},
    )
    return await AlertRepository(session).create(alert)


def dispatcher(session, settings, channels):
    return AlertDispatcher(session, settings, channels=channels)


class TestSuccessfulDelivery:
    async def test_marks_delivered(self, session, settings):
        alert = await make_alert(session)
        channel = ScriptedChannel()
        outcome = await dispatcher(session, settings, [channel]).dispatch(alert)
        assert outcome.delivered
        assert alert.status == AlertStatus.DELIVERED
        assert alert.delivered_at is not None
        assert alert.next_retry_at is None
        assert alert.last_error is None

    async def test_counts_a_single_attempt(self, session, settings):
        alert = await make_alert(session)
        channel = ScriptedChannel()
        await dispatcher(session, settings, [channel]).dispatch(alert)
        assert channel.calls == 1
        assert alert.attempts == 1

    async def test_records_the_successful_attempt(self, session, settings):
        alert = await make_alert(session)
        await dispatcher(session, settings, [ScriptedChannel()]).dispatch(alert)
        await session.refresh(alert, ["delivery_attempts"])
        assert len(alert.delivery_attempts) == 1
        assert alert.delivery_attempts[0].succeeded
        assert alert.delivery_attempts[0].status_code == 200


class TestRetryWithinOneDispatch:
    async def test_retries_then_succeeds(self, session, settings):
        alert = await make_alert(session)
        channel = ScriptedChannel(outcomes=[TransientDeliveryError("503", status_code=503)] * 2)
        outcome = await dispatcher(session, settings, [channel]).dispatch(alert)
        assert outcome.delivered
        assert channel.calls == 3

    async def test_every_attempt_is_audited(self, session, settings):
        """This is what makes "why did this alert never arrive?" answerable."""
        alert = await make_alert(session)
        channel = ScriptedChannel(outcomes=[TransientDeliveryError("503", status_code=503)] * 2)
        await dispatcher(session, settings, [channel]).dispatch(alert)
        await session.refresh(alert, ["delivery_attempts"])
        attempts = alert.delivery_attempts
        assert [a.succeeded for a in attempts] == [False, False, True]
        assert [a.attempt_number for a in attempts] == [1, 2, 3]
        assert attempts[0].status_code == 503

    async def test_exhausting_the_budget_marks_failed(self, session, settings):
        alert = await make_alert(session)
        channel = ScriptedChannel(outcomes=[TransientDeliveryError("down")] * 10)
        outcome = await dispatcher(session, settings, [channel]).dispatch(alert)
        assert outcome.status is AlertStatus.FAILED
        assert channel.calls == settings.alert_max_retries
        assert alert.last_error and "down" in alert.last_error

    async def test_permanent_failure_is_not_retried(self, session, settings):
        alert = await make_alert(session)
        channel = ScriptedChannel(outcomes=[PermanentDeliveryError("400", status_code=400)])
        outcome = await dispatcher(session, settings, [channel]).dispatch(alert)
        assert outcome.status is AlertStatus.FAILED
        assert channel.calls == 1

    async def test_permanent_failure_is_abandoned_not_scheduled(self, session, settings):
        alert = await make_alert(session)
        channel = ScriptedChannel(outcomes=[PermanentDeliveryError("400", status_code=400)])
        await dispatcher(session, settings, [channel]).dispatch(alert)
        assert alert.next_retry_at is None, "a 400 will not become a 200 later"

    async def test_transient_exhaustion_schedules_a_sweep(self, session, settings):
        alert = await make_alert(session)
        channel = ScriptedChannel(outcomes=[TransientDeliveryError("down")] * 10)
        await dispatcher(session, settings, [channel]).dispatch(alert)
        assert alert.next_retry_at is not None
        assert alert.next_retry_at > datetime.now(UTC)


class TestMultiChannel:
    async def test_all_succeeding_is_delivered(self, session, settings):
        alert = await make_alert(session)
        channels = [ScriptedChannel(AlertChannel.WEBHOOK), ScriptedChannel(AlertChannel.EMAIL)]
        outcome = await dispatcher(session, settings, channels).dispatch(alert)
        assert outcome.status is AlertStatus.DELIVERED
        assert len(outcome.channels) == 2

    async def test_mixed_results_are_partial(self, session, settings):
        alert = await make_alert(session)
        channels = [
            ScriptedChannel(AlertChannel.WEBHOOK),
            ScriptedChannel(AlertChannel.EMAIL, [TransientDeliveryError("smtp")] * 10),
        ]
        outcome = await dispatcher(session, settings, channels).dispatch(alert)
        assert outcome.status is AlertStatus.PARTIAL
        assert alert.delivered_at is not None, "the channel that worked still delivered"

    async def test_partial_is_retried(self, session, settings):
        alert = await make_alert(session)
        channels = [
            ScriptedChannel(AlertChannel.WEBHOOK),
            ScriptedChannel(AlertChannel.EMAIL, [TransientDeliveryError("smtp")] * 10),
        ]
        await dispatcher(session, settings, channels).dispatch(alert)
        assert alert.next_retry_at is not None

    async def test_a_dead_channel_does_not_block_the_other(self, session, settings):
        alert = await make_alert(session)
        email = ScriptedChannel(AlertChannel.EMAIL)
        channels = [
            ScriptedChannel(AlertChannel.WEBHOOK, [PermanentDeliveryError("gone")]),
            email,
        ]
        await dispatcher(session, settings, channels).dispatch(alert)
        assert email.calls == 1

    async def test_attempts_are_attributed_per_channel(self, session, settings):
        alert = await make_alert(session)
        channels = [
            ScriptedChannel(AlertChannel.WEBHOOK, [TransientDeliveryError("x")]),
            ScriptedChannel(AlertChannel.EMAIL),
        ]
        await dispatcher(session, settings, channels).dispatch(alert)
        await session.refresh(alert, ["delivery_attempts"])
        by_channel: dict[str, int] = {}
        for attempt in alert.delivery_attempts:
            by_channel[attempt.channel] = by_channel.get(attempt.channel, 0) + 1
        assert by_channel == {"webhook": 2, "email": 1}


class TestNoChannels:
    async def test_stays_pending(self, session, settings):
        """Configuring a channel later should pick these up, not find them
        written off as undeliverable."""
        alert = await make_alert(session)
        outcome = await dispatcher(session, settings, []).dispatch(alert)
        assert outcome.status is AlertStatus.PENDING
        assert alert.last_error == "no alert channels configured"

    async def test_does_not_consume_the_retry_budget(self, session, settings):
        alert = await make_alert(session)
        await dispatcher(session, settings, []).dispatch(alert)
        assert alert.attempts == 0
        assert alert.next_retry_at is None


class TestDispatchPending:
    async def test_delivers_every_pending_alert(self, session, settings):
        for offset in range(3):
            await make_alert(session, usage_date=DAY - timedelta(days=offset))
        outcomes = await dispatcher(session, settings, [ScriptedChannel()]).dispatch_pending()
        assert len(outcomes) == 3
        assert all(outcome.delivered for outcome in outcomes)

    async def test_ignores_already_delivered(self, session, settings):
        await make_alert(session, status=AlertStatus.DELIVERED)
        outcomes = await dispatcher(session, settings, [ScriptedChannel()]).dispatch_pending()
        assert outcomes == []

    async def test_ignores_suppressed(self, session, settings):
        await make_alert(session, status=AlertStatus.SUPPRESSED)
        assert await dispatcher(session, settings, [ScriptedChannel()]).dispatch_pending() == []


class TestSweep:
    async def test_nothing_due_yet_is_left_alone(self, session, settings):
        channel = ScriptedChannel()
        await make_alert(
            session,
            status=AlertStatus.FAILED,
            next_retry_at=datetime.now(UTC) + timedelta(hours=2),
        )
        assert await dispatcher(session, settings, [channel]).sweep() == []
        assert channel.calls == 0

    async def test_due_alerts_are_retried(self, session, settings):
        alert = await make_alert(
            session,
            status=AlertStatus.FAILED,
            next_retry_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        outcomes = await dispatcher(session, settings, [ScriptedChannel()]).sweep()
        assert len(outcomes) == 1
        assert outcomes[0].delivered
        assert alert.status == AlertStatus.DELIVERED

    async def test_an_outage_longer_than_the_budget_still_delivers(self, session, settings):
        """The point of the cross-poll tier: in-process retries alone would
        have dropped this alert."""
        alert = await make_alert(session)
        down = ScriptedChannel(outcomes=[TransientDeliveryError("down")] * 10)
        await dispatcher(session, settings, [down]).dispatch(alert)
        assert alert.status == AlertStatus.FAILED

        recovered = ScriptedChannel()
        later = datetime.now(UTC) + timedelta(days=1)
        outcomes = await dispatcher(session, settings, [recovered]).sweep(now=later)
        assert len(outcomes) == 1
        assert alert.status == AlertStatus.DELIVERED
        assert alert.next_retry_at is None

    async def test_backoff_grows_across_sweeps(self, session, settings):
        alert = await make_alert(session)
        channel = ScriptedChannel(outcomes=[TransientDeliveryError("down")] * 100)
        disp = dispatcher(session, settings, [channel])

        await disp.dispatch(alert)
        first_gap = alert.next_retry_at - datetime.now(UTC)
        await disp.dispatch(alert)
        second_gap = alert.next_retry_at - datetime.now(UTC)
        assert second_gap > first_gap

    async def test_alert_is_abandoned_once_the_budget_runs_out(self, session, settings):
        alert = await make_alert(session, attempts=SWEEP_MAX_ATTEMPTS - 1)
        channel = ScriptedChannel(outcomes=[TransientDeliveryError("down")] * 100)
        await dispatcher(session, settings, [channel]).dispatch(alert)
        assert alert.attempts >= SWEEP_MAX_ATTEMPTS
        assert alert.next_retry_at is None
        assert alert.status == AlertStatus.FAILED

    async def test_sweep_limit_is_respected(self, session, settings):
        past = datetime.now(UTC) - timedelta(minutes=5)
        for offset in range(5):
            await make_alert(
                session,
                usage_date=DAY - timedelta(days=offset),
                status=AlertStatus.FAILED,
                next_retry_at=past,
            )
        outcomes = await dispatcher(session, settings, [ScriptedChannel()]).sweep(limit=2)
        assert len(outcomes) == 2


class TestPayloadFromStoredAlert:
    async def test_rebuilds_from_the_row(self, session, settings):
        """A swept alert is delivered long after the AnomalyResult is gone."""
        alert = await make_alert(session)
        payload = build_payload_from_alert(alert)
        assert payload.service == "AmazonEC2"
        assert payload.amount == 900.0
        assert payload.zscore == 7.0
        assert payload.alert_id == alert.id
        assert payload.direction == "spike"

    async def test_a_drop_is_reconstructed_as_a_drop(self, session, settings):
        record = await CostRepository(session).upsert(
            service="AmazonS3", usage_date=DAY, amount=5.0
        )
        alert = Alert(
            cost_record_id=record.id,
            service="AmazonS3",
            usage_date=DAY,
            amount=record.amount,
            zscore=-6.0,
            baseline_mean=100.0,
            baseline_stddev=10.0,
            severity="warning",
            status=AlertStatus.PENDING,
            message="drop",
        )
        assert build_payload_from_alert(alert).direction == "drop"

    async def test_zero_baseline_does_not_divide_by_zero(self, session, settings):
        record = await CostRepository(session).upsert(service="New", usage_date=DAY, amount=5.0)
        alert = Alert(
            cost_record_id=record.id,
            service="New",
            usage_date=DAY,
            amount=record.amount,
            zscore=3.5,
            baseline_mean=0.0,
            baseline_stddev=1.0,
            severity="info",
            status=AlertStatus.PENDING,
            message="new service",
        )
        assert build_payload_from_alert(alert).delta_pct == 0.0


class TestAttemptNumbering:
    async def test_numbering_continues_across_dispatches(self, session, settings):
        """So the audit log reads as one escalating history, not three runs of 1-3."""
        alert = await make_alert(session)
        channel = ScriptedChannel(outcomes=[TransientDeliveryError("down")] * 100)
        disp = dispatcher(session, settings, [channel])
        await disp.dispatch(alert)
        await disp.dispatch(alert)

        total = (
            await session.execute(
                select(func.count(AlertAttempt.id)).where(AlertAttempt.alert_id == alert.id)
            )
        ).scalar_one()
        numbers = (
            (
                await session.execute(
                    select(AlertAttempt.attempt_number)
                    .where(AlertAttempt.alert_id == alert.id)
                    .order_by(AlertAttempt.attempt_number)
                )
            )
            .scalars()
            .all()
        )
        assert total == settings.alert_max_retries * 2
        assert list(numbers) == list(range(1, settings.alert_max_retries * 2 + 1))


class TestPayloadUsesStoredReasoning:
    async def test_reason_and_detector_come_from_the_row(self, session, settings):
        alert = await make_alert(
            session, detector="ensemble", reason="spike of $720 confirmed by the forest"
        )
        payload = build_payload_from_alert(alert)
        assert payload.detector == "ensemble"
        assert payload.reason == "spike of $720 confirmed by the forest"
        assert payload.reason != payload.message

    async def test_falls_back_for_alerts_written_before_the_columns_existed(
        self, session, settings
    ):
        """The migration adds these nullable with no backfill, so older rows
        genuinely have nothing to show."""
        alert = await make_alert(session, detector=None, reason=None)
        payload = build_payload_from_alert(alert)
        assert payload.reason == alert.message
        assert payload.detector == "robust_zscore"

    async def test_rendered_email_does_not_duplicate_itself(self, session, settings):
        from src.notifier.payload import render_text

        alert = await make_alert(
            session, detector="ensemble", reason="isolation forest flags the point"
        )
        body = render_text(build_payload_from_alert(alert))
        assert body.count(alert.message) == 1
        assert "isolation forest flags the point" in body
