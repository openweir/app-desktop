"""Blocking calls made from asyncio on daemon threads that stay for the next call.

The macOS manager's calls (host/extension.py) complete on the main queue, which nothing
serves once the app quits: one in flight then returns only at its timeout. The backups'
requests to iCloud and Google Drive run until theirs, and UAC's consent prompt for the
take-over helper (desktop/elevation.py) waits for the user. asyncio.to_thread runs calls on
threads that the interpreter joins as it exits, so any of them would hold the process up
after the user quit; these threads are daemons. They stay once started, rather than a thread
being started for each provider message the UI polls with.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections import deque
from collections.abc import Callable
from functools import partial

LIMIT = 8

# A call made on a thread; it returns what hands the outcome to the caller, which the thread
# does once it counts as free for the next call.
type _Job = Callable[[], Callable[[], None]]


def _nothing() -> None:
    pass


class DaemonWorkers:
    """Up to ``limit`` daemon threads that make blocking calls side by side, so that a slow
    one (a Start, a speed test) does not hold up the status reads. A thread starts when a
    call finds none free, and then waits for the next call; once ``limit`` threads are busy,
    a call waits for the first of them to finish."""

    def __init__(self, name: str, limit: int = LIMIT) -> None:
        self._name = name
        self._limit = limit
        self._ready = threading.Condition()
        self._jobs: deque[_Job] = deque()
        self._threads = 0
        self._busy = 0  # threads making a call
        self._started = 0  # threads ever started, to number their names

    @property
    def threads(self) -> int:
        with self._ready:
            return self._threads

    async def run[T](self, call: Callable[[], T]) -> T:
        """``call``'s result, from one of the threads. A caller that gives up stops waiting
        at once; a call given up on before a thread took it is not made at all."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        abandoned = threading.Event()
        future.add_done_callback(lambda _: abandoned.set())

        def settle(outcome: Callable[[], None]) -> None:
            if not future.done():  # the caller may have given up
                outcome()

        def deliver(outcome: Callable[[], None]) -> None:
            with contextlib.suppress(RuntimeError):  # the loop closed meanwhile
                loop.call_soon_threadsafe(settle, outcome)

        def job() -> Callable[[], None]:
            if abandoned.is_set():
                return _nothing
            try:
                result = call()
            except BaseException as error:  # handed to the caller
                return partial(deliver, partial(future.set_exception, error))
            return partial(deliver, partial(future.set_result, result))

        self._submit(job)
        return await future

    def _submit(self, job: _Job) -> None:
        with self._ready:
            self._jobs.append(job)
            if self._threads - self._busy >= len(self._jobs):
                self._ready.notify()  # a free thread takes it
                return
            if self._threads >= self._limit:
                return  # the first thread to finish its call takes this one
            self._threads += 1
            self._started += 1
            name = f"{self._name}-{self._started}"
        threading.Thread(target=self._work, name=name, daemon=True).start()

    def _work(self) -> None:
        try:
            while True:
                with self._ready:
                    while not self._jobs:
                        self._ready.wait()
                    job = self._jobs.popleft()
                    self._busy += 1
                hand_over = job()
                with self._ready:
                    # Free before the caller hears back, so that its next call finds this
                    # thread rather than start another.
                    self._busy -= 1
                hand_over()
        finally:
            with self._ready:
                self._threads -= 1


_WORKERS = DaemonWorkers("openweir-blocking")


async def run_blocking[T](call: Callable[[], T]) -> T:
    """``call``'s result, made on one of the process's daemon worker threads."""
    return await _WORKERS.run(call)
