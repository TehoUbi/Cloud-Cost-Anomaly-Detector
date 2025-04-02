"""Detection against the database: scoring, persistence, cooldown, backfill."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.db.repositories import AlertRepository, BaselineRepository, CostRepository
from src.detector.service import DetectionService, format_alert_message
from src.detector.zscore import AnomalyResult, Direction, Severity
from src.models.cost import AlertStatus
from tests.conftest import make_costs

pytestmark = pytest.mark.integration

END = date(2026, 10, 1)
SERVICE = "Amazon Elastic Compute Cloud - Compute"


def steady(n: int = 30, value: float = 420.0) -> list[float]:
    """Mild deterministic wobble: enough spread for a usable sigma."""
    return [value + (i % 5) - 2 for i in range(n)]


class TestScoringAndPersistence:
    async def test_every_observation_is_stored(self, session, settings):
        costs = make_costs(SERVICE, steady(20), end=END)
        summary = await DetectionService(session, settings).run(costs)
        assert summary.records_upserted == 20
        _, total = await CostRepository(session).list_records(days=3650)
        assert total == 20

    async def test_clean_history_raises_nothing(self, session, settings):
        costs = make_costs(SERVICE, steady(30), end=END)
        summary = await DetectionService(session, settings).run(costs)
        assert summary.anomalies_found == 0
        assert summary.alerts_created == 0

    async def test_a_spike_is_detected_and_alerted(self, session, settings):
        costs = make_costs(SERVICE, [*steady(29), 5000.0], end=END)
        summary = await DetectionService(session, settings).run(costs)
        assert summary.anomalies_found == 1
        assert summary.alerts_created == 1
        assert summary.anomalies[0].direction is Direction.SPIKE

    async def test_the_scored_row_carries_the_verdict(self, session, settings):
        costs = make_costs(SERVICE, [*steady(29), 5000.0], end=END)
        await DetectionService(session, settings).run(costs)
        rows, _ = await CostRepository(session).list_records(days=3650, anomalies_only=True)
        assert len(rows) == 1
        assert rows[0].usage_date == END
        assert rows[0].zscore > 3.0
        assert rows[0].detector == "robust_zscore"
        assert rows[0].baseline_mean == pytest.approx(420.0, abs=5.0)

    async def test_services_are_scored_independently(self, session, settings):
        costs = [
            *make_costs(SERVICE, [*steady(29), 5000.0], end=END),
            *make_costs("AWS Lambda", steady(30, 34.0), end=END),
        ]
        summary = await DetectionService(session, settings).run(costs)
        assert summary.services_seen == 2
        assert summary.anomalies_found == 1
        assert summary.anomalies[0].service == SERVICE

    async def test_observations_out_of_order_are_sorted(self, session, settings):
        costs = make_costs(SERVICE, [*steady(29), 5000.0], end=END)
        summary = await DetectionService(session, settings).run(list(reversed(costs)))
        assert summary.anomalies_found == 1
        assert summary.anomalies[0].amount == 5000.0

    async def test_warmup_days_are_not_flagged(self, session, settings):
        """min_history_days is 14 in the test config."""
        costs = make_costs(SERVICE, [1.0, 900.0, 2.0], end=END)
        summary = await DetectionService(session, settings).run(costs)
        assert summary.anomalies_found == 0


class TestBaselinePersistence:
    async def test_baseline_is_written(self, session, settings):
        costs = make_costs(SERVICE, steady(30), end=END)
        await DetectionService(session, settings).run(costs)
        baseline = await BaselineRepository(session).get(SERVICE)
        assert baseline is not None
        assert baseline.sample_count == 30
        assert baseline.mean == pytest.approx(420.0, abs=2.0)
        assert baseline.last_evaluated_date == END

    async def test_baseline_tracks_the_configured_window(self, session, settings):
        costs = make_costs(SERVICE, steady(60), end=END)
        await DetectionService(session, settings).run(costs)
        baseline = await BaselineRepository(session).get(SERVICE)
        assert baseline.window_size == settings.window_size
        assert baseline.sample_count == settings.window_size

    async def test_baseline_is_refreshed_on_a_later_poll(self, session, settings):
        service = DetectionService(session, settings)
        await service.run(make_costs(SERVICE, steady(30), end=END))
        await service.run(make_costs(SERVICE, steady(5, 900.0), end=END + timedelta(days=5)))
        baseline = await BaselineRepository(session).get(SERVICE)
        assert baseline.last_evaluated_date == END + timedelta(days=5)
        assert baseline.last_amount == pytest.approx(902.0, abs=3.0)

    async def test_no_baseline_without_observations(self, session, settings):
        await DetectionService(session, settings).run([])
        assert await BaselineRepository(session).get(SERVICE) is None


class TestHistoryContinuity:
    async def test_a_second_poll_uses_stored_history(self, session, settings):
        """The window must survive across invocations; Lambda keeps nothing
        in memory between runs."""
        service = DetectionService(session, settings)
        await service.run(make_costs(SERVICE, steady(30), end=END))
        summary = await service.run(make_costs(SERVICE, [5000.0], end=END + timedelta(days=1)))
        assert summary.days_evaluated == 1
        assert summary.anomalies_found == 1

    async def test_a_restated_day_is_rescored_not_duplicated(self, session, settings):
        service = DetectionService(session, settings)
        await service.run(make_costs(SERVICE, [*steady(29), 430.0], end=END))
        await service.run(make_costs(SERVICE, [5000.0], end=END))
        rows, total = await CostRepository(session).list_records(days=3650)
        assert total == 30
        latest = next(row for row in rows if row.usage_date == END)
        assert latest.amount_usd == 5000.0
        assert latest.is_anomaly


class TestCooldown:
    async def test_repeat_alerts_are_suppressed_inside_the_window(
        self, session, settings, monkeypatch
    ):
        """A runaway instance spikes for days. Alerting every poll trains
        people to filter the channel."""
        monkeypatch.setattr(settings, "alert_cooldown_hours", 24.0)
        costs = make_costs(SERVICE, [*steady(26), 5000.0, 5100.0, 5200.0, 5300.0], end=END)
        summary = await DetectionService(session, settings).run(costs)
        assert summary.alerts_created == 1
        assert summary.alerts_suppressed >= 1

    async def test_suppressed_alerts_are_still_recorded(self, session, settings, monkeypatch):
        monkeypatch.setattr(settings, "alert_cooldown_hours", 24.0)
        costs = make_costs(SERVICE, [*steady(26), 5000.0, 5100.0, 5200.0, 5300.0], end=END)
        await DetectionService(session, settings).run(costs)
        counts = await AlertRepository(session).counts_by_status()
        assert counts.get(AlertStatus.SUPPRESSED, 0) >= 1

    async def test_zero_cooldown_alerts_every_time(self, session, settings, monkeypatch):
        monkeypatch.setattr(settings, "alert_cooldown_hours", 0.0)
        costs = make_costs(SERVICE, [*steady(26), 5000.0, 5100.0, 5200.0, 5300.0], end=END)
        summary = await DetectionService(session, settings).run(costs)
        assert summary.alerts_created >= 2
        assert summary.alerts_suppressed == 0

    async def test_cooldown_is_per_service(self, session, settings, monkeypatch):
        monkeypatch.setattr(settings, "alert_cooldown_hours", 24.0)
        costs = [
            *make_costs(SERVICE, [*steady(29), 5000.0], end=END),
            *make_costs("AWS Lambda", [*steady(29, 34.0), 900.0], end=END),
        ]
        summary = await DetectionService(session, settings).run(costs)
        assert summary.alerts_created == 2


class TestDropPolicy:
    async def test_drops_are_recorded_but_not_alerted(self, session, settings):
        """The day spend returns to normal after a spike is itself a drop."""
        costs = make_costs(SERVICE, [*steady(29), 1.0], end=END)
        summary = await DetectionService(session, settings).run(costs)
        assert summary.anomalies_found == 1
        assert summary.drops_recorded == 1
        assert summary.alerts_created == 0

    async def test_the_drop_is_still_visible_in_the_data(self, session, settings):
        costs = make_costs(SERVICE, [*steady(29), 1.0], end=END)
        await DetectionService(session, settings).run(costs)
        rows, _ = await CostRepository(session).list_records(days=3650, anomalies_only=True)
        assert len(rows) == 1
        assert rows[0].zscore < 0

    async def test_drops_alert_when_enabled(self, session, settings, monkeypatch):
        monkeypatch.setattr(settings, "alert_on_drops", True)
        costs = make_costs(SERVICE, [*steady(29), 1.0], end=END)
        summary = await DetectionService(session, settings).run(costs)
        assert summary.alerts_created == 1


class TestSuppressAlerts:
    async def test_backfill_records_without_queueing(self, session, settings):
        """Seeding history must not leave months of pending alerts for the
        next live poll to deliver."""
        costs = make_costs(SERVICE, [*steady(29), 5000.0], end=END)
        summary = await DetectionService(session, settings).run(costs, suppress_alerts=True)
        assert summary.anomalies_found == 1
        assert summary.alerts_created == 0
        assert summary.alerts_suppressed == 1

        pending, total = await AlertRepository(session).list_alerts(status=AlertStatus.PENDING)
        assert total == 0

    async def test_suppression_reason_is_stored(self, session, settings):
        costs = make_costs(SERVICE, [*steady(29), 5000.0], end=END)
        await DetectionService(session, settings).run(costs, suppress_alerts=True)
        alerts, _ = await AlertRepository(session).list_alerts()
        assert "backfill" in alerts[0].last_error


class TestEnsembleIntegration:
    async def test_ensemble_annotates_the_verdict(self, session, settings, monkeypatch):
        monkeypatch.setattr(settings, "ensemble_enabled", True)
        service = DetectionService(session, settings)
        await service.run(make_costs(SERVICE, steady(40), end=END))
        summary = await service.run(make_costs(SERVICE, [9000.0], end=END + timedelta(days=1)))
        assert summary.anomalies_found == 1
        assert summary.ensemble_confirmations + summary.ensemble_overrides == 1
        assert summary.anomalies[0].detector == "ensemble"

    async def test_disabled_ensemble_leaves_the_detector_name_alone(self, session, settings):
        assert settings.ensemble_enabled is False
        costs = make_costs(SERVICE, [*steady(29), 5000.0], end=END)
        summary = await DetectionService(session, settings).run(costs)
        assert summary.anomalies[0].detector == "robust_zscore"
        assert summary.ensemble_confirmations == 0


class TestAlertMessage:
    def test_message_names_the_service_and_the_numbers(self):
        result = AnomalyResult(
            service="AWS Lambda",
            amount=162.37,
            zscore=18.27,
            baseline_mean=35.77,
            baseline_stddev=6.93,
            is_anomaly=True,
            direction=Direction.SPIKE,
            severity=Severity.CRITICAL,
            reason="spike of $126.61",
        )
        message = format_alert_message(result, date(2026, 9, 22))
        assert "AWS Lambda" in message
        assert "2026-09-22" in message
        assert "$162.37" in message
        assert "+18.27" in message

    def test_a_drop_reads_as_down(self):
        result = AnomalyResult(
            service="AmazonS3",
            amount=5.0,
            zscore=-4.0,
            baseline_mean=100.0,
            baseline_stddev=10.0,
            is_anomaly=True,
            direction=Direction.DROP,
        )
        assert "down" in format_alert_message(result, date(2026, 9, 22))
