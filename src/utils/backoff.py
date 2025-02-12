"""Exponential backoff with full jitter, shared by the AWS client and notifier.

Both call sites need the same policy, and a retry loop is easy to get subtly
wrong — off-by-one on the exponent, unbounded delays, or synchronized retries
from every worker. Keeping it in one tested function fixes that once.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    base_delay: float = 1.0
    max_delay: float = 60.0
    max_attempts: int = 5
    jitter: float = 0.25
    """Fraction of the delay to randomize, in [0, 1].

    0 is deterministic (what the tests use); 1 is AWS's "full jitter". The
    default spreads retries enough that N workers failing on the same webhook
    outage do not come back in lockstep.
    """

    def delay_for(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """Delay in seconds to wait *after* a failed ``attempt`` (1-based)."""
        if attempt < 1:
            raise ValueError("attempt is 1-based")
        raw = self.base_delay * (2 ** (attempt - 1))
        capped = min(raw, self.max_delay)
        if self.jitter <= 0:
            return capped
        spread = capped * self.jitter
        source = rng or random
        return max(0.0, capped - spread + source.uniform(0, 2 * spread))

    def schedule(self) -> list[float]:
        """The delay ladder, for logging and docs. Jitter is not applied."""
        return [
            min(self.base_delay * (2**i), self.max_delay) for i in range(self.max_attempts - 1)
        ]


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: BackoffPolicy,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    give_up_on: tuple[type[BaseException], ...] = (),
    retry_if: Callable[[BaseException], bool] | None = None,
    on_failure: Callable[[int, BaseException, float], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run ``operation`` until it succeeds or the policy is exhausted.

    ``give_up_on`` takes precedence over ``retry_on``, and ``retry_if`` filters
    further on the instance, so a permanent failure (a 400 from a malformed
    payload, an AccessDenied) is raised immediately instead of being retried
    five times with growing delays. Re-raises the last exception once attempts
    run out.

    ``sleep`` is injectable so tests assert on the delay ladder without
    actually waiting through it.
    """
    last_error: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await operation()
        except give_up_on:
            raise
        except retry_on as exc:
            if retry_if is not None and not retry_if(exc):
                raise
            last_error = exc
            if attempt == policy.max_attempts:
                break
            delay = policy.delay_for(attempt)
            if on_failure is not None:
                on_failure(attempt, exc, delay)
            await sleep(delay)

    assert last_error is not None  # unreachable: the loop runs at least once
    raise last_error


def retry_sync(
    operation: Callable[[], T],
    *,
    policy: BackoffPolicy,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    retry_if: Callable[[BaseException], bool] | None = None,
    on_failure: Callable[[int, BaseException, float], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Blocking counterpart for boto3, whose client calls are synchronous."""
    last_error: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return operation()
        except retry_on as exc:
            if retry_if is not None and not retry_if(exc):
                raise
            last_error = exc
            if attempt == policy.max_attempts:
                break
            delay = policy.delay_for(attempt)
            if on_failure is not None:
                on_failure(attempt, exc, delay)
            sleep(delay)

    assert last_error is not None
    raise last_error


def chunked(items: Iterable[T], size: int) -> Iterable[list[T]]:
    """Yield ``items`` in lists of at most ``size``."""
    batch: list[T] = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch
