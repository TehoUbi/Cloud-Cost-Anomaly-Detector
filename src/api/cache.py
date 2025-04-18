"""Small TTL cache for the aggregate read endpoints.

Cost data changes when a poll runs — a few times a day — but the dashboard
endpoints that aggregate it (``/costs/summary``, ``/costs/services``,
``/costs/baselines``) are the most-requested in the API. Recomputing a 90-day
rollup per request makes the database the bottleneck long before the
application is; a load test at 5,000 concurrent users measured a p95 of 6.5s
on those endpoints while ``/health``, which touches nothing, stayed at 280ms.

Serving them from a short-lived cache is correct rather than a workaround: the
underlying numbers are hours old by construction. Writes invalidate it, so a
poll's results are visible immediately.

Deliberately in-process. A shared cache would mean Redis, and the thing being
cached is a few kilobytes of JSON that each worker can hold itself. The cost
is that a cached value is per-worker, so a stale read can persist for up to
one TTL after an invalidation on a different worker — acceptable for a
spend-rollup, and the reason write endpoints never read through the cache.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 30.0


@dataclass(slots=True)
class _Entry:
    value: Any
    expires_at: float


class TTLCache:
    """Async-safe cache with single-flight recomputation.

    The per-key lock is the point: without it, 5,000 requests arriving the
    moment an entry expires all miss and all run the same aggregate query,
    which is a worse spike than having no cache at all.
    """

    def __init__(self, *, ttl: float = DEFAULT_TTL_SECONDS, max_entries: int = 256) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._entries: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._hits = 0
        self._misses = 0

    async def get_or_set(
        self, key: str, factory: Callable[[], Awaitable[Any]], *, ttl: float | None = None
    ) -> Any:
        now = time.monotonic()
        entry = self._entries.get(key)
        if entry is not None and entry.expires_at > now:
            self._hits += 1
            return entry.value

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Re-check: another coroutine may have populated it while we waited.
            entry = self._entries.get(key)
            now = time.monotonic()
            if entry is not None and entry.expires_at > now:
                self._hits += 1
                return entry.value

            self._misses += 1
            value = await factory()
            self._store(key, value, ttl if ttl is not None else self.ttl)
            return value

    def _store(self, key: str, value: Any, ttl: float) -> None:
        if len(self._entries) >= self.max_entries and key not in self._entries:
            # Crude but bounded: drop whatever expires soonest. The key space
            # here is small and fixed (endpoint plus a few query parameters).
            oldest = min(self._entries, key=lambda k: self._entries[k].expires_at)
            del self._entries[oldest]
            self._locks.pop(oldest, None)
        self._entries[key] = _Entry(value=value, expires_at=time.monotonic() + ttl)

    def invalidate(self, prefix: str | None = None) -> int:
        """Drop everything, or every key under ``prefix``. Returns the count."""
        if prefix is None:
            dropped = len(self._entries)
            self._entries.clear()
            self._locks.clear()
            return dropped

        keys = [key for key in self._entries if key.startswith(prefix)]
        for key in keys:
            del self._entries[key]
            self._locks.pop(key, None)
        return len(keys)

    @property
    def stats(self) -> dict[str, float | int]:
        total = self._hits + self._misses
        return {
            "entries": len(self._entries),
            "hits": self._hits,
            "misses": self._misses,
            "hit_ratio": round(self._hits / total, 4) if total else 0.0,
            "ttl_seconds": self.ttl,
        }


_cache = TTLCache()


def get_cache() -> TTLCache:
    return _cache


def invalidate_read_cache(reason: str = "") -> None:
    """Called after anything that writes cost or alert data."""
    dropped = _cache.invalidate()
    if dropped:
        logger.debug(
            "Invalidated %d cached read(s)%s", dropped, f" after {reason}" if reason else ""
        )
