"""HTTP surface, exercised through the ASGI app against a real database."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx

from src.db.repositories import AlertRepository
from src.detector.service import DetectionService
from src.models.cost import AlertStatus
from tests.conftest import make_costs

pytestmark = pytest.mark.integration

# Yesterday: the last day Cost Explorer reports as complete. Relative to
# today so the suite does not fall outside the API's look-back window as
# the calendar moves on.
END = date.today() - timedelta(days=1)
SERVICE = "Amazon Elastic Compute Cloud - Compute"
WEBHOOK_URL = "https://hooks.example.com/sentinel"


def steady(n: int = 30, value: float = 420.0) -> list[float]:
    return [value + (i % 5) - 2 for i in range(n)]


async def seed(session, settings, *, spike: bool = False, end: date = END):
    amounts = [*steady(29), 5000.0] if spike else steady(30)
    costs = make_costs(SERVICE, amounts, end=end)
    summary = await DetectionService(session, settings).run(costs)
    await session.commit()
    return summary


class TestHealth:
    async def test_health_is_ok(self, client):
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_health_reports_the_version(self, client, settings):
        assert (await client.get("/health")).json()["version"] == settings.api_version

    async def test_readiness_checks_the_database(self, client):
        response = await client.get("/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["database"] == "ok"

    async def test_readiness_reports_configuration(self, client):
        body = (await client.get("/ready")).json()
        assert body["cost_provider"] == "synthetic"
        assert body["alert_channels"] == []

    async def test_root_points_at_the_docs(self, client):
        assert (await client.get("/")).json()["docs"] == "/docs"


class TestRequestContext:
    async def test_a_request_id_is_returned(self, client):
        assert (await client.get("/health")).headers["x-request-id"]

    async def test_a_supplied_request_id_is_echoed(self, client):
        """So a caller's trace id survives into our logs."""
        response = await client.get("/health", headers={"x-request-id": "trace-abc"})
        assert response.headers["x-request-id"] == "trace-abc"

    async def test_response_time_is_reported(self, client):
        assert float((await client.get("/health")).headers["x-response-time-ms"]) >= 0


class TestListCosts:
    async def test_empty_database_returns_an_empty_page(self, client):
        body = (await client.get("/costs/?days=365")).json()
        assert body == {"items": [], "total": 0, "limit": 100, "offset": 0}

    async def test_returns_seeded_rows(self, client, session, settings):
        await seed(session, settings)
        body = (await client.get("/costs/?days=365")).json()
        assert body["total"] == 30
        assert len(body["items"]) == 30

    async def test_row_shape(self, client, session, settings):
        await seed(session, settings)
        row = (await client.get("/costs/?days=365&limit=1")).json()["items"][0]
        assert set(row) == {
            "id",
            "service",
            "usage_date",
            "amount",
            "unit",
            "zscore",
            "baseline_mean",
            "baseline_stddev",
            "is_anomaly",
            "detector",
            "created_at",
        }

    async def test_total_is_independent_of_the_page(self, client, session, settings):
        await seed(session, settings)
        body = (await client.get("/costs/?days=365&limit=5")).json()
        assert body["total"] == 30
        assert len(body["items"]) == 5

    async def test_offset_pages_forward(self, client, session, settings):
        await seed(session, settings)
        first = (await client.get("/costs/?days=365&limit=5&offset=0")).json()["items"]
        second = (await client.get("/costs/?days=365&limit=5&offset=5")).json()["items"]
        assert {row["id"] for row in first}.isdisjoint({row["id"] for row in second})

    async def test_filters_by_service(self, client, session, settings):
        await seed(session, settings)
        assert (await client.get("/costs/?days=365&service=Nope")).json()["total"] == 0
        assert (await client.get(f"/costs/?days=365&service={SERVICE}")).json()["total"] == 30

    async def test_anomalies_only(self, client, session, settings):
        await seed(session, settings, spike=True)
        body = (await client.get("/costs/?days=365&anomalies_only=true")).json()
        assert body["total"] == 1
        assert body["items"][0]["is_anomaly"] is True

    async def test_explicit_date_range(self, client, session, settings):
        await seed(session, settings)
        start, end = END - timedelta(days=4), END - timedelta(days=2)
        body = (await client.get(f"/costs/?start={start}&end={end}")).json()
        assert body["total"] == 3

    async def test_inverted_range_is_rejected(self, client):
        response = await client.get("/costs/?start=2026-10-10&end=2026-01-01")
        assert response.status_code == 422
        assert "start must not be after end" in response.json()["detail"]

    @pytest.mark.parametrize("query", ["limit=0", "limit=100000", "offset=-1", "days=0"])
    async def test_out_of_range_parameters_are_rejected(self, client, query):
        response = await client.get(f"/costs/?{query}")
        assert response.status_code == 422
        assert response.json()["error"] == "validation_error"

    async def test_validation_errors_carry_a_request_id(self, client):
        body = (await client.get("/costs/?limit=0")).json()
        assert body["request_id"]


class TestServicesAndSummary:
    async def test_services_lists_what_was_polled(self, client, session, settings):
        await seed(session, settings)
        assert (await client.get("/costs/services")).json() == [SERVICE]

    async def test_summary_on_an_empty_database(self, client):
        body = (await client.get("/costs/summary")).json()
        assert body["total_spend"] == 0
        assert body["services_tracked"] == 0
        assert body["alerts_by_status"] == {}

    async def test_summary_totals_the_window(self, client, session, settings):
        today = date.today()
        await seed(session, settings, end=today)
        body = (await client.get("/costs/summary?days=90")).json()
        assert body["total_spend"] == pytest.approx(sum(steady(30)), abs=1.0)
        assert body["services_tracked"] == 1
        assert len(body["daily_totals"]) == 30

    async def test_summary_ranks_services(self, client, session, settings):
        today = date.today()
        costs = [
            *make_costs(SERVICE, steady(10), end=today),
            *make_costs("AWS Lambda", steady(10, 34.0), end=today),
        ]
        await DetectionService(session, settings).run(costs)
        await session.commit()
        body = (await client.get("/costs/summary?days=90")).json()
        assert [entry["service"] for entry in body["top_services"]] == [SERVICE, "AWS Lambda"]

    async def test_summary_counts_alerts_by_status(self, client, session, settings):
        await seed(session, settings, spike=True, end=date.today())
        body = (await client.get("/costs/summary?days=90")).json()
        assert body["alerts_by_status"].get("pending") == 1

    async def test_top_is_capped(self, client, session, settings):
        today = date.today()
        costs = []
        for index in range(5):
            costs += make_costs(f"svc-{index}", steady(5, 10.0 * (index + 1)), end=today)
        await DetectionService(session, settings).run(costs)
        await session.commit()
        body = (await client.get("/costs/summary?days=90&top=2")).json()
        assert len(body["top_services"]) == 2


class TestBaselines:
    async def test_baseline_is_served(self, client, session, settings):
        await seed(session, settings)
        body = (await client.get(f"/costs/{SERVICE}/baseline")).json()
        assert body["service"] == SERVICE
        assert body["sample_count"] == 30
        assert body["mean"] == pytest.approx(420.0, abs=2.0)
        assert body["mad"] > 0

    async def test_unknown_service_is_404(self, client):
        response = await client.get("/costs/Nope/baseline")
        assert response.status_code == 404
        assert "may not have been polled" in response.json()["detail"]

    async def test_baselines_are_listed(self, client, session, settings):
        await seed(session, settings)
        body = (await client.get("/costs/baselines")).json()
        assert body["total"] == 1
        assert body["items"][0]["service"] == SERVICE

    async def test_history_is_chronological(self, client, session, settings):
        await seed(session, settings)
        rows = (await client.get(f"/costs/{SERVICE}/history?limit=5")).json()
        dates = [row["usage_date"] for row in rows]
        assert dates == sorted(dates)
        assert len(rows) == 5

    async def test_history_for_an_unknown_service_is_404(self, client):
        assert (await client.get("/costs/Nope/history")).status_code == 404


class TestPoll:
    async def test_poll_runs_a_cycle(self, client):
        response = await client.post("/costs/poll", json={"lookback_days": 45})
        assert response.status_code == 200
        body = response.json()
        assert body["observations_fetched"] > 0
        assert body["detection"]["services_seen"] == 8

    async def test_poll_persists_what_it_found(self, client):
        await client.post("/costs/poll", json={"lookback_days": 45})
        assert (await client.get("/costs/?days=90")).json()["total"] > 0

    async def test_poll_accepts_an_empty_body(self, client):
        assert (await client.post("/costs/poll")).status_code == 200

    async def test_poll_without_delivery_queues_nothing(self, client):
        body = (
            await client.post("/costs/poll", json={"lookback_days": 45, "deliver": False})
        ).json()
        assert body["delivered"] == 0
        alerts = (await client.get("/alerts/?status=pending")).json()
        assert alerts["total"] == 0

    @pytest.mark.parametrize("lookback", [1, 400])
    async def test_lookback_bounds_are_enforced(self, client, lookback):
        response = await client.post("/costs/poll", json={"lookback_days": lookback})
        assert response.status_code == 422

    async def test_async_poll_returns_202(self, client):
        response = await client.post("/costs/poll/async", json={"lookback_days": 20})
        assert response.status_code == 202
        assert response.json()["status"] == "accepted"

    async def test_upstream_failure_is_a_502(self, client, monkeypatch):
        """An AWS outage is not our 500."""
        from src.utils.cost_explorer import CostExplorerError

        def broken(*args, **kwargs):
            raise CostExplorerError("Cost Explorer unavailable after 4 attempts")

        monkeypatch.setattr(
            "src.pipeline.get_cost_provider",
            lambda *a, **k: type("P", (), {"fetch_daily_costs": staticmethod(broken)})(),
        )
        response = await client.post("/costs/poll")
        assert response.status_code == 502
        assert response.json()["error"] == "cost_explorer_unavailable"
        assert response.json()["request_id"]


class TestAlertsApi:
    async def test_empty_listing(self, client):
        assert (await client.get("/alerts/")).json() == {
            "items": [],
            "total": 0,
            "limit": 50,
            "offset": 0,
        }

    async def test_alerts_are_listed(self, client, session, settings):
        await seed(session, settings, spike=True)
        body = (await client.get("/alerts/")).json()
        assert body["total"] == 1
        assert body["items"][0]["service"] == SERVICE
        assert body["items"][0]["status"] == "pending"

    async def test_filter_by_status(self, client, session, settings):
        await seed(session, settings, spike=True)
        assert (await client.get("/alerts/?status=pending")).json()["total"] == 1
        assert (await client.get("/alerts/?status=delivered")).json()["total"] == 0

    async def test_invalid_status_is_rejected(self, client):
        assert (await client.get("/alerts/?status=banana")).status_code == 422

    async def test_filter_by_service(self, client, session, settings):
        await seed(session, settings, spike=True)
        assert (await client.get(f"/alerts/?service={SERVICE}")).json()["total"] == 1
        assert (await client.get("/alerts/?service=Nope")).json()["total"] == 0

    async def test_delivered_shorthand(self, client, session, settings):
        await seed(session, settings, spike=True)
        assert (await client.get("/alerts/?delivered=false")).json()["total"] == 1
        assert (await client.get("/alerts/?delivered=true")).json()["total"] == 0

    async def test_single_alert_includes_its_attempt_log(self, client, session, settings):
        await seed(session, settings, spike=True)
        alert_id = (await client.get("/alerts/")).json()["items"][0]["id"]
        body = (await client.get(f"/alerts/{alert_id}")).json()
        assert body["id"] == alert_id
        assert body["delivery_attempts"] == []

    async def test_unknown_alert_is_404(self, client):
        assert (await client.get("/alerts/does-not-exist")).status_code == 404

    async def test_listing_omits_the_attempt_log(self, client, session, settings):
        """A list of 500 alerts should not fan out into 500 attempt queries."""
        await seed(session, settings, spike=True)
        item = (await client.get("/alerts/")).json()["items"][0]
        assert item["delivery_attempts"] == []


class TestRetryAndSweep:
    async def test_retry_without_channels_leaves_it_pending(self, client, session, settings):
        await seed(session, settings, spike=True)
        alert_id = (await client.get("/alerts/")).json()["items"][0]["id"]
        body = (await client.post(f"/alerts/{alert_id}/retry")).json()
        assert body["status"] == "pending"
        assert body["channels"] == []

    @respx.mock
    async def test_retry_delivers_when_a_channel_exists(
        self, client, session, settings, monkeypatch
    ):
        monkeypatch.setattr(settings, "alert_webhook_url", WEBHOOK_URL)
        route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))
        await seed(session, settings, spike=True)

        alert_id = (await client.get("/alerts/")).json()["items"][0]["id"]
        body = (await client.post(f"/alerts/{alert_id}/retry")).json()
        assert body["status"] == "delivered"
        assert route.call_count == 1

    @respx.mock
    async def test_retrying_a_delivered_alert_is_a_conflict(
        self, client, session, settings, monkeypatch
    ):
        monkeypatch.setattr(settings, "alert_webhook_url", WEBHOOK_URL)
        respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))
        await seed(session, settings, spike=True)
        alert_id = (await client.get("/alerts/")).json()["items"][0]["id"]
        await client.post(f"/alerts/{alert_id}/retry")

        response = await client.post(f"/alerts/{alert_id}/retry")
        assert response.status_code == 409
        assert "already delivered" in response.json()["detail"]

    async def test_retrying_an_unknown_alert_is_404(self, client):
        assert (await client.post("/alerts/nope/retry")).status_code == 404

    async def test_sweep_with_nothing_due_is_empty(self, client):
        assert (await client.post("/alerts/sweep")).json() == []

    @respx.mock
    async def test_sweep_delivers_a_due_alert(self, client, session, settings, monkeypatch):
        monkeypatch.setattr(settings, "alert_webhook_url", WEBHOOK_URL)
        respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))
        await seed(session, settings, spike=True)

        alerts, _ = await AlertRepository(session).list_alerts()
        alerts[0].status = AlertStatus.FAILED
        alerts[0].next_retry_at = datetime.now(UTC) - timedelta(minutes=5)
        await session.commit()

        body = (await client.post("/alerts/sweep")).json()
        assert len(body) == 1
        assert body[0]["status"] == "delivered"


class TestTestAlert:
    async def test_conflict_without_channels(self, client):
        response = await client.post("/alerts/test")
        assert response.status_code == 409
        assert "no alert channels configured" in response.json()["detail"]

    @respx.mock
    async def test_fires_through_the_real_channel(self, client, settings, monkeypatch):
        monkeypatch.setattr(settings, "alert_webhook_url", WEBHOOK_URL)
        route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))
        body = (await client.post("/alerts/test", json={"service": "demo"})).json()
        assert body["delivered"] is True
        assert body["channels"] == ["webhook"]
        assert "demo" in body["subject"]
        assert route.call_count == 1

    @respx.mock
    async def test_nothing_is_persisted(self, client, session, settings, monkeypatch):
        """It is a configuration check, not an anomaly."""
        monkeypatch.setattr(settings, "alert_webhook_url", WEBHOOK_URL)
        respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))
        await client.post("/alerts/test")
        assert (await client.get("/alerts/")).json()["total"] == 0
        assert (await client.get("/costs/?days=365")).json()["total"] == 0

    @respx.mock
    async def test_reports_a_failed_delivery(self, client, settings, monkeypatch):
        monkeypatch.setattr(settings, "alert_webhook_url", WEBHOOK_URL)
        respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(500))
        body = (await client.post("/alerts/test")).json()
        assert body["delivered"] is False


class TestOpenApi:
    async def test_schema_is_served(self, client):
        spec = (await client.get("/openapi.json")).json()
        assert spec["info"]["title"].startswith("Sentinel")

    async def test_every_route_is_documented(self, client):
        paths = (await client.get("/openapi.json")).json()["paths"]
        for expected in (
            "/health",
            "/ready",
            "/costs/",
            "/costs/summary",
            "/costs/services",
            "/costs/poll",
            "/costs/{service}/baseline",
            "/alerts/",
            "/alerts/{alert_id}",
            "/alerts/{alert_id}/retry",
            "/alerts/sweep",
            "/alerts/test",
        ):
            assert expected in paths, expected
