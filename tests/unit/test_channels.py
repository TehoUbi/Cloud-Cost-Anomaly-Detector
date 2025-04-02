"""Delivery channels: one attempt each, and correct failure classification.

Getting transient-vs-permanent wrong is expensive in both directions — a
retried 400 wastes the whole ladder, and a non-retried 503 drops the alert.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx
from botocore.exceptions import ClientError

from src.config import Settings
from src.detector.zscore import AnomalyResult, Direction, Severity
from src.models.cost import AlertChannel
from src.notifier.channels import (
    RETRYABLE_STATUS,
    EmailChannel,
    PermanentDeliveryError,
    TransientDeliveryError,
    WebhookChannel,
    build_channels,
)
from src.notifier.payload import build_payload

WEBHOOK_URL = "https://hooks.example.com/sentinel"


@pytest.fixture
def payload():
    return build_payload(
        AnomalyResult(
            service="AWS Lambda",
            amount=162.37,
            zscore=18.27,
            baseline_mean=35.77,
            baseline_stddev=6.93,
            is_anomaly=True,
            direction=Direction.SPIKE,
            severity=Severity.CRITICAL,
            reason="spike of $126.61",
        ),
        usage_date=date(2026, 9, 22),
        alert_id="alert-1",
    )


def webhook_settings(**overrides) -> Settings:
    base = {"alert_webhook_url": WEBHOOK_URL}
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[call-arg]


def email_settings(**overrides) -> Settings:
    base = {
        "alert_email_from": "sentinel@example.com",
        "alert_email_to": ["oncall@example.com", "finops@example.com"],
        "alert_email_transport": "ses",
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[call-arg]


class TestWebhookEnablement:
    def test_disabled_without_a_url(self):
        assert not WebhookChannel(Settings(_env_file=None)).enabled  # type: ignore[call-arg]

    def test_enabled_with_a_url(self):
        assert WebhookChannel(webhook_settings()).enabled

    def test_channel_name(self):
        assert WebhookChannel(webhook_settings()).name is AlertChannel.WEBHOOK


class TestWebhookDelivery:
    @respx.mock
    async def test_posts_the_payload_as_json(self, payload):
        route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))
        channel = WebhookChannel(webhook_settings())
        response = await channel.deliver(payload)
        await channel.aclose()

        assert response.succeeded
        assert route.call_count == 1
        body = route.calls[0].request.read()
        assert b'"service":"AWS Lambda"' in body.replace(b", ", b",").replace(b": ", b":")

    @respx.mock
    async def test_sends_a_user_agent(self, payload):
        route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))
        channel = WebhookChannel(webhook_settings())
        await channel.deliver(payload)
        await channel.aclose()
        assert "sentinel" in route.calls[0].request.headers["user-agent"]

    @respx.mock
    @pytest.mark.parametrize("code", [200, 201, 202, 204])
    async def test_any_2xx_is_success(self, payload, code):
        respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(code))
        channel = WebhookChannel(webhook_settings())
        assert (await channel.deliver(payload)).succeeded
        await channel.aclose()

    @respx.mock
    async def test_records_the_status_code_and_duration(self, payload):
        respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(202))
        channel = WebhookChannel(webhook_settings())
        response = await channel.deliver(payload)
        await channel.aclose()
        assert response.status_code == 202
        assert response.duration_ms >= 0


class TestWebhookFailureClassification:
    @respx.mock
    @pytest.mark.parametrize("code", sorted(RETRYABLE_STATUS))
    async def test_retryable_statuses_raise_transient(self, payload, code):
        respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(code))
        channel = WebhookChannel(webhook_settings())
        with pytest.raises(TransientDeliveryError) as caught:
            await channel.deliver(payload)
        await channel.aclose()
        assert caught.value.status_code == code

    @respx.mock
    @pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 422])
    async def test_other_4xx_raise_permanent(self, payload, code):
        """These fail identically every time; retrying only delays the error."""
        respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(code))
        channel = WebhookChannel(webhook_settings())
        with pytest.raises(PermanentDeliveryError) as caught:
            await channel.deliver(payload)
        await channel.aclose()
        assert caught.value.status_code == code

    @respx.mock
    async def test_timeout_is_transient(self, payload):
        respx.post(WEBHOOK_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
        channel = WebhookChannel(webhook_settings())
        with pytest.raises(TransientDeliveryError, match="ConnectTimeout"):
            await channel.deliver(payload)
        await channel.aclose()

    @respx.mock
    async def test_connection_error_is_transient(self, payload):
        respx.post(WEBHOOK_URL).mock(side_effect=httpx.ConnectError("refused"))
        channel = WebhookChannel(webhook_settings())
        with pytest.raises(TransientDeliveryError):
            await channel.deliver(payload)
        await channel.aclose()

    @respx.mock
    async def test_error_detail_is_truncated(self, payload):
        respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(400, text="x" * 5000))
        channel = WebhookChannel(webhook_settings())
        with pytest.raises(PermanentDeliveryError) as caught:
            await channel.deliver(payload)
        await channel.aclose()
        assert len(str(caught.value)) < 400


class SesStub:
    def __init__(self, error: BaseException | None = None):
        self.error = error
        self.calls: list[dict] = []

    def send_raw_email(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"MessageId": "msg-1"}


def ses_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "nope"}}, "SendRawEmail")


class TestEmailEnablement:
    def test_disabled_by_default(self):
        assert not EmailChannel(Settings(_env_file=None)).enabled  # type: ignore[call-arg]

    def test_disabled_without_recipients(self):
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None, alert_email_from="a@b.com", alert_email_transport="ses"
        )
        assert not EmailChannel(settings).enabled

    def test_disabled_without_a_sender(self):
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None, alert_email_to=["a@b.com"], alert_email_transport="ses"
        )
        assert not EmailChannel(settings).enabled

    def test_enabled_when_fully_configured(self):
        assert EmailChannel(email_settings()).enabled

    def test_channel_name(self):
        assert EmailChannel(email_settings()).name is AlertChannel.EMAIL


class TestEmailMessage:
    def test_is_multipart_with_text_and_html(self):
        """A text/plain fallback is what makes the alert readable in a client
        that will not render HTML."""
        message = EmailChannel(email_settings()).build_message(
            build_payload(
                AnomalyResult(
                    service="AmazonRDS",
                    amount=900.0,
                    zscore=7.0,
                    baseline_mean=180.0,
                    baseline_stddev=8.0,
                    is_anomaly=True,
                    direction=Direction.SPIKE,
                    severity=Severity.WARNING,
                )
            )
        )
        subtypes = {
            part.get_content_subtype() for part in message.walk() if not part.is_multipart()
        }
        assert subtypes == {"plain", "html"}

    def test_addresses_every_recipient(self, payload):
        message = EmailChannel(email_settings()).build_message(payload)
        assert message["To"] == "oncall@example.com, finops@example.com"
        assert message["From"] == "sentinel@example.com"

    def test_subject_matches_the_payload(self, payload):
        message = EmailChannel(email_settings()).build_message(payload)
        assert message["Subject"] == payload.subject

    def test_carries_tracing_headers(self, payload):
        """So a mail client can thread one service's alerts together."""
        message = EmailChannel(email_settings()).build_message(payload)
        assert message["X-Sentinel-Service"] == "AWS Lambda"
        assert message["X-Sentinel-Severity"] == "critical"
        assert message["X-Sentinel-Alert-Id"] == "alert-1"


class TestEmailSes:
    async def test_sends_raw_email_to_every_destination(self, payload):
        stub = SesStub()
        channel = EmailChannel(email_settings(), ses_client=stub)
        response = await channel.deliver(payload)
        assert response.succeeded
        assert stub.calls[0]["Destinations"] == ["oncall@example.com", "finops@example.com"]
        assert stub.calls[0]["Source"] == "sentinel@example.com"
        assert b"Subject:" in stub.calls[0]["RawMessage"]["Data"]

    @pytest.mark.parametrize(
        "code",
        ["MessageRejected", "MailFromDomainNotVerifiedException", "AccountSendingPausedException"],
    )
    async def test_configuration_errors_are_permanent(self, payload, code):
        """An unverified sender will not verify itself five seconds later."""
        channel = EmailChannel(email_settings(), ses_client=SesStub(ses_error(code)))
        with pytest.raises(PermanentDeliveryError):
            await channel.deliver(payload)

    @pytest.mark.parametrize("code", ["Throttling", "ServiceUnavailable", "InternalFailure"])
    async def test_server_side_errors_are_transient(self, payload, code):
        channel = EmailChannel(email_settings(), ses_client=SesStub(ses_error(code)))
        with pytest.raises(TransientDeliveryError):
            await channel.deliver(payload)

    async def test_unexpected_errors_are_transient(self, payload):
        channel = EmailChannel(email_settings(), ses_client=SesStub(RuntimeError("socket gone")))
        with pytest.raises(TransientDeliveryError):
            await channel.deliver(payload)


class TestBuildChannels:
    def test_none_configured(self):
        assert build_channels(Settings(_env_file=None)) == []  # type: ignore[call-arg]

    def test_webhook_only(self):
        channels = build_channels(webhook_settings())
        assert [channel.name for channel in channels] == [AlertChannel.WEBHOOK]

    def test_email_only(self):
        channels = build_channels(email_settings())
        assert [channel.name for channel in channels] == [AlertChannel.EMAIL]

    def test_both(self):
        settings = email_settings(alert_webhook_url=WEBHOOK_URL)
        assert {channel.name for channel in build_channels(settings)} == {
            AlertChannel.WEBHOOK,
            AlertChannel.EMAIL,
        }


class TestEmailSmtp:
    """The self-hosting path: aiosmtplib instead of SES."""

    def smtp_settings(self, **overrides) -> Settings:
        return email_settings(
            alert_email_transport="smtp",
            smtp_host="mail.example.com",
            smtp_port=587,
            **overrides,
        )

    async def test_sends_via_aiosmtplib(self, payload, monkeypatch):
        captured: dict = {}

        async def fake_send(message, **kwargs):
            captured["message"] = message
            captured["kwargs"] = kwargs

        monkeypatch.setattr("aiosmtplib.send", fake_send)
        channel = EmailChannel(self.smtp_settings())
        assert (await channel.deliver(payload)).succeeded
        assert captured["kwargs"]["hostname"] == "mail.example.com"
        assert captured["kwargs"]["port"] == 587
        assert captured["message"]["Subject"] == payload.subject

    async def test_blank_credentials_are_sent_as_none(self, payload, monkeypatch):
        """aiosmtplib treats an empty username as an auth attempt and fails."""
        captured: dict = {}

        async def fake_send(message, **kwargs):
            captured.update(kwargs)

        monkeypatch.setattr("aiosmtplib.send", fake_send)
        await EmailChannel(self.smtp_settings()).deliver(payload)
        assert captured["username"] is None
        assert captured["password"] is None

    async def test_credentials_are_passed_when_set(self, payload, monkeypatch):
        captured: dict = {}

        async def fake_send(message, **kwargs):
            captured.update(kwargs)

        monkeypatch.setattr("aiosmtplib.send", fake_send)
        settings = self.smtp_settings(smtp_username="relay", smtp_password="secret")
        await EmailChannel(settings).deliver(payload)
        assert captured["username"] == "relay"
        assert captured["password"] == "secret"

    async def test_refused_recipients_are_permanent(self, payload, monkeypatch):
        import aiosmtplib

        async def fake_send(message, **kwargs):
            raise aiosmtplib.SMTPRecipientsRefused([])

        monkeypatch.setattr("aiosmtplib.send", fake_send)
        with pytest.raises(PermanentDeliveryError, match="recipients refused"):
            await EmailChannel(self.smtp_settings()).deliver(payload)

    async def test_bad_credentials_are_permanent(self, payload, monkeypatch):
        """Retrying a rejected password five times just locks the account."""
        import aiosmtplib

        async def fake_send(message, **kwargs):
            raise aiosmtplib.SMTPAuthenticationError(535, "bad credentials")

        monkeypatch.setattr("aiosmtplib.send", fake_send)
        with pytest.raises(PermanentDeliveryError, match="auth rejected"):
            await EmailChannel(self.smtp_settings()).deliver(payload)

    async def test_connection_trouble_is_transient(self, payload, monkeypatch):
        import aiosmtplib

        async def fake_send(message, **kwargs):
            raise aiosmtplib.SMTPConnectError("connection refused")

        monkeypatch.setattr("aiosmtplib.send", fake_send)
        with pytest.raises(TransientDeliveryError):
            await EmailChannel(self.smtp_settings()).deliver(payload)

    async def test_tls_setting_is_honoured(self, payload, monkeypatch):
        captured: dict = {}

        async def fake_send(message, **kwargs):
            captured.update(kwargs)

        monkeypatch.setattr("aiosmtplib.send", fake_send)
        await EmailChannel(self.smtp_settings(smtp_use_tls=False)).deliver(payload)
        assert captured["start_tls"] is False
