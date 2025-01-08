import pytest
from unittest.mock import AsyncMock, patch
from src.notifier.alert import send_alert, build_payload
from src.detector.zscore import AnomalyResult


def make_anomaly() -> AnomalyResult:
    return AnomalyResult(
        service="AmazonEC2",
        amount=450.0,
        zscore=4.2,
        baseline_mean=10.0,
        baseline_stddev=2.0,
        is_anomaly=True,
    )


class TestBuildPayload:
    def test_message_contains_service_name(self):
        result = make_anomaly()
        payload = build_payload(result)
        assert result.service in payload.message

    def test_zscore_is_rounded(self):
        result = make_anomaly()
        payload = build_payload(result)
        assert payload.zscore == round(result.zscore, 2)


class TestSendAlert:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_webhook_url(self):
        with patch.dict("os.environ", {"ALERT_WEBHOOK_URL": ""}):
            success = await send_alert(make_anomaly())
        assert success is False

    @pytest.mark.asyncio
    async def test_returns_true_on_successful_delivery(self):
        import httpx
        mock_response = AsyncMock()
        mock_response.raise_for_status = lambda: None

        with patch.dict("os.environ", {"ALERT_WEBHOOK_URL": "http://fake-webhook"}):
            with patch("httpx.AsyncClient.post", return_value=mock_response):
                success = await send_alert(make_anomaly())
        assert success is True
