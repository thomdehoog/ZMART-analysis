"""
Job queue -- the order in which waiting jobs start.

Every job gets an order key when it is submitted: ``(deadline, number)``.
The deadline is the time by which the microscope needs the answer
(``math.inf`` without one); the number counts every submit of the engine,
so among equal deadlines the earlier submit comes first. The key is unique,
so two jobs never tie.

Jobs wait in two places, and both keep this order:

- ``OrderedThreadPool``: for a thread of the engine (image jobs, and the
  closes of units, each in a pool of their own).
- ``OrderedGate``: for a worker of a step, which allows ``max_workers`` of
  that step at once.
"""

from __future__ import annotations

import heapq
import itertools
import math
import threading
import time
from concurrent.futures import Future


class Order:
    """Hands out the order keys of one engine."""

    def __init__(self):
        self.numbers = itertools.count(1)
        self.lock = threading.Lock()

    def key(self, deadline_at=None):
        """The next key; *deadline_at* is a time as ``time.time()`` gives it."""
        with self.lock:
            number = next(self.numbers)
        return (math.inf if deadline_at is None else deadline_at, number)


def deadline_at(deadline):
    """The time *deadline* seconds from now, or None without one."""
    if deadline is None:
        return None
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or deadline < 0:
        raise ValueError(f"deadline must be a number of seconds, 0 or more, got {deadline!r}")
    return time.time() + deadline


class OrderedThreadPool:
    """A thread pool that starts the waiting job whose order key comes first.

    ``submit()`` returns a ``concurrent.futures.Future``; ``shutdown()``
    lets the waiting jobs finish, or with ``wait=False`` cancels them.
    """

    def __init__(self, max_workers, name):
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self.waiting = []
        self.lock = threading.Lock()
        self.not_empty = threading.Condition(self.lock)
        self.stopped = False
        self.threads = [
            threading.Thread(target=self.serve, name=f"{name}-{i}", daemon=True)
            for i in range(max_workers)
        ]
        for thread in self.threads:
            thread.start()

    def submit(self, order, fn, *args):
        """Run ``fn(*args)`` on a thread, in the place *order* gives it."""
        future = Future()
        with self.not_empty:
            if self.stopped:
                raise RuntimeError("The job queue has been shut down")
            heapq.heappush(self.waiting, (order, future, fn, args))
            self.not_empty.notify()
        return future

    def serve(self):
        """One thread: take the first waiting job, run it, repeat."""
        while True:
            with self.not_empty:
                while not self.waiting and not self.stopped:
                    self.not_empty.wait()
                if not self.waiting:
                    return
                _, future, fn, args = heapq.heappop(self.waiting)
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn(*args))
            except BaseException as e:
                future.set_exception(e)

    def shutdown(self, wait=True):
        cancelled = []
        with self.not_empty:
            self.stopped = True
            if not wait:
                cancelled = [item[1] for item in self.waiting]
                self.waiting.clear()
            self.not_empty.notify_all()
        for future in cancelled:
            future.cancel()
        if wait:
            for thread in self.threads:
                thread.join()


class OrderedGate:
    """Lets *width* callers through at once; the others wait in order.

    Like a semaphore, but when a place comes free it goes to the waiting
    caller whose order key comes first, not to whichever thread wakes first.
    """

    def __init__(self, width):
        self.width = width
        self.free = width
        self.waiting = []
        self.changed = threading.Condition()
        #: A ticket per caller, so two callers with the same key stay apart.
        self.tickets = itertools.count()

    def acquire(self, order=None):
        """Wait for a place; a caller without a key comes after every keyed one."""
        entry = (order or (math.inf, math.inf), next(self.tickets))
        with self.changed:
            heapq.heappush(self.waiting, entry)
            while not (self.free and self.waiting[0] == entry):
                self.changed.wait()
            heapq.heappop(self.waiting)
            self.free -= 1
            # The next in line may also fit, when the gate is wider than one.
            self.changed.notify_all()

    def release(self):
        with self.changed:
            self.free += 1
            self.changed.notify_all()
