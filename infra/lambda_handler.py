"""AWS Lambda entrypoint for the scheduled poll.

EventBridge invokes this on a cron. One cycle runs and the container freezes
until the next invocation, which shapes two decisions:

* The engine is built lazily and the pool uses ``pool_recycle``, because a
  frozen container's sockets may have been dropped by NAT while it slept.
* Heavy imports happen at module scope so they land in the init phase, which
  Lambda does not bill and does not count against the handler timeout.

The event may carry overrides::

    {"lookback_days": 90, "deliver": false}   # one-off backfill-style run
    {"action": "sweep"}                       # flush the retry backlog only
    {"action": "backfill", "days": 180}       # seed history, no alerts
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from src.api.main import configure_logging
from src.config import get_settings
from src.db.session import dispose_engine, session_scope
from src.notifier.dispatcher import AlertDispatcher
from src.pipeline import backfill, run_poll_cycle

if os.getenv("SENTINEL_WARM_IMPORTS", "1") == "1":
    # Paid for once during init rather than on the first invocation. numpy and
    # scikit-learn together are a few seconds of import time.
    import numpy  # noqa: F401
    import sklearn.ensemble  # noqa: F401

configure_logging(get_settings())
logger = logging.getLogger(__name__)


async def _run(event: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    action = (event or {}).get("action", "poll")

    async with session_scope() as session:
        if action == "sweep":
            dispatcher = AlertDispatcher(session, settings)
            try:
                outcomes = await dispatcher.sweep(limit=int(event.get("limit", 100)))
            finally:
                await dispatcher.aclose()
            return {
                "action": "sweep",
                "swept": len(outcomes),
                "delivered": sum(1 for outcome in outcomes if outcome.delivered),
            }

        if action == "backfill":
            result = await backfill(
                session,
                days=int(event.get("days", 180)),
                settings=settings,
                chunk_days=int(event.get("chunk_days", 60)),
            )
            return {"action": "backfill", **result.as_dict()}

        result = await run_poll_cycle(
            session,
            settings=settings,
            lookback_days=event.get("lookback_days"),
            deliver=bool(event.get("deliver", True)),
        )
        return {"action": "poll", **result.as_dict()}


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    """Synchronous wrapper — Lambda's contract is a blocking callable."""
    event = event or {}
    logger.info("Invoked with event: %s", json.dumps(event, default=str)[:500])

    try:
        body = asyncio.run(_run(event))
        status = 200
    except Exception as exc:
        # Logged with a traceback and re-raised as a payload rather than an
        # exception: a raise marks the invocation failed and EventBridge retries
        # it, which for a poll cycle just doubles the Cost Explorer spend.
        logger.exception("Poll cycle failed")
        body = {"error": type(exc).__name__, "detail": str(exc)}
        status = 500
    finally:
        # The pool must not survive the freeze; see the module docstring.
        asyncio.run(dispose_engine())

    logger.info("Result: %s", json.dumps(body, default=str)[:1000])
    return {"statusCode": status, "body": json.dumps(body, default=str)}


if __name__ == "__main__":
    print(json.dumps(handler({}), indent=2))
