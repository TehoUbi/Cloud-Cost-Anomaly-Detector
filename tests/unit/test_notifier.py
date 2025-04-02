"""The send_alert facade: multi-channel fan-out and the retry ladder."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from src.config import Settings
from src.detector.zscore import AnomalyResult, Direction, Severity
from src.models.cost import AlertChannel
from src.notifier.alert import send_alert, send_payload
from src.notifier.channels import (
    ChannelResponse,
    PermanentDeliveryError,
    TransientDeliveryError,
)
from src.notifier.payload import build_payload

WEBHOOK_URL = "https://hooks.example.com/sentinel"


def settings(**overrides) -> Settings:
    base = {
        "alert_webhook_url": WEBHOOK_URL,
        "alert_base_delay_seconds": 0.001,
        "alert_max_delay_seconds": 0.002,
        "alert_backoff_jitter": 0.0,
        "alert_max_retries": 4,
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[call-arg]


def anomaly() -> AnomalyResult:
    return AnomalyResult(
        service="AmazonEC2",
        amount=2110.1,
        zscore=23.17,
        baseline_mean=420.5,
        baseline_stddev=18.2,
        is_anomaly=True,
        direction=Direction.SPIKE,
        severity=Severity.CRITICAL,
        reason="spike",
    )


class FakeChannel:
    """Channel double that replays a scripted sequence of outcomes."""

    def __init__(self, name: AlertChannel, outcomes: list):
        self.name = name
        self.outcomes = list(outcomes)
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return True

    async def deliver(self, payload):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else ChannelResponse(succeeded=True)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class TestSendAlertOverHttp:
    @respx.mock
    async def test_delivers_on_the_first_attempt(self):
        route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))
        assert await send_alert(anomaly(), usage_date=date(2026, 10, 10), settings=settings())
        assert route.call_count == 1

    @respx.mock
    async def test_retries_a_transient_failure_then_succeeds(self):
        route = respx.post(WEBHOOK_URL).mock(
            side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(200)]
        )
        assert await send_alert(anomaly(), settings=settings())
        assert route.call_count == 3

    @respx.mock
    async def test_exhausts_the_budget_and_reports_failure(self):
        route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(503))
        assert not await send_alert(anomaly(), settings=settings())
        assert route.call_count == 4

    @respx.mock
    async def test_does_not_retry_a_permanent_failure(self):
        route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(400))
        assert not await send_alert(anomaly(), settings=settings())
        assert route.call_count == 1

    @respx.mock
    async def test_retries_a_timeout(self):
        route = respx.post(WEBHOOK_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        assert not await send_alert(anomaly(), settings=settings())
        assert route.call_count == 4

    @respx.mock
    async def test_max_retries_is_honoured(self):
        route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(503))
        await send_alert(anomaly(), settings=settings(alert_max_retries=2))
        assert route.call_count == 2


class TestNoChannels:
    async def test_returns_false_when_nothing_is_configured(self):
        assert not await send_alert(anomaly(), settings=Settings(_env_file=None))  # type: ignore[call-arg]

    async def test_empty_channel_list_is_a_failure_not_a_success(self):
        """Reporting success here would make a misconfiguration invisible."""
        assert not await send_payload(build_payload(anomaly()), settings=settings(), channels=[])


class TestMultiChannelFanOut:
    async def test_every_channel_receives_the_alert(self):
        webhook = FakeChannel(AlertChannel.WEBHOOK, [])
        email = FakeChannel(AlertChannel.EMAIL, [])
        assert await send_payload(
            build_payload(anomaly()), settings=settings(), channels=[webhook, email]
        )
        assert webhook.calls == 1
        assert email.calls == 1

    async def test_one_failing_channel_does_not_stop_the_other(self):
        """A dead webhook must not swallow the email."""
        webhook = FakeChannel(AlertChannel.WEBHOOK, [PermanentDeliveryError("gone")])
        email = FakeChannel(AlertChannel.EMAIL, [])
        delivered = await send_payload(
            build_payload(anomaly()), settings=settings(), channels=[webhook, email]
        )
        assert not delivered, "partial delivery is not success"
        assert email.calls == 1

    async def test_partial_delivery_reports_false(self):
        webhook = FakeChannel(AlertChannel.WEBHOOK, [])
        email = FakeChannel(AlertChannel.EMAIL, [TransientDeliveryError("smtp down")] * 8)
        assert not await send_payload(
            build_payload(anomaly()), settings=settings(), channels=[webhook, email]
        )

    async def test_each_channel_gets_its_own_retry_budget(self):
        webhook = FakeChannel(AlertChannel.WEBHOOK, [TransientDeliveryError("x")] * 8)
        email = FakeChannel(AlertChannel.EMAIL, [TransientDeliveryError("y")] * 8)
        await send_payload(
            build_payload(anomaly()),
            settings=settings(alert_max_retries=3),
            channels=[webhook, email],
        )
        assert webhook.calls == 3
        assert email.calls == 3

    async def test_closure_binding_sends_to_the_right_channel(self):
        """The retry wrapper captures the channel per iteration; a late-binding
        closure here would send every alert to the last channel in the list."""
        first = FakeChannel(AlertChannel.WEBHOOK, [])
        second = FakeChannel(AlertChannel.EMAIL, [])
        await send_payload(build_payload(anomaly()), settings=settings(), channels=[first, second])
        assert first.calls == 1 and second.calls == 1


class TestPayloadDelivered:
    @respx.mock
    async def test_webhook_body_carries_the_anomaly(self):
        route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))
        await send_alert(anomaly(), usage_date=date(2026, 10, 10), settings=settings())
        body = route.calls[0].request.read().decode()
        assert "AmazonEC2" in body
        assert "2026-10-10" in body
        assert "23.17" in body

    @respx.mock
    async def test_usage_date_defaults_to_today(self):
        respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))
        await send_alert(anomaly(), settings=settings())

    @pytest.mark.parametrize("severity", [Severity.INFO, Severity.WARNING, Severity.CRITICAL])
    @respx.mock
    async def test_severity_is_transmitted(self, severity):
        route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))
        result = AnomalyResult(
            service="S3",
            amount=1.0,
            zscore=3.1,
            baseline_mean=0.5,
            baseline_stddev=0.1,
            is_anomaly=True,
            severity=severity,
        )
        await send_alert(result, settings=settings())
        assert severity.value in route.calls[0].request.read().decode()
