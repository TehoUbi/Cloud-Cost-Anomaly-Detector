"""The read cache. Its job is to keep an aggregate query off the hot path."""

from __future__ import annotations

import asyncio

import pytest

from src.api.cache import TTLCache, get_cache, invalidate_read_cache


def counting_factory(value: str = "v"):
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return value

    return factory, calls


class TestBasics:
    async def test_first_call_computes(self):
        cache = TTLCache(ttl=10)
        factory, calls = counting_factory()
        assert await cache.get_or_set("k", factory) == "v"
        assert calls["n"] == 1

    async def test_second_call_is_a_hit(self):
        cache = TTLCache(ttl=10)
        factory, calls = counting_factory()
        await cache.get_or_set("k", factory)
        await cache.get_or_set("k", factory)
        assert calls["n"] == 1

    async def test_distinct_keys_are_independent(self):
        cache = TTLCache(ttl=10)
        factory, calls = counting_factory()
        await cache.get_or_set("a", factory)
        await cache.get_or_set("b", factory)
        assert calls["n"] == 2

    async def test_entry_expires(self):
        cache = TTLCache(ttl=0.05)
        factory, calls = counting_factory()
        await cache.get_or_set("k", factory)
        await asyncio.sleep(0.08)
        await cache.get_or_set("k", factory)
        assert calls["n"] == 2

    async def test_per_call_ttl_override(self):
        cache = TTLCache(ttl=10)
        factory, calls = counting_factory()
        await cache.get_or_set("k", factory, ttl=0.05)
        await asyncio.sleep(0.08)
        await cache.get_or_set("k", factory, ttl=0.05)
        assert calls["n"] == 2

    async def test_falsy_values_are_cached(self):
        """An empty rollup is a real answer, not a cache miss."""
        cache = TTLCache(ttl=10)
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            return []

        assert await cache.get_or_set("k", factory) == []
        assert await cache.get_or_set("k", factory) == []
        assert calls["n"] == 1


class TestSingleFlight:
    async def test_concurrent_misses_compute_once(self):
        """Without this, every request arriving at the moment an entry expires
        runs the same aggregate query — a worse spike than no cache."""
        cache = TTLCache(ttl=10)
        calls = {"n": 0}

        async def slow_factory():
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return "v"

        results = await asyncio.gather(
            *[cache.get_or_set("k", slow_factory) for _ in range(100)]
        )
        assert calls["n"] == 1
        assert set(results) == {"v"}

    async def test_different_keys_do_not_block_each_other(self):
        cache = TTLCache(ttl=10)
        order: list[str] = []

        async def factory(name: str, delay: float):
            await asyncio.sleep(delay)
            order.append(name)
            return name

        await asyncio.gather(
            cache.get_or_set("slow", lambda: factory("slow", 0.06)),
            cache.get_or_set("fast", lambda: factory("fast", 0.01)),
        )
        assert order == ["fast", "slow"]

    async def test_a_failing_factory_does_not_poison_the_key(self):
        cache = TTLCache(ttl=10)

        async def broken():
            raise RuntimeError("query failed")

        with pytest.raises(RuntimeError):
            await cache.get_or_set("k", broken)

        factory, calls = counting_factory("recovered")
        assert await cache.get_or_set("k", factory) == "recovered"
        assert calls["n"] == 1


class TestInvalidation:
    async def test_invalidate_all(self):
        cache = TTLCache(ttl=10)
        factory, calls = counting_factory()
        await cache.get_or_set("a", factory)
        await cache.get_or_set("b", factory)
        assert cache.invalidate() == 2
        await cache.get_or_set("a", factory)
        assert calls["n"] == 3

    async def test_invalidate_by_prefix(self):
        cache = TTLCache(ttl=10)
        factory, _ = counting_factory()
        await cache.get_or_set("summary:30", factory)
        await cache.get_or_set("summary:90", factory)
        await cache.get_or_set("services", factory)
        assert cache.invalidate("summary:") == 2
        assert cache.stats["entries"] == 1

    async def test_invalidating_an_empty_cache_is_harmless(self):
        assert TTLCache().invalidate() == 0

    async def test_module_level_invalidation(self):
        """What the poll endpoint calls after writing."""
        factory, calls = counting_factory()
        await get_cache().get_or_set("k", factory)
        invalidate_read_cache("test")
        await get_cache().get_or_set("k", factory)
        assert calls["n"] == 2


class TestBounds:
    async def test_entry_count_is_capped(self):
        cache = TTLCache(ttl=100, max_entries=4)
        factory, _ = counting_factory()
        for index in range(10):
            await cache.get_or_set(f"k{index}", factory)
        assert cache.stats["entries"] <= 4

    async def test_locks_do_not_leak_past_eviction(self):
        cache = TTLCache(ttl=100, max_entries=3)
        factory, _ = counting_factory()
        for index in range(20):
            await cache.get_or_set(f"k{index}", factory)
        assert len(cache._locks) <= 4


class TestStats:
    async def test_hit_ratio_is_reported(self):
        cache = TTLCache(ttl=10)
        factory, _ = counting_factory()
        await cache.get_or_set("k", factory)
        for _ in range(3):
            await cache.get_or_set("k", factory)
        stats = cache.stats
        assert stats["misses"] == 1
        assert stats["hits"] == 3
        assert stats["hit_ratio"] == pytest.approx(0.75)

    async def test_empty_cache_reports_zero_ratio(self):
        assert TTLCache().stats["hit_ratio"] == 0.0
