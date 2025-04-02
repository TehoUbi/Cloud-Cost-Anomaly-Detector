"""Repositories against a real database."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from src.db.repositories import AlertRepository, BaselineRepository, CostRepository
from src.models.cost import Alert, AlertAttempt, AlertStatus

pytestmark = pytest.mark.integration

DAY = date(2026, 10, 1)


async def seed_days(repo: CostRepository, service: str, amounts: list[float], end: date = DAY):
    start = end - timedelta(days=len(amounts) - 1)
    records = []
    for offset, amount in enumerate(amounts):
        records.append(
            await repo.upsert(
                service=service, usage_date=start + timedelta(days=offset), amount=amount
            )
        )
    return records


class TestCostUpsert:
    async def test_inserts_a_new_service_day(self, session):
        repo = CostRepository(session)
        record = await repo.upsert(service="AmazonEC2", usage_date=DAY, amount=12.5)
        assert record.id
        assert record.amount_usd == 12.5

    async def test_restated_day_updates_in_place(self, session):
        """Cost Explorer revises recent days; the old insert-only path
        duplicated every service-day on each poll."""
        repo = CostRepository(session)
        first = await repo.upsert(service="AmazonEC2", usage_date=DAY, amount=12.5)
        second = await repo.upsert(
            service="AmazonEC2", usage_date=DAY, amount=99.0, zscore=4.2, is_anomaly=True
        )
        assert first.id == second.id
        assert second.amount_usd == 99.0
        assert second.is_anomaly is True
        assert second.zscore == 4.2

    async def test_upsert_returns_fresh_state_not_the_cached_instance(self, session):
        """Without populate_existing the ORM hands back the stale identity-map
        object and the caller sees the pre-update values."""
        repo = CostRepository(session)
        await repo.upsert(service="AmazonEC2", usage_date=DAY, amount=10.0)
        updated = await repo.upsert(service="AmazonEC2", usage_date=DAY, amount=50.0)
        assert updated.amount_usd == 50.0

    async def test_only_one_row_survives_a_repoll(self, session):
        repo = CostRepository(session)
        for amount in (1.0, 2.0, 3.0):
            await repo.upsert(service="AmazonEC2", usage_date=DAY, amount=amount)
        rows, total = await repo.list_records(days=3650)
        assert total == 1
        assert rows[0].amount_usd == 3.0

    async def test_different_services_do_not_collide(self, session):
        repo = CostRepository(session)
        await repo.upsert(service="AmazonEC2", usage_date=DAY, amount=1.0)
        await repo.upsert(service="AWSLambda", usage_date=DAY, amount=2.0)
        _, total = await repo.list_records(days=3650)
        assert total == 2

    async def test_amount_keeps_sub_cent_precision(self, session):
        """NUMERIC, not float: daily spend gets summed."""
        repo = CostRepository(session)
        record = await repo.upsert(service="AmazonEC2", usage_date=DAY, amount=Decimal("0.123456"))
        assert record.amount == Decimal("0.123456")


class TestCostHistory:
    async def test_returns_oldest_first(self, session):
        repo = CostRepository(session)
        await seed_days(repo, "AmazonEC2", [1.0, 2.0, 3.0])
        history = await repo.history("AmazonEC2")
        assert [r.amount_usd for r in history] == [1.0, 2.0, 3.0]

    async def test_before_excludes_the_day_under_test(self, session):
        """A point must never be scored against a baseline containing itself."""
        repo = CostRepository(session)
        await seed_days(repo, "AmazonEC2", [1.0, 2.0, 3.0])
        history = await repo.history("AmazonEC2", before=DAY)
        assert [r.amount_usd for r in history] == [1.0, 2.0]

    async def test_limit_keeps_the_newest(self, session):
        repo = CostRepository(session)
        await seed_days(repo, "AmazonEC2", [1.0, 2.0, 3.0, 4.0, 5.0])
        history = await repo.history("AmazonEC2", limit=2)
        assert [r.amount_usd for r in history] == [4.0, 5.0]

    async def test_unknown_service_is_empty(self, session):
        assert await CostRepository(session).history("Nope") == []


class TestCostListing:
    async def test_total_ignores_paging(self, session):
        repo = CostRepository(session)
        await seed_days(repo, "AmazonEC2", [float(i) for i in range(1, 11)])
        rows, total = await repo.list_records(days=3650, limit=3)
        assert len(rows) == 3
        assert total == 10

    async def test_offset_walks_the_result_set(self, session):
        repo = CostRepository(session)
        await seed_days(repo, "AmazonEC2", [float(i) for i in range(1, 6)])
        first, _ = await repo.list_records(days=3650, limit=2, offset=0)
        second, _ = await repo.list_records(days=3650, limit=2, offset=2)
        assert {r.id for r in first}.isdisjoint({r.id for r in second})

    async def test_filters_by_service(self, session):
        repo = CostRepository(session)
        await seed_days(repo, "AmazonEC2", [1.0, 2.0])
        await seed_days(repo, "AWSLambda", [3.0])
        _, total = await repo.list_records(service="AWSLambda", days=3650)
        assert total == 1

    async def test_filters_by_explicit_range(self, session):
        repo = CostRepository(session)
        await seed_days(repo, "AmazonEC2", [1.0, 2.0, 3.0, 4.0, 5.0])
        _, total = await repo.list_records(
            start=DAY - timedelta(days=2), end=DAY - timedelta(days=1)
        )
        assert total == 2

    async def test_explicit_range_overrides_days(self, session):
        repo = CostRepository(session)
        await seed_days(repo, "AmazonEC2", [1.0], end=date(2020, 1, 1))
        _, total = await repo.list_records(days=7, start=date(2019, 1, 1), end=date(2021, 1, 1))
        assert total == 1

    async def test_anomalies_only(self, session):
        repo = CostRepository(session)
        await repo.upsert(service="AmazonEC2", usage_date=DAY, amount=1.0)
        await repo.upsert(
            service="AmazonEC2",
            usage_date=DAY - timedelta(days=1),
            amount=99.0,
            is_anomaly=True,
        )
        _, total = await repo.list_records(days=3650, anomalies_only=True)
        assert total == 1

    async def test_days_window_excludes_older_rows(self, session):
        repo = CostRepository(session)
        await repo.upsert(service="AmazonEC2", usage_date=date(2020, 1, 1), amount=1.0)
        _, total = await repo.list_records(days=30)
        assert total == 0


class TestAggregates:
    async def test_services_are_distinct_and_sorted(self, session):
        repo = CostRepository(session)
        await seed_days(repo, "AmazonEC2", [1.0, 2.0])
        await seed_days(repo, "AWSLambda", [1.0])
        assert await repo.services() == ["AWSLambda", "AmazonEC2"]

    async def test_daily_totals_sum_across_services(self, session):
        repo = CostRepository(session)
        today = date.today()
        await repo.upsert(service="AmazonEC2", usage_date=today, amount=10.0)
        await repo.upsert(service="AWSLambda", usage_date=today, amount=5.0)
        totals = await repo.daily_totals(days=7)
        assert dict(totals)[today] == pytest.approx(15.0)

    async def test_spend_by_service_is_ranked(self, session):
        repo = CostRepository(session)
        today = date.today()
        await repo.upsert(service="Small", usage_date=today, amount=1.0)
        await repo.upsert(service="Large", usage_date=today, amount=100.0)
        ranked = await repo.spend_by_service(days=7)
        assert [name for name, _ in ranked] == ["Large", "Small"]

    async def test_spend_by_service_respects_the_limit(self, session):
        repo = CostRepository(session)
        today = date.today()
        for i in range(5):
            await repo.upsert(service=f"svc-{i}", usage_date=today, amount=float(i + 1))
        assert len(await repo.spend_by_service(days=7, limit=2)) == 2

    async def test_purge_removes_only_old_rows(self, session):
        repo = CostRepository(session)
        await repo.upsert(service="AmazonEC2", usage_date=date(2020, 1, 1), amount=1.0)
        await repo.upsert(service="AmazonEC2", usage_date=date.today(), amount=2.0)
        removed = await repo.purge_before(date(2021, 1, 1))
        assert removed == 1
        _, total = await repo.list_records(days=3650)
        assert total == 1


class TestBaselineRepository:
    def payload(self, **overrides) -> dict:
        return {
            "service": "AmazonEC2",
            "window_size": 30,
            "sample_count": 30,
            "mean": 420.0,
            "stddev": 12.0,
            "median": 418.0,
            "mad": 9.0,
            "p95": 440.0,
            "last_amount": 425.0,
            "last_evaluated_date": DAY,
            **overrides,
        }

    async def test_insert_then_fetch(self, session):
        repo = BaselineRepository(session)
        await repo.upsert(**self.payload())
        baseline = await repo.get("AmazonEC2")
        assert baseline is not None
        assert baseline.mean == 420.0

    async def test_upsert_replaces(self, session):
        repo = BaselineRepository(session)
        await repo.upsert(**self.payload())
        await repo.upsert(**self.payload(mean=500.0, sample_count=25))
        baseline = await repo.get("AmazonEC2")
        assert baseline.mean == 500.0
        assert baseline.sample_count == 25

    async def test_missing_service_is_none(self, session):
        assert await BaselineRepository(session).get("Nope") is None

    async def test_list_is_ordered_by_spend(self, session):
        repo = BaselineRepository(session)
        await repo.upsert(**self.payload(service="Small", mean=1.0))
        await repo.upsert(**self.payload(service="Large", mean=900.0))
        assert [b.service for b in await repo.list_all()] == ["Large", "Small"]


class TestAlertRepository:
    async def make_alert(self, session, **overrides) -> Alert:
        record = await CostRepository(session).upsert(
            service=overrides.get("service", "AmazonEC2"),
            usage_date=overrides.get("usage_date", DAY),
            amount=overrides.get("amount", 900.0),
        )
        alert = Alert(
            cost_record_id=record.id,
            service=record.service,
            usage_date=record.usage_date,
            amount=record.amount,
            zscore=overrides.get("zscore", 7.0),
            baseline_mean=180.0,
            baseline_stddev=8.0,
            severity="warning",
            status=overrides.get("status", AlertStatus.PENDING),
            message="spike",
            next_retry_at=overrides.get("next_retry_at"),
        )
        return await AlertRepository(session).create(alert)

    async def test_create_assigns_an_id(self, session):
        alert = await self.make_alert(session)
        assert alert.id

    async def test_get_round_trips(self, session):
        alert = await self.make_alert(session)
        assert (await AlertRepository(session).get(alert.id)).id == alert.id

    async def test_missing_alert_is_none(self, session):
        assert await AlertRepository(session).get("nope") is None

    async def test_filters_by_status(self, session):
        await self.make_alert(session, status=AlertStatus.DELIVERED)
        await self.make_alert(session, usage_date=DAY - timedelta(days=1))
        _, total = await AlertRepository(session).list_alerts(status=AlertStatus.DELIVERED)
        assert total == 1

    async def test_delivered_shorthand_is_the_inverse_of_itself(self, session):
        await self.make_alert(session, status=AlertStatus.DELIVERED)
        await self.make_alert(session, usage_date=DAY - timedelta(days=1))
        repo = AlertRepository(session)
        _, delivered = await repo.list_alerts(delivered=True)
        _, undelivered = await repo.list_alerts(delivered=False)
        assert (delivered, undelivered) == (1, 1)

    async def test_filters_by_service(self, session):
        await self.make_alert(session, service="AmazonEC2")
        await self.make_alert(session, service="AWSLambda")
        _, total = await AlertRepository(session).list_alerts(service="AWSLambda")
        assert total == 1

    async def test_counts_by_status(self, session):
        await self.make_alert(session, status=AlertStatus.DELIVERED)
        await self.make_alert(
            session, usage_date=DAY - timedelta(days=1), status=AlertStatus.FAILED
        )
        counts = await AlertRepository(session).counts_by_status()
        assert counts == {"delivered": 1, "failed": 1}

    async def test_last_alert_skips_suppressed(self, session):
        """The cooldown must not be reset by an alert it suppressed itself."""
        await self.make_alert(session, status=AlertStatus.DELIVERED)
        await self.make_alert(
            session, usage_date=DAY - timedelta(days=1), status=AlertStatus.SUPPRESSED
        )
        latest = await AlertRepository(session).last_alert_for_service("AmazonEC2")
        assert latest.status == AlertStatus.DELIVERED

    async def test_due_for_retry_respects_the_schedule(self, session):
        now = datetime.now(UTC)
        await self.make_alert(
            session, status=AlertStatus.FAILED, next_retry_at=now - timedelta(minutes=1)
        )
        await self.make_alert(
            session,
            usage_date=DAY - timedelta(days=1),
            status=AlertStatus.FAILED,
            next_retry_at=now + timedelta(hours=1),
        )
        due = await AlertRepository(session).due_for_retry(now=now)
        assert len(due) == 1

    async def test_abandoned_alerts_are_not_swept(self, session):
        """next_retry_at of NULL means given up on."""
        await self.make_alert(session, status=AlertStatus.FAILED, next_retry_at=None)
        assert await AlertRepository(session).due_for_retry() == []

    async def test_delivered_alerts_are_not_swept(self, session):
        await self.make_alert(
            session,
            status=AlertStatus.DELIVERED,
            next_retry_at=datetime.now(UTC) - timedelta(hours=1),
        )
        assert await AlertRepository(session).due_for_retry() == []

    async def test_attempts_are_recorded(self, session):
        alert = await self.make_alert(session)
        await AlertRepository(session).record_attempts(
            [
                AlertAttempt(
                    alert_id=alert.id,
                    channel="webhook",
                    attempt_number=1,
                    succeeded=False,
                    status_code=503,
                    error="unavailable",
                ),
                AlertAttempt(
                    alert_id=alert.id,
                    channel="webhook",
                    attempt_number=2,
                    succeeded=True,
                    status_code=200,
                ),
            ]
        )
        await session.refresh(alert, ["delivery_attempts"])
        assert len(alert.delivery_attempts) == 2
        assert [a.succeeded for a in alert.delivery_attempts] == [False, True]
