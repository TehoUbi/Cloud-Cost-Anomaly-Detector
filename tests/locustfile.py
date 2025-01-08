"""
tests/locustfile.py

Load test for the Sentinel API.
Run with:
    locust -f tests/locustfile.py --host=http://localhost:8000
"""

from locust import HttpUser, task, between


class SentinelUser(HttpUser):
    wait_time = between(0.1, 0.5)

    @task(5)
    def health_check(self):
        self.client.get("/health")

    @task(3)
    def list_costs(self):
        self.client.get("/costs/")

    @task(2)
    def list_costs_filtered(self):
        self.client.get("/costs/?service=AmazonEC2&days=7")

    @task(1)
    def list_alerts(self):
        self.client.get("/alerts/")

    @task(1)
    def trigger_poll(self):
        self.client.post("/costs/poll")
