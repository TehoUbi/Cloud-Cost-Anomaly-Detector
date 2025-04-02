"""Alert rendering. The webhook field names are a public contract."""

from __future__ import annotations

import json
from datetime import date

import pytest

from src.detector.zscore import AnomalyResult, Direction, Severity
from src.notifier.payload import build_payload, render_html, render_text


def anomaly(**overrides) -> AnomalyResult:
    defaults = {
        "service": "Amazon Elastic Compute Cloud - Compute",
        "amount": 2110.1,
        "zscore": 23.1749,
        "baseline_mean": 420.5,
        "baseline_stddev": 18.2,
        "is_anomaly": True,
        "direction": Direction.SPIKE,
        "severity": Severity.CRITICAL,
        "reason": "spike of $1,689.60 against a $420.50 baseline",
    }
    return AnomalyResult(**{**defaults, **overrides})


class TestBuildPayload:
    def test_contract_fields_are_present(self):
        payload = build_payload(anomaly(), usage_date=date(2026, 10, 10))
        assert set(payload.to_dict()) == {
            "service",
            "usage_date",
            "amount",
            "zscore",
            "baseline_mean",
            "baseline_stddev",
            "delta",
            "delta_pct",
            "direction",
            "severity",
            "detector",
            "reason",
            "message",
            "alert_id",
            "generated_at",
        }

    def test_payload_is_json_serializable(self):
        payload = build_payload(anomaly(), alert_id="abc")
        assert json.loads(json.dumps(payload.to_dict()))["service"].startswith("Amazon")

    def test_numbers_are_rounded_for_display(self):
        payload = build_payload(anomaly())
        assert payload.zscore == 23.17
        assert payload.amount == 2110.1

    def test_delta_and_percentage_are_computed(self):
        payload = build_payload(anomaly(amount=200.0, baseline_mean=100.0))
        assert payload.delta == 100.0
        assert payload.delta_pct == 100.0

    def test_zero_baseline_does_not_divide_by_zero(self):
        payload = build_payload(anomaly(amount=5.0, baseline_mean=0.0))
        assert payload.delta_pct == 0.0

    def test_usage_date_is_isoformat(self):
        payload = build_payload(anomaly(), usage_date=date(2026, 2, 3))
        assert payload.usage_date == "2026-02-03"

    def test_alert_id_is_carried_through(self):
        assert build_payload(anomaly(), alert_id="xyz-1").alert_id == "xyz-1"

    def test_message_can_be_overridden(self):
        payload = build_payload(anomaly(), message="custom text")
        assert payload.message == "custom text"

    def test_generated_at_is_set(self):
        assert build_payload(anomaly()).generated_at


class TestSubject:
    def test_includes_severity_service_and_amount(self):
        subject = build_payload(anomaly(), usage_date=date(2026, 10, 10)).subject
        assert "CRITICAL" in subject
        assert "Amazon Elastic Compute Cloud - Compute" in subject
        assert "2,110.10" in subject
        assert "2026-10-10" in subject

    @pytest.mark.parametrize(
        ("severity", "expected"),
        [(Severity.CRITICAL, "CRITICAL"), (Severity.WARNING, "WARNING"), (Severity.INFO, "INFO")],
    )
    def test_severity_prefix(self, severity, expected):
        assert expected in build_payload(anomaly(severity=severity)).subject

    def test_drop_reads_as_a_drop(self):
        payload = build_payload(anomaly(direction=Direction.DROP, amount=1.0))
        assert "drop" in payload.subject


class TestRenderText:
    def test_contains_the_key_figures(self):
        text = render_text(build_payload(anomaly(), usage_date=date(2026, 10, 10)))
        for fragment in ("$2,110.10", "$420.50", "+23.17", "critical", "2026-10-10"):
            assert fragment in text

    def test_includes_the_reason(self):
        text = render_text(build_payload(anomaly(reason="because of X")))
        assert "because of X" in text

    def test_handles_a_missing_alert_id(self):
        assert "n/a" in render_text(build_payload(anomaly()))


class TestRenderHtml:
    def test_is_a_complete_document(self):
        html = render_html(build_payload(anomaly()))
        assert html.startswith("<!doctype html>")
        assert html.rstrip().endswith("</html>")

    def test_uses_inline_styles_and_tables(self):
        """Mail clients strip <style> blocks and ignore flexbox."""
        html = render_html(build_payload(anomaly()))
        assert "style=" in html
        assert "<table" in html
        assert "display:flex" not in html

    @pytest.mark.parametrize(
        ("severity", "colour"),
        [(Severity.CRITICAL, "#b42318"), (Severity.WARNING, "#b54708"), (Severity.INFO, "#175cd3")],
    )
    def test_accent_tracks_severity(self, severity, colour):
        assert colour in render_html(build_payload(anomaly(severity=severity)))

    def test_contains_the_figures(self):
        html = render_html(build_payload(anomaly(), usage_date=date(2026, 10, 10)))
        assert "$2,110.10" in html
        assert "+23.17" in html
