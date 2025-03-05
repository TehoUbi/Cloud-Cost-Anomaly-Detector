"""Delivery channels: HTTP webhook, and email over SES or SMTP.

Each channel does exactly one attempt and reports what happened. Retrying is
the dispatcher's job, so the backoff policy is identical everywhere and a
channel cannot quietly invent its own.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Protocol

import httpx

from src.config import Settings, get_settings
from src.models.cost import AlertChannel
from src.notifier.payload import AlertPayload, render_html, render_text

logger = logging.getLogger(__name__)

# 408 and 429 are worth another try; so is anything 5xx. Every other 4xx is the
# payload's fault and will fail identically five times in a row.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 509})


@dataclass(frozen=True, slots=True)
class ChannelResponse:
    succeeded: bool
    status_code: int | None = None
    error: str | None = None
    duration_ms: float = 0.0
    retryable: bool = True
    """False marks a permanent failure, which stops the dispatcher retrying."""


class PermanentDeliveryError(Exception):
    """Raised for a failure that retrying cannot fix."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TransientDeliveryError(Exception):
    """Raised for a failure that is worth another attempt."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class NotificationChannel(Protocol):
    name: AlertChannel

    @property
    def enabled(self) -> bool: ...

    async def deliver(self, payload: AlertPayload) -> ChannelResponse: ...


class WebhookChannel:
    """POSTs the alert as JSON to ``ALERT_WEBHOOK_URL``."""

    name = AlertChannel.WEBHOOK

    def __init__(
        self, settings: Settings | None = None, client: httpx.AsyncClient | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None

    @property
    def enabled(self) -> bool:
        return self.settings.webhook_enabled

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.settings.alert_webhook_timeout_seconds,
                headers={"user-agent": "sentinel-cost-anomaly/1.0"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def deliver(self, payload: AlertPayload) -> ChannelResponse:
        client = await self._get_client()
        started = time.perf_counter()
        try:
            response = await client.post(self.settings.alert_webhook_url, json=payload.to_dict())
        except httpx.HTTPError as exc:
            elapsed = (time.perf_counter() - started) * 1000
            # A timeout or connection reset says nothing about the payload.
            raise TransientDeliveryError(f"{type(exc).__name__}: {exc}") from exc

        elapsed = (time.perf_counter() - started) * 1000
        if response.is_success:
            return ChannelResponse(
                succeeded=True, status_code=response.status_code, duration_ms=elapsed
            )

        detail = f"HTTP {response.status_code}: {response.text[:200]}"
        if response.status_code in RETRYABLE_STATUS:
            raise TransientDeliveryError(detail, status_code=response.status_code)
        raise PermanentDeliveryError(detail, status_code=response.status_code)


class EmailChannel:
    """Sends the alert as a multipart email via SES or SMTP.

    SES is the deployed path — the Lambda already has an execution role, so it
    needs no SMTP credentials in config. SMTP exists for self-hosting and for
    pointing at a local catcher like MailHog during development.
    """

    name = AlertChannel.EMAIL

    def __init__(self, settings: Settings | None = None, ses_client: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._ses = ses_client

    @property
    def enabled(self) -> bool:
        return self.settings.email_enabled

    def build_message(self, payload: AlertPayload) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = payload.subject
        message["From"] = self.settings.alert_email_from
        message["To"] = ", ".join(self.settings.alert_email_to)
        # Lets a mail client thread every alert for one service together.
        message["X-Sentinel-Service"] = payload.service
        message["X-Sentinel-Severity"] = payload.severity
        if payload.alert_id:
            message["X-Sentinel-Alert-Id"] = payload.alert_id
        message.set_content(render_text(payload))
        message.add_alternative(render_html(payload), subtype="html")
        return message

    async def deliver(self, payload: AlertPayload) -> ChannelResponse:
        message = self.build_message(payload)
        started = time.perf_counter()
        try:
            if self.settings.alert_email_transport == "ses":
                await self._send_via_ses(message)
            else:
                await self._send_via_smtp(message)
        except (PermanentDeliveryError, TransientDeliveryError):
            raise
        except Exception as exc:
            raise TransientDeliveryError(f"{type(exc).__name__}: {exc}") from exc
        return ChannelResponse(succeeded=True, duration_ms=(time.perf_counter() - started) * 1000)

    @property
    def ses(self) -> Any:
        if self._ses is None:
            import boto3

            self._ses = boto3.client("ses", region_name=self.settings.aws_default_region)
        return self._ses

    async def _send_via_ses(self, message: EmailMessage) -> None:
        # boto3 is synchronous; off the event loop so a slow SES call does not
        # stall the API's other requests.
        def send() -> dict[str, Any]:
            return self.ses.send_raw_email(
                Source=self.settings.alert_email_from,
                Destinations=list(self.settings.alert_email_to),
                RawMessage={"Data": message.as_bytes()},
            )

        try:
            await asyncio.to_thread(send)
        except Exception as exc:
            if self._is_permanent_ses_error(exc):
                raise PermanentDeliveryError(f"SES rejected the message: {exc}") from exc
            raise TransientDeliveryError(f"SES send failed: {exc}") from exc

    @staticmethod
    def _is_permanent_ses_error(exc: BaseException) -> bool:
        """An unverified sender or a blocked address will not fix itself."""
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            return False
        return response.get("Error", {}).get("Code") in {
            "MessageRejected",
            "MailFromDomainNotVerifiedException",
            "AccountSendingPausedException",
            "ConfigurationSetDoesNotExistException",
        }

    async def _send_via_smtp(self, message: EmailMessage) -> None:
        import aiosmtplib

        try:
            await aiosmtplib.send(
                message,
                hostname=self.settings.smtp_host,
                port=self.settings.smtp_port,
                username=self.settings.smtp_username or None,
                password=self.settings.smtp_password or None,
                start_tls=self.settings.smtp_use_tls,
                timeout=15.0,
            )
        except aiosmtplib.SMTPRecipientsRefused as exc:
            raise PermanentDeliveryError(f"all recipients refused: {exc}") from exc
        except aiosmtplib.SMTPAuthenticationError as exc:
            raise PermanentDeliveryError(f"SMTP auth rejected: {exc}") from exc
        except aiosmtplib.SMTPException as exc:
            raise TransientDeliveryError(f"SMTP send failed: {exc}") from exc


def build_channels(settings: Settings | None = None) -> list[NotificationChannel]:
    """Every configured channel. An alert goes to all of them."""
    settings = settings or get_settings()
    channels: list[NotificationChannel] = []
    webhook = WebhookChannel(settings)
    if webhook.enabled:
        channels.append(webhook)
    email = EmailChannel(settings)
    if email.enabled:
        channels.append(email)
    if not channels:
        logger.warning(
            "No alert channels configured; anomalies will be recorded but not delivered. "
            "Set ALERT_WEBHOOK_URL and/or ALERT_EMAIL_TRANSPORT."
        )
    return channels
