"""Shared fixtures.

The suite runs against SQLite by default so it needs no services. Point
``TEST_DATABASE_URL`` at a Postgres instance to run the same tests against the
real target — CI does exactly that, which is what catches the places where
SQLite is more forgiving than Postgres (NUMERIC handling, ON CONFLICT
RETURNING, timezone-aware timestamps).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def env_defaults(tmp_path: object) -> dict[str, str]:
    """Deterministic configuration: no jitter, no sleeping, no live AWS."""
    database_url = TEST_DATABASE_URL or f"sqlite+aiosqlite:///{tmp_path}/sentinel-test.db"
    return {
        "DATABASE_URL": database_url,
        "COST_PROVIDER": "synthetic",
        "LOG_LEVEL": "WARNING",
        "ALERT_WEBHOOK_URL": "",
        "ALERT_EMAIL_TRANSPORT": "disabled",
        "ALERT_EMAIL_FROM": "",
        "ALERT_EMAIL_TO": "",
        "ALERT_BASE_DELAY_SECONDS": "0.001",
        "ALERT_MAX_DELAY_SECONDS": "0.01",
        "ALERT_BACKOFF_JITTER": "0",
        "ALERT_MAX_RETRIES": "3",
        "ALERT_COOLDOWN_HOURS": "0",
        "ZSCORE_THRESHOLD": "3.0",
        "WINDOW_SIZE": "30",
        "MIN_HISTORY_DAYS": "14",
        "MIN_ABSOLUTE_DELTA_USD": "1.0",
        "ENSEMBLE_ENABLED": "false",
        "AWS_DEFAULT_REGION": "us-east-1",
    }


@pytest.fixture
def configure_env(monkeypatch: pytest.MonkeyPatch, env_defaults: dict[str, str]) -> Iterator[None]:
    """Install the test environment and reset the cached settings around it.

    get_settings is lru_cached, so a test that changes the environment without
    clearing it would read the previous test's configuration.
    """
    from src.config import Settings, get_settings

    for key, value in env_defaults.items():
        monkeypatch.setenv(key, value)
    # A developer's local .env would otherwise leak into the suite. Only
    # env_file is overridden; the rest of model_config still applies.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings(configure_env: None):
    from src.config import get_settings

    return get_settings()


@pytest_asyncio.fixture
async def engine(settings) -> AsyncIterator[object]:
    """A fresh schema per test, created through Alembic's metadata."""
    from src.db.base import Base
    from src.db.session import dispose_engine, get_engine
    from src.models import cost  # noqa: F401

    await dispose_engine()
    eng = get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await dispose_engine()


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[object]:
    from src.db.session import get_session_factory

    async with get_session_factory()() as sess:
        yield sess
        await sess.rollback()


@pytest_asyncio.fixture
async def client(engine, settings) -> AsyncIterator[AsyncClient]:
    """ASGI client with the lifespan already run by the engine fixture."""
    from src.api.main import create_app

    app = create_app(settings)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sentinel.test"
    ) as http:
        yield http


# --- data builders -------------------------------------------------------


@pytest.fixture
def today() -> date:
    return date(2026, 10, 1)


def make_costs(
    service: str,
    amounts: list[float],
    *,
    end: date,
    unit: str = "USD",
) -> list:
    """Build ServiceCost rows ending on ``end``, one per day, oldest first."""
    from src.utils.cost_explorer import ServiceCost

    start = end - timedelta(days=len(amounts) - 1)
    return [
        ServiceCost(
            service=service,
            usage_date=start + timedelta(days=offset),
            amount=Decimal(str(amount)),
            unit=unit,
        )
        for offset, amount in enumerate(amounts)
    ]


@pytest.fixture
def cost_factory():
    return make_costs


@pytest.fixture
def steady_history() -> list[float]:
    """Thirty days of believable EC2 spend with no anomaly in it."""
    import numpy as np

    rng = np.random.default_rng(20261001)
    return [round(float(value), 2) for value in rng.normal(420.0, 12.0, 30)]
