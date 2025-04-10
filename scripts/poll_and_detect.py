"""Scheduled poll as a long-running process.

Three ways to run the same cycle:

* this script, for a container or systemd unit that owns its own schedule
* ``infra/lambda_handler.py``, for EventBridge
* ``POST /costs/poll``, for a manual run

All three call :func:`src.pipeline.run_poll_cycle`, so they cannot drift apart.

    python -m scripts.poll_and_detect              # loop on POLL_INTERVAL_HOURS
    python -m scripts.poll_and_detect --once       # one cycle and exit
    python -m scripts.poll_and_detect --backfill 180
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys

from src.api.main import configure_logging
from src.config import get_settings
from src.db.session import dispose_engine, init_db, session_scope
from src.pipeline import backfill, run_poll_cycle

logger = logging.getLogger("sentinel.poller")

_shutdown = asyncio.Event()


def _request_shutdown() -> None:
    logger.info("Shutdown requested; finishing the current cycle")
    _shutdown.set()


async def run_once(*, lookback_days: int | None = None, deliver: bool = True) -> int:
    settings = get_settings()
    async with session_scope() as session:
        result = await run_poll_cycle(
            session, settings=settings, lookback_days=lookback_days, deliver=deliver
        )
    logger.info("Cycle complete: %s", result.as_dict())
    return result.detection.alerts_created


async def run_backfill(days: int, chunk_days: int = 60) -> None:
    settings = get_settings()
    async with session_scope() as session:
        result = await backfill(session, days=days, settings=settings, chunk_days=chunk_days)
    logger.info("Backfill complete: %s", result.as_dict())


async def run_forever() -> None:
    settings = get_settings()
    interval = settings.poll_interval_hours * 3600
    logger.info("Polling every %.1fh", settings.poll_interval_hours)

    while not _shutdown.is_set():
        try:
            await run_once()
        except Exception:
            # A poll failure must not kill the loop; the next cycle re-reads
            # the same window, so one lost run costs nothing.
            logger.exception("Poll cycle failed; continuing")

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_shutdown.wait(), timeout=interval)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sentinel cost poller")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument(
        "--backfill", type=int, metavar="DAYS", help="seed history without alerting"
    )
    parser.add_argument("--chunk-days", type=int, default=60, help="backfill request size")
    parser.add_argument("--lookback-days", type=int, help="override POLL_LOOKBACK_DAYS")
    parser.add_argument(
        "--no-deliver", action="store_true", help="score and store without sending alerts"
    )
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="create tables from metadata instead of running Alembic (local use only)",
    )
    args = parser.parse_args(argv)

    configure_logging(get_settings())

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_shutdown)

    try:
        if args.init_db:
            await init_db()

        if args.backfill:
            await run_backfill(args.backfill, chunk_days=args.chunk_days)
        elif args.once:
            await run_once(lookback_days=args.lookback_days, deliver=not args.no_deliver)
        else:
            await run_forever()
    finally:
        await dispose_engine()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
