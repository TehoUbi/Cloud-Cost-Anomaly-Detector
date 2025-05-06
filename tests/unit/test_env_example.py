"""`.env.example` is the only configuration reference most people will read.

These guard against the usual rot: a setting added to Settings and never
documented, or a variable left in the template after the code stopped reading
it. Both are the kind of thing that costs someone an afternoon.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from src.config import Settings

ENV_EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / ".env.example"

# Consumed by boto3 directly rather than through Settings, and deliberately
# documented anyway — they are the first thing anyone configures.
BOTO3_ONLY = {"aws_access_key_id", "aws_secret_access_key"}

# Settings that exist but are not worth an operator's attention.
INTENTIONALLY_UNDOCUMENTED = {"api_title", "api_version"}


def documented_keys() -> set[str]:
    return {
        match.group(1).lower()
        for line in ENV_EXAMPLE.read_text().splitlines()
        if (match := re.match(r"^([A-Z0-9_]+)=", line))
    }


def test_env_example_exists():
    assert ENV_EXAMPLE.is_file()


def test_every_documented_key_is_real():
    """A variable in the template that nothing reads is a trap."""
    unknown = documented_keys() - set(Settings.model_fields) - BOTO3_ONLY
    assert not unknown, f"documented but not a setting: {sorted(unknown)}"


def test_every_setting_is_documented():
    missing = set(Settings.model_fields) - documented_keys() - INTENTIONALLY_UNDOCUMENTED
    assert not missing, f"settings missing from .env.example: {sorted(missing)}"


def test_no_real_secrets_are_committed():
    text = ENV_EXAMPLE.read_text()
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "SMTP_PASSWORD"):
        line = next(ln for ln in text.splitlines() if ln.startswith(f"{key}="))
        assert line == f"{key}=", f"{key} must ship empty, got {line!r}"


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("ZSCORE_THRESHOLD", "3.0"),
        ("WINDOW_SIZE", "30"),
        ("MIN_HISTORY_DAYS", "14"),
        ("ROBUST_ZSCORE", "true"),
        ("ALERT_ON_DROPS", "false"),
        ("ALERT_COOLDOWN_HOURS", "24"),
    ],
)
def test_documented_defaults_match_the_code(key, expected):
    """A template that disagrees with the code is worse than no template."""
    text = ENV_EXAMPLE.read_text()
    line = next(ln for ln in text.splitlines() if ln.startswith(f"{key}="))
    assert line.split("=", 1)[1] == expected

    field = Settings.model_fields[key.lower()]
    actual = field.default
    if isinstance(actual, bool):
        assert str(actual).lower() == expected
    else:
        assert float(actual) == float(expected)


def test_lookback_exceeds_the_window():
    """A poll must fetch more days than the baseline window, or a restated
    recent day gets scored against a window that is still filling."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.poll_lookback_days > settings.window_size
