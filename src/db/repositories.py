"""Data access for cost records, baselines and alerts.

The services and routes talk to these classes rather than writing queries
inline, which keeps the dialect quirks (upsert syntax, in particular) in one
place and lets the test suite run the same code against SQLite.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import new_id
from src.models.cost import Alert, AlertAttempt, AlertStatus, CostRecord, ServiceBaseline


def _upsert_stmt(session: AsyncSession, table: Any, values: dict[str, Any]) -> Any:
    """Build an INSERT ... ON CONFLICT for whichever dialect is attached.

    Postgres and SQLite expose the same ``on_conflict_do_update`` API through
    different modules, and the core ``insert()`` exposes neither.
    """
    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
    insert = sqlite_insert if dialect == "sqlite" else pg_insert
    return insert(table).values(**values)


class CostRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(
        self,
        *,
        service: str,
        usage_date: date,
        amount: Decimal | float,
        unit: str = "USD",
        zscore: float | None = None,
        baseline_mean: float | None = None,
        baseline_stddev: float | None = None,
        is_anomaly: bool = False,
        detector: str | None = None,
    ) -> CostRecord:
        """Insert a service-day, or refresh it if the poll has seen it before.

        Cost Explorer restates the last few days as usage settles, so a re-poll
        must overwrite rather than duplicate.
        """
        values = {
            "id": new_id(),
            "service": service,
            "usage_date": usage_date,
            "amount": Decimal(str(amount)),
            "unit": unit,
            "zscore": zscore,
            "baseline_mean": baseline_mean,
            "baseline_stddev": baseline_stddev,
            "is_anomaly": is_anomaly,
            "detector": detector,
        }
        stmt = (
            _upsert_stmt(self.session, CostRecord, values)
            .on_conflict_do_update(
                index_elements=[CostRecord.service, CostRecord.usage_date],
                set_={
                    "amount": values["amount"],
                    "unit": unit,
                    "zscore": zscore,
                    "baseline_mean": baseline_mean,
                    "baseline_stddev": baseline_stddev,
                    "is_anomaly": is_anomaly,
                    "detector": detector,
                    "updated_at": datetime.now(UTC),
                },
            )
            .returning(CostRecord)
        )
        # populate_existing: without it the ORM hands back the stale instance
        # already in the identity map instead of the row RETURNING just gave us.
        result = await self.session.execute(stmt, execution_options={"populate_existing": True})
        await self.session.flush()
        return result.scalar_one()

    async def history(
        self, service: str, *, before: date | None = None, limit: int = 90
    ) -> list[CostRecord]:
        """Most recent ``limit`` days for a service, oldest first.

        ``before`` excludes the day under evaluation so a point is never scored
        against a baseline that already contains it.
        """
        stmt = select(CostRecord).where(CostRecord.service == service)
        if before is not None:
            stmt = stmt.where(CostRecord.usage_date < before)
        stmt = stmt.order_by(CostRecord.usage_date.desc()).limit(limit)
        rows = (await self.session.execute(stmt)).scalars().all()
        return list(reversed(rows))

    async def list_records(
        self,
        *,
        service: str | None = None,
        days: int | None = 30,
        start: date | None = None,
        end: date | None = None,
        anomalies_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[CostRecord], int]:
        """Filtered page of cost records plus the total matching count."""
        filters = self._build_filters(
            service=service, days=days, start=start, end=end, anomalies_only=anomalies_only
        )
        total = (
            await self.session.execute(select(func.count(CostRecord.id)).where(*filters))
        ).scalar_one()
        stmt = (
            select(CostRecord)
            .where(*filters)
            .order_by(CostRecord.usage_date.desc(), CostRecord.service)
            .limit(limit)
            .offset(offset)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return list(rows), int(total)

    @staticmethod
    def _build_filters(
        *,
        service: str | None,
        days: int | None,
        start: date | None,
        end: date | None,
        anomalies_only: bool,
    ) -> list[Any]:
        """Shared WHERE clauses so the page and its total never disagree."""
        filters: list[Any] = []
        if service:
            filters.append(CostRecord.service == service)
        if start is not None:
            filters.append(CostRecord.usage_date >= start)
        if end is not None:
            filters.append(CostRecord.usage_date <= end)
        if start is None and end is None and days is not None:
            filters.append(CostRecord.usage_date >= date.today() - timedelta(days=days))
        if anomalies_only:
            filters.append(CostRecord.is_anomaly.is_(True))
        return filters

    async def services(self) -> list[str]:
        stmt = select(CostRecord.service).distinct().order_by(CostRecord.service)
        return list((await self.session.execute(stmt)).scalars().all())

    async def daily_totals(self, *, days: int = 30) -> list[tuple[date, float]]:
        """Total spend per day across every service — the summary endpoint's series."""
        cutoff = date.today() - timedelta(days=days)
        stmt = (
            select(CostRecord.usage_date, func.sum(CostRecord.amount))
            .where(CostRecord.usage_date >= cutoff)
            .group_by(CostRecord.usage_date)
            .order_by(CostRecord.usage_date)
        )
        rows = await self.session.execute(stmt)
        return [(row[0], float(row[1] or 0)) for row in rows]

    async def spend_by_service(self, *, days: int = 30, limit: int = 10) -> list[tuple[str, float]]:
        cutoff = date.today() - timedelta(days=days)
        stmt = (
            select(CostRecord.service, func.sum(CostRecord.amount).label("total"))
            .where(CostRecord.usage_date >= cutoff)
            .group_by(CostRecord.service)
            .order_by(func.sum(CostRecord.amount).desc())
            .limit(limit)
        )
        rows = await self.session.execute(stmt)
        return [(row[0], float(row[1] or 0)) for row in rows]

    async def purge_before(self, cutoff: date) -> int:
        """Drop history older than ``cutoff``. Used by the retention task."""
        result = await self.session.execute(
            delete(CostRecord).where(CostRecord.usage_date < cutoff)
        )
        # execute() is typed as returning Result, which has no rowcount; a DML
        # statement always yields a CursorResult, which does.
        return int(cast("CursorResult[Any]", result).rowcount or 0)


class BaselineRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(
        self,
        *,
        service: str,
        window_size: int,
        sample_count: int,
        mean: float,
        stddev: float,
        median: float,
        mad: float,
        p95: float,
        last_amount: float,
        last_evaluated_date: date,
    ) -> None:
        values = {
            "service": service,
            "window_size": window_size,
            "sample_count": sample_count,
            "mean": mean,
            "stddev": stddev,
            "median": median,
            "mad": mad,
            "p95": p95,
            "last_amount": last_amount,
            "last_evaluated_date": last_evaluated_date,
        }
        stmt = _upsert_stmt(self.session, ServiceBaseline, values).on_conflict_do_update(
            index_elements=[ServiceBaseline.service],
            set_={k: v for k, v in values.items() if k != "service"}
            | {"updated_at": datetime.now(UTC)},
        )
        await self.session.execute(stmt)

    async def get(self, service: str) -> ServiceBaseline | None:
        return await self.session.get(ServiceBaseline, service)

    async def list_all(self, *, limit: int = 200, offset: int = 0) -> list[ServiceBaseline]:
        stmt = (
            select(ServiceBaseline)
            .order_by(ServiceBaseline.mean.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class AlertRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, alert: Alert) -> Alert:
        self.session.add(alert)
        await self.session.flush()
        return alert

    async def get(self, alert_id: str) -> Alert | None:
        return await self.session.get(Alert, alert_id)

    async def list_alerts(
        self,
        *,
        status: str | None = None,
        service: str | None = None,
        delivered: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Alert], int]:
        filters = []
        if status:
            filters.append(Alert.status == status)
        if service:
            filters.append(Alert.service == service)
        if delivered is True:
            filters.append(Alert.status == AlertStatus.DELIVERED)
        elif delivered is False:
            filters.append(Alert.status != AlertStatus.DELIVERED)

        total = (
            await self.session.execute(select(func.count(Alert.id)).where(*filters))
        ).scalar_one()
        stmt = (
            select(Alert)
            .where(*filters)
            .order_by(Alert.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return list(rows), int(total)

    async def last_alert_for_service(self, service: str) -> Alert | None:
        """Most recent non-suppressed alert — the cooldown check reads this."""
        stmt = (
            select(Alert)
            .where(Alert.service == service, Alert.status != AlertStatus.SUPPRESSED)
            .order_by(Alert.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def due_for_retry(self, *, now: datetime | None = None, limit: int = 50) -> list[Alert]:
        """Alerts whose in-process retries ran out but whose backoff has elapsed.

        The sweeper picks these up on the next poll, so a webhook outage longer
        than one alert's retry budget still gets delivered afterwards.
        """
        now = now or datetime.now(UTC)
        stmt = (
            select(Alert)
            .where(
                Alert.status.in_([AlertStatus.FAILED, AlertStatus.PARTIAL, AlertStatus.PENDING]),
                Alert.next_retry_at.is_not(None),
                Alert.next_retry_at <= now,
            )
            .order_by(Alert.next_retry_at)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def record_attempts(self, attempts: Sequence[AlertAttempt]) -> None:
        self.session.add_all(list(attempts))
        await self.session.flush()

    async def counts_by_status(self) -> dict[str, int]:
        stmt = select(Alert.status, func.count(Alert.id)).group_by(Alert.status)
        rows = await self.session.execute(stmt)
        return {row[0]: int(row[1]) for row in rows}
