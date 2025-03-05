"""The alert body, rendered once and shared by every channel."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import Any

from src.detector.zscore import AnomalyResult, Severity

SEVERITY_PREFIX = {
    Severity.CRITICAL: "CRITICAL",
    Severity.WARNING: "WARNING",
    Severity.INFO: "INFO",
}


@dataclass(frozen=True, slots=True)
class AlertPayload:
    """Serializable alert. Field names are part of the webhook contract."""

    service: str
    usage_date: str
    amount: float
    zscore: float
    baseline_mean: float
    baseline_stddev: float
    delta: float
    delta_pct: float
    direction: str
    severity: str
    detector: str
    reason: str
    message: str
    alert_id: str | None = None
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def subject(self) -> str:
        return (
            f"[Sentinel {SEVERITY_PREFIX.get(Severity(self.severity), 'ALERT')}] "
            f"{self.service} cost {self.direction} "
            f"${self.amount:,.2f} on {self.usage_date}"
        )


def build_payload(
    result: AnomalyResult,
    *,
    usage_date: date | None = None,
    alert_id: str | None = None,
    message: str | None = None,
) -> AlertPayload:
    """Render an :class:`AnomalyResult` into a deliverable payload."""
    return AlertPayload(
        service=result.service,
        usage_date=(usage_date or date.today()).isoformat(),
        amount=round(result.amount, 2),
        zscore=round(result.zscore, 2),
        baseline_mean=round(result.baseline_mean, 2),
        baseline_stddev=round(result.baseline_stddev, 2),
        delta=round(result.delta, 2),
        delta_pct=round(result.delta_pct, 1),
        direction=result.direction.value,
        severity=result.severity.value,
        detector=result.detector,
        reason=result.reason,
        message=message
        or (
            f"Cost {result.direction.value} detected for {result.service}: "
            f"${result.amount:,.2f} (z={result.zscore:+.2f}, "
            f"baseline ${result.baseline_mean:,.2f})"
        ),
        alert_id=alert_id,
        generated_at=datetime.now(UTC).isoformat(),
    )


def render_text(payload: AlertPayload) -> str:
    """Plain-text email body — also the fallback part of the MIME message."""
    return "\n".join(
        [
            payload.message,
            "",
            f"Service:          {payload.service}",
            f"Usage date:       {payload.usage_date}",
            f"Amount:           ${payload.amount:,.2f}",
            f"Baseline:         ${payload.baseline_mean:,.2f} "
            f"(sigma ${payload.baseline_stddev:,.2f})",
            f"Change:           ${payload.delta:+,.2f} ({payload.delta_pct:+.1f}%)",
            f"Z-score:          {payload.zscore:+.2f}",
            f"Severity:         {payload.severity}",
            f"Detector:         {payload.detector}",
            "",
            f"Why: {payload.reason}",
            "",
            f"Alert ID: {payload.alert_id or 'n/a'}",
            f"Generated: {payload.generated_at}",
        ]
    )


def render_html(payload: AlertPayload) -> str:
    """HTML body. Table layout and inline styles, because that is what email
    clients actually render."""
    accent = {"critical": "#b42318", "warning": "#b54708", "info": "#175cd3"}.get(
        payload.severity, "#175cd3"
    )
    rows = [
        ("Service", payload.service),
        ("Usage date", payload.usage_date),
        ("Amount", f"${payload.amount:,.2f}"),
        ("Baseline", f"${payload.baseline_mean:,.2f} (&sigma; ${payload.baseline_stddev:,.2f})"),
        ("Change", f"${payload.delta:+,.2f} ({payload.delta_pct:+.1f}%)"),
        ("Z-score", f"{payload.zscore:+.2f}"),
        ("Detector", payload.detector),
    ]
    body = "".join(
        f'<tr><td style="padding:6px 14px 6px 0;color:#475467;font-size:13px">{label}</td>'
        f'<td style="padding:6px 0;color:#101828;font-size:13px;font-weight:600">{value}</td></tr>'
        for label, value in rows
    )
    return f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#f9fafb;
  font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
  <table role="presentation" cellpadding="0" cellspacing="0" width="100%"
    style="max-width:560px;margin:0 auto;background:#ffffff;border:1px solid #eaecf0;
    border-radius:10px;overflow:hidden">
    <tr><td style="background:{accent};padding:14px 20px;color:#ffffff;
      font-size:13px;font-weight:700;letter-spacing:.04em;text-transform:uppercase">
      Sentinel &middot; {payload.severity} &middot; cost {payload.direction}
    </td></tr>
    <tr><td style="padding:20px">
      <p style="margin:0 0 16px;color:#101828;font-size:15px;line-height:1.5">
        {payload.message}</p>
      <table role="presentation" cellpadding="0" cellspacing="0">{body}</table>
      <p style="margin:16px 0 0;color:#475467;font-size:13px;line-height:1.5">
        <strong>Why:</strong> {payload.reason}</p>
    </td></tr>
    <tr><td style="padding:12px 20px;background:#f9fafb;border-top:1px solid #eaecf0;
      color:#667085;font-size:11px">
      Alert {payload.alert_id or "n/a"} &middot; generated {payload.generated_at}
    </td></tr>
  </table>
</body></html>"""
