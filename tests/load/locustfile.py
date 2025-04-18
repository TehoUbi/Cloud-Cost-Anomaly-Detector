"""Locust load profile for the Sentinel API.

Run:
    locust -f tests/load/locustfile.py --host http://localhost:8000

Headless, as CI and the recorded run do it:
    locust -f tests/load/locustfile.py --host http://localhost:8000 \
        --headless --users 5000 --spawn-rate 250 --run-time 2m \
        --processes -1 --html reports/load.html --csv reports/load

Notes on the shape of this profile:

* ``FastHttpUser`` (geventhttpclient), not ``HttpUser`` (requests). Above a
  few hundred users per process the requests-based client spends more time in
  its own stack than the server does serving, and you end up load-testing
  Locust. This client holds a few thousand users per worker instead.
* Weights are read-heavy because that is the real traffic: dashboards polling
  cost history and alert state, with writes arriving from EventBridge a few
  times a day. ``/costs/poll`` is deliberately excluded — it walks 45 days of
  history for every service, so including it in a throughput number would
  measure the detector, not the API.
* ``catch_response`` is used to mark an expected 404 as a success. A test that
  reports a correct 404 as a failure makes the failure rate meaningless.
"""

from __future__ import annotations

import random

from locust import FastHttpUser, between, events, task
from locust.runners import WorkerRunner

# Mirrors SYNTHETIC_SERVICES in src/utils/cost_explorer.py.
SERVICES = [
    "Amazon Elastic Compute Cloud - Compute",
    "Amazon Relational Database Service",
    "Amazon Simple Storage Service",
    "AWS Lambda",
    "Amazon CloudFront",
    "AWS Key Management Service",
    "Amazon CloudWatch",
    "Amazon DynamoDB",
]

WINDOWS = [7, 14, 30, 60, 90]


class DashboardUser(FastHttpUser):
    """A cost dashboard: summary, history, drill-downs, alert list."""

    wait_time = between(0.1, 0.5)
    # Below the server's own timeout, so a slow response is recorded as slow
    # rather than as a client-side failure.
    network_timeout = 30.0
    connection_timeout = 10.0

    @task(20)
    def health(self):
        self.client.get("/health", name="/health")

    @task(15)
    def summary(self):
        days = random.choice(WINDOWS)
        self.client.get(f"/costs/summary?days={days}", name="/costs/summary")

    @task(15)
    def list_costs(self):
        days = random.choice(WINDOWS)
        self.client.get(f"/costs/?days={days}&limit=100", name="/costs/ [page]")

    @task(10)
    def list_costs_for_service(self):
        service = random.choice(SERVICES)
        self.client.get(
            f"/costs/?service={service}&days=30&limit=50", name="/costs/ [by service]"
        )

    @task(8)
    def anomalies(self):
        self.client.get("/costs/?days=90&anomalies_only=true", name="/costs/ [anomalies]")

    @task(8)
    def service_history(self):
        service = random.choice(SERVICES)
        with self.client.get(
            f"/costs/{service}/history?limit=90",
            name="/costs/{service}/history",
            catch_response=True,
        ) as response:
            # 404 is correct for a service with no stored history yet.
            if response.status_code in (200, 404):
                response.success()

    @task(8)
    def baseline(self):
        service = random.choice(SERVICES)
        with self.client.get(
            f"/costs/{service}/baseline", name="/costs/{service}/baseline", catch_response=True
        ) as response:
            if response.status_code in (200, 404):
                response.success()

    @task(6)
    def list_alerts(self):
        self.client.get("/alerts/?limit=50", name="/alerts/")

    @task(4)
    def list_alerts_filtered(self):
        status = random.choice(["pending", "delivered", "failed", "suppressed"])
        self.client.get(f"/alerts/?status={status}&limit=50", name="/alerts/ [by status]")

    @task(3)
    def services(self):
        self.client.get("/costs/services", name="/costs/services")

    @task(2)
    def baselines(self):
        self.client.get("/costs/baselines?limit=50", name="/costs/baselines")

    @task(1)
    def readiness(self):
        """Touches the database on every call, unlike /health."""
        self.client.get("/ready", name="/ready")


class HealthOnlyUser(FastHttpUser):
    """Pure framework overhead, with no database in the path.

    Useful as a ceiling: if /health and /costs/summary report similar
    latencies, the bottleneck is ASGI, not the queries.
    """

    wait_time = between(0.05, 0.2)

    @task
    def health(self):
        self.client.get("/health", name="/health [isolated]")


@events.quitting.add_listener
def _assert_thresholds(environment, **_kwargs):
    """Fail the run on a bad result so CI does not go green on a red test.

    Only the master judges. A worker's local stats are a fraction of the run,
    and in distributed mode every worker fires this event too — which printed
    four "PASS: 0 requests" lines ahead of the real verdict.
    """
    if isinstance(environment.runner, WorkerRunner):
        return

    stats = environment.stats.total
    failure_ratio = stats.fail_ratio
    p95 = stats.get_response_time_percentile(0.95)

    if failure_ratio > 0.01:
        environment.process_exit_code = 1
        print(f"FAIL: failure ratio {failure_ratio:.2%} exceeds 1%")
    elif p95 and p95 > 2000:
        environment.process_exit_code = 1
        print(f"FAIL: p95 {p95:.0f}ms exceeds 2000ms")
    else:
        environment.process_exit_code = 0
        print(f"PASS: {stats.num_requests} requests, {failure_ratio:.2%} failures, p95 {p95:.0f}ms")
