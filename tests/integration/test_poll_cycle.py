"""The full cycle: fetch, score, store, deliver, sweep."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from src.db.repositories import AlertRepository, BaselineRepository, CostRepository
from src.models.cost import AlertStatus
from src.notifier.channels import ChannelResponse, TransientDeliveryError
from src.pipeline import backfill, run_poll_cycle
from src.utils.cost_explorer import CostExplorerError, SyntheticCostProvider
from tests.conftest import make_costs

pytestmark = pytest.mark.integration

END = date(2026, 10, 1)
SERVICE = "Amazon Elastic Compute Cloud - Compute"


class ListProvider:
    """Returns a fixed batch, recording what was asked for."""

    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[int, date | None]] = []

    def fetch_daily_costs(self, *, days_back: int, end_date: date | None = None):
        self.calls.append((days_back, end_date))
        return self.rows


class BrokenProvider:
    def fetch_daily_costs(self, *, days_back: int, end_date: date | None = None):
        raise CostExplorerError("Cost Explorer unavailable after 4 attempts")


class ScriptedChannel:
    from src.models.cost import AlertChannel

    def __init__(self, outcomes: list | None = None):
        self.name = self.AlertChannel.WEBHOOK
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


def steady(n: int = 30, value: float = 420.0) -> list[float]:
    return [value + (i % 5) - 2 for i in range(n)]


async def cycle(session, settings, provider, *, channels=None, **kwargs):
    """run_poll_cycle with a dispatcher wired to test channels."""
    from src.notifier.dispatcher import AlertDispatcher

    dispatcher = AlertDispatcher(
        session, settings, channels=[ScriptedChannel()] if channels is None else channels
    )
    return await run_poll_cycle(
        session, settings=settings, provider=provider, dispatcher=dispatcher, **kwargs
    )


class TestHappyPath:
    async def test_clean_data_produces_no_alerts(self, session, settings):
        provider = ListProvider(make_costs(SERVICE, steady(30), end=END))
        result = await cycle(session, settings, provider)
        assert result.observations_fetched == 30
        assert result.detection.anomalies_found == 0
        assert result.delivered == 0

    async def test_a_spike_is_detected_stored_and_delivered(self, session, settings):
        provider = ListProvider(make_costs(SERVICE, [*steady(29), 5000.0], end=END))
        channel = ScriptedChannel()
        result = await cycle(session, settings, provider, channels=[channel])

        assert result.detection.anomalies_found == 1
        assert result.detection.alerts_created == 1
        assert result.delivered == 1
        assert result.failed == 0
        assert channel.calls == 1

        alerts, total = await AlertRepository(session).list_alerts()
        assert total == 1
        assert alerts[0].status == AlertStatus.DELIVERED

    async def test_data_and_baseline_are_both_persisted(self, session, settings):
        provider = ListProvider(make_costs(SERVICE, steady(30), end=END))
        await cycle(session, settings, provider)
        _, total = await CostRepository(session).list_records(days=3650)
        assert total == 30
        assert await BaselineRepository(session).get(SERVICE) is not None

    async def test_duration_is_measured(self, session, settings):
        provider = ListProvider(make_costs(SERVICE, steady(20), end=END))
        result = await cycle(session, settings, provider)
        assert result.duration_seconds > 0

    async def test_summaries_report_separate_durations(self, session, settings):
        """Flattening the two dicts let the inner duration overwrite the outer."""
        provider = ListProvider(make_costs(SERVICE, steady(20), end=END))
        result = await cycle(session, settings, provider)
        payload = result.as_dict()
        assert payload["duration_seconds"] >= payload["detection"]["duration_seconds"]


class TestLookback:
    async def test_lookback_defaults_to_configuration(self, session, settings):
        provider = ListProvider([])
        await cycle(session, settings, provider)
        assert provider.calls[0][0] == settings.poll_lookback_days

    async def test_lookback_can_be_overridden(self, session, settings):
        provider = ListProvider([])
        await cycle(session, settings, provider, lookback_days=7)
        assert provider.calls[0][0] == 7

    async def test_end_date_is_passed_through(self, session, settings):
        provider = ListProvider([])
        await cycle(session, settings, provider, end_date=END)
        assert provider.calls[0][1] == END


class TestEmptyAndBrokenUpstream:
    async def test_no_data_is_not_an_error(self, session, settings):
        result = await cycle(session, settings, ListProvider([]))
        assert result.observations_fetched == 0
        assert result.detection.records_upserted == 0

    async def test_upstream_failure_propagates(self, session, settings):
        """So the API can answer 502 rather than swallow it."""
        with pytest.raises(CostExplorerError):
            await cycle(session, settings, BrokenProvider())

    async def test_duration_is_recorded_even_on_failure(self, session, settings):
        """Timing lives in a finally block, so a failed cycle still reports how
        long it spent before giving up."""
        from src.notifier.dispatcher import AlertDispatcher
        from src.pipeline import PollResult

        captured: list[PollResult] = []
        dispatcher = AlertDispatcher(session, settings, channels=[ScriptedChannel()])

        real_init = PollResult.__init__

        def capture(self, *args, **kwargs):
            real_init(self, *args, **kwargs)
            captured.append(self)

        PollResult.__init__ = capture  # type: ignore[method-assign]
        try:
            with pytest.raises(CostExplorerError):
                await run_poll_cycle(
                    session,
                    settings=settings,
                    provider=BrokenProvider(),
                    dispatcher=dispatcher,
                )
        finally:
            PollResult.__init__ = real_init  # type: ignore[method-assign]

        assert captured, "the cycle should have built a result before failing"
        assert captured[-1].duration_seconds > 0


class TestDeliveryDisabled:
    async def test_scores_and_stores_without_sending(self, session, settings):
        provider = ListProvider(make_costs(SERVICE, [*steady(29), 5000.0], end=END))
        channel = ScriptedChannel()
        result = await cycle(session, settings, provider, channels=[channel], deliver=False)
        assert result.detection.anomalies_found == 1
        assert result.delivered == 0
        assert channel.calls == 0

    async def test_nothing_is_left_pending(self, session, settings):
        provider = ListProvider(make_costs(SERVICE, [*steady(29), 5000.0], end=END))
        await cycle(session, settings, provider, deliver=False)
        _, pending = await AlertRepository(session).list_alerts(status=AlertStatus.PENDING)
        assert pending == 0


class TestDeliveryFailure:
    async def test_failed_delivery_is_counted(self, session, settings):
        provider = ListProvider(make_costs(SERVICE, [*steady(29), 5000.0], end=END))
        channel = ScriptedChannel([TransientDeliveryError("down")] * 20)
        result = await cycle(session, settings, provider, channels=[channel])
        assert result.delivered == 0
        assert result.failed == 1

    async def test_failed_alert_is_scheduled_for_a_sweep(self, session, settings):
        provider = ListProvider(make_costs(SERVICE, [*steady(29), 5000.0], end=END))
        channel = ScriptedChannel([TransientDeliveryError("down")] * 20)
        await cycle(session, settings, provider, channels=[channel])
        alerts, _ = await AlertRepository(session).list_alerts()
        assert alerts[0].status == AlertStatus.FAILED
        assert alerts[0].next_retry_at is not None


class TestSweepOrdering:
    async def test_the_backlog_is_flushed_before_new_detection(self, session, settings):
        """Running the sweep first means a later detection error still leaves
        the backlog drained."""
        provider = ListProvider(make_costs(SERVICE, [*steady(29), 5000.0], end=END))
        down = ScriptedChannel([TransientDeliveryError("down")] * 20)
        await cycle(session, settings, provider, channels=[down])

        alerts, _ = await AlertRepository(session).list_alerts()
        alerts[0].next_retry_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.flush()

        quiet = ListProvider(make_costs(SERVICE, steady(5), end=END + timedelta(days=5)))
        result = await cycle(session, settings, quiet, channels=[ScriptedChannel()])
        assert result.swept == 1
        assert result.swept_delivered == 1


class TestBackfill:
    async def test_covers_the_whole_range_in_chunks(self, session, settings):
        provider = SyntheticCostProvider()
        result = await backfill(
            session, days=120, chunk_days=60, settings=settings, provider=provider
        )
        assert result.observations_fetched > 0
        assert result.detection.days_evaluated == result.detection.records_upserted

    async def test_history_is_seeded(self, session, settings):
        await backfill(
            session, days=90, chunk_days=45, settings=settings, provider=SyntheticCostProvider()
        )
        services = await CostRepository(session).services()
        assert len(services) == 8

    async def test_nothing_is_queued_for_delivery(self, session, settings):
        """Otherwise the next live poll delivers months of historical alerts."""
        await backfill(
            session, days=180, chunk_days=60, settings=settings, provider=SyntheticCostProvider()
        )
        _, pending = await AlertRepository(session).list_alerts(status=AlertStatus.PENDING)
        assert pending == 0

    async def test_historical_anomalies_are_still_recorded(self, session, settings):
        await backfill(
            session, days=180, chunk_days=60, settings=settings, provider=SyntheticCostProvider()
        )
        counts = await AlertRepository(session).counts_by_status()
        assert counts.get(AlertStatus.SUPPRESSED, 0) > 0

    async def test_backfill_then_live_poll_only_alerts_on_new_findings(self, session, settings):
        provider = SyntheticCostProvider()
        await backfill(session, days=120, chunk_days=60, settings=settings, provider=provider)
        before = await AlertRepository(session).counts_by_status()

        result = await cycle(session, settings, provider, lookback_days=45)
        after = await AlertRepository(session).counts_by_status()

        assert after.get("delivered", 0) == result.delivered
        assert before.get("delivered", 0) == 0
