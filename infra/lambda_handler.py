"""
infra/lambda_handler.py

AWS Lambda entrypoint. Triggered by EventBridge on a schedule.
Runs one poll-and-detect cycle and exits.

Deploy as a container image Lambda using the provided Dockerfile.
"""

import asyncio
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    """Lambda handler — synchronous wrapper around the async poll cycle."""
    from scripts.poll_and_detect import run_once, init_db

    async def _run():
        await init_db()
        await run_once()

    asyncio.run(_run())
    return {"statusCode": 200, "body": "Poll complete"}
