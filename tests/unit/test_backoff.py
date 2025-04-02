"""The retry policy. Everything that retries in this service uses it."""

from __future__ import annotations

import random

import pytest

from src.utils.backoff import BackoffPolicy, chunked, retry_async, retry_sync


class TestBackoffPolicy:
    def test_delay_doubles(self):
        policy = BackoffPolicy(base_delay=1.0, max_delay=60.0, max_attempts=6, jitter=0)
        assert [policy.delay_for(n) for n in range(1, 6)] == [1.0, 2.0, 4.0, 8.0, 16.0]

    def test_delay_is_capped(self):
        policy = BackoffPolicy(base_delay=1.0, max_delay=10.0, max_attempts=10, jitter=0)
        assert policy.delay_for(9) == 10.0

    def test_attempt_is_one_based(self):
        policy = BackoffPolicy(base_delay=2.0, jitter=0)
        assert policy.delay_for(1) == 2.0
        with pytest.raises(ValueError, match="1-based"):
            policy.delay_for(0)

    def test_jitter_stays_within_the_band(self):
        policy = BackoffPolicy(base_delay=4.0, max_delay=60.0, jitter=0.25)
        rng = random.Random(0)
        delays = [policy.delay_for(1, rng=rng) for _ in range(200)]
        assert all(3.0 <= delay <= 5.0 for delay in delays)

    def test_jitter_actually_varies(self):
        """Without this the retries of N workers stay synchronized."""
        policy = BackoffPolicy(base_delay=4.0, jitter=0.5)
        rng = random.Random(1)
        assert len({policy.delay_for(2, rng=rng) for _ in range(50)}) > 20

    def test_schedule_describes_the_ladder(self):
        policy = BackoffPolicy(base_delay=1.0, max_delay=8.0, max_attempts=6, jitter=0.5)
        assert policy.schedule() == [1.0, 2.0, 4.0, 8.0, 8.0]


class TestRetryAsync:
    @pytest.fixture
    def policy(self) -> BackoffPolicy:
        return BackoffPolicy(base_delay=1.0, max_delay=8.0, max_attempts=4, jitter=0)

    async def test_returns_first_success_without_sleeping(self, policy):
        slept: list[float] = []

        async def operation():
            return "ok"

        result = await retry_async(operation, policy=policy, sleep=_recorder(slept))
        assert result == "ok"
        assert slept == []

    async def test_retries_until_success(self, policy):
        slept: list[float] = []
        attempts = {"n": 0}

        async def operation():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionError("not yet")
            return attempts["n"]

        result = await retry_async(operation, policy=policy, sleep=_recorder(slept))
        assert result == 3
        assert slept == [1.0, 2.0]

    async def test_does_not_sleep_after_the_final_attempt(self, policy):
        """The original notifier slept a full delay before giving up."""
        slept: list[float] = []

        async def operation():
            raise ConnectionError("always")

        with pytest.raises(ConnectionError):
            await retry_async(operation, policy=policy, sleep=_recorder(slept))
        assert len(slept) == policy.max_attempts - 1 == 3

    async def test_reraises_the_last_error(self, policy):
        async def operation():
            raise TimeoutError("upstream gone")

        with pytest.raises(TimeoutError, match="upstream gone"):
            await retry_async(operation, policy=policy, sleep=_noop)

    async def test_give_up_on_skips_retries(self, policy):
        calls = {"n": 0}

        async def operation():
            calls["n"] += 1
            raise ValueError("malformed payload")

        with pytest.raises(ValueError):
            await retry_async(
                operation,
                policy=policy,
                retry_on=(Exception,),
                give_up_on=(ValueError,),
                sleep=_noop,
            )
        assert calls["n"] == 1

    async def test_retry_if_filters_on_the_instance(self, policy):
        calls = {"n": 0}

        async def operation():
            calls["n"] += 1
            raise OSError(13, "permission denied")

        with pytest.raises(OSError):
            await retry_async(
                operation,
                policy=policy,
                retry_if=lambda exc: getattr(exc, "errno", None) != 13,
                sleep=_noop,
            )
        assert calls["n"] == 1

    async def test_unlisted_exception_propagates_immediately(self, policy):
        calls = {"n": 0}

        async def operation():
            calls["n"] += 1
            raise KeyError("boom")

        with pytest.raises(KeyError):
            await retry_async(operation, policy=policy, retry_on=(ConnectionError,), sleep=_noop)
        assert calls["n"] == 1

    async def test_on_failure_sees_each_failed_attempt(self, policy):
        seen: list[tuple[int, float]] = []

        async def operation():
            raise ConnectionError("down")

        with pytest.raises(ConnectionError):
            await retry_async(
                operation,
                policy=policy,
                on_failure=lambda n, exc, delay: seen.append((n, delay)),
                sleep=_noop,
            )
        assert seen == [(1, 1.0), (2, 2.0), (3, 4.0)]


class TestRetrySync:
    def test_retries_then_succeeds(self):
        policy = BackoffPolicy(base_delay=1.0, max_attempts=3, jitter=0)
        slept: list[float] = []
        attempts = {"n": 0}

        def operation():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise ConnectionError("throttled")
            return "done"

        assert retry_sync(operation, policy=policy, sleep=slept.append) == "done"
        assert slept == [1.0]

    def test_retry_if_short_circuits(self):
        policy = BackoffPolicy(base_delay=1.0, max_attempts=5, jitter=0)
        calls = {"n": 0}

        def operation():
            calls["n"] += 1
            raise RuntimeError("AccessDenied")

        with pytest.raises(RuntimeError):
            retry_sync(
                operation,
                policy=policy,
                retry_if=lambda exc: "AccessDenied" not in str(exc),
                sleep=lambda _: None,
            )
        assert calls["n"] == 1


class TestChunked:
    def test_splits_evenly(self):
        assert list(chunked(range(6), 2)) == [[0, 1], [2, 3], [4, 5]]

    def test_yields_the_remainder(self):
        assert list(chunked(range(5), 2)) == [[0, 1], [2, 3], [4]]

    def test_empty_input_yields_nothing(self):
        assert list(chunked([], 3)) == []


def _recorder(sink: list[float]):
    async def sleep(delay: float) -> None:
        sink.append(delay)

    return sleep


async def _noop(_delay: float) -> None:
    return None
