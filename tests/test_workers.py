"""DaemonWorkers: blocking calls on daemon threads that stay for the next call."""

import asyncio
import threading

import pytest

from openweir.host.workers import DaemonWorkers


def test_calls_one_after_another_reuse_one_daemon_thread() -> None:
    workers = DaemonWorkers("test")
    threads: list[threading.Thread] = []

    async def scenario() -> list[int]:
        results = []
        for n in range(20):
            results.append(
                await workers.run(lambda n=n: (threads.append(threading.current_thread()), n)[1])
            )
        return results

    assert asyncio.run(scenario()) == list(range(20))
    assert len(set(threads)) == 1
    assert workers.threads == 1
    (thread,) = set(threads)
    assert thread.daemon
    assert thread is not threading.main_thread()


def test_a_slow_call_does_not_hold_up_the_others() -> None:
    workers = DaemonWorkers("test", limit=2)
    entered, release = threading.Event(), threading.Event()

    def slow_call() -> str:
        entered.set()
        release.wait(2)
        return "slow"

    async def scenario() -> list[str]:
        slow = asyncio.create_task(workers.run(slow_call))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            quick = await asyncio.wait_for(workers.run(lambda: "quick"), 1)
        finally:
            release.set()
        return [quick, await slow]

    assert asyncio.run(scenario()) == ["quick", "slow"]
    assert workers.threads == 2


def test_beyond_the_limit_a_call_waits_and_one_given_up_is_not_made() -> None:
    workers = DaemonWorkers("test", limit=1)
    release = threading.Event()
    made: list[str] = []

    def blocked() -> str:
        release.wait(2)
        return "first"

    async def scenario() -> str:
        first = asyncio.create_task(workers.run(blocked))
        await asyncio.sleep(0.05)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(workers.run(lambda: made.append("given up")), 0.05)
        later = asyncio.create_task(workers.run(lambda: made.append("later") or "later"))
        release.set()
        assert await first == "first"
        return await later

    assert asyncio.run(scenario()) == "later"
    assert made == ["later"]
    assert workers.threads == 1


def test_a_failing_call_raises_to_its_caller_and_the_thread_stays() -> None:
    workers = DaemonWorkers("test")

    def failing() -> None:
        raise ValueError("no")

    async def scenario() -> int:
        with pytest.raises(ValueError, match="no"):
            await workers.run(failing)
        return await workers.run(lambda: 7)

    assert asyncio.run(scenario()) == 7
    assert workers.threads == 1
