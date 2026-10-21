"""
Job queue -- the order in which waiting jobs start.

The engine runs each submitted image on a thread from an
``OrderedThreadPool``. A plain ``ThreadPoolExecutor`` runs jobs in the
order they arrived; this one keeps them sorted, so a submit with a higher
``priority`` is started before the ones already waiting. Among equals, the
earlier submit goes first.
"""

from __future__ import annotations

import heapq
import threading
from concurrent.futures import Future


class OrderedThreadPool:
    """A thread pool that starts the waiting job that comes first in order.

    Higher priority first; among equals, the earlier submit. ``submit()``
    returns a ``concurrent.futures.Future``; ``shutdown(wait=True)`` lets
    the waiting jobs finish.
    """

    def __init__(self, max_workers, name="engine-worker"):
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self.waiting = []
        self.lock = threading.Lock()
        self.not_empty = threading.Condition(self.lock)
        self.counter = 0
        self.stopped = False
        self.threads = [
            threading.Thread(target=self.serve, name=f"{name}-{i}", daemon=True)
            for i in range(max_workers)
        ]
        for thread in self.threads:
            thread.start()

    def submit(self, fn, *args, priority=0, **kwargs):
        future = Future()
        with self.not_empty:
            if self.stopped:
                raise RuntimeError("The job queue has been shut down")
            self.counter += 1
            heapq.heappush(self.waiting, (-priority, self.counter, future, fn, args, kwargs))
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
                _, _, future, fn, args, kwargs = heapq.heappop(self.waiting)
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as e:
                future.set_exception(e)

    def shutdown(self, wait=True):
        cancelled = []
        with self.not_empty:
            self.stopped = True
            if not wait:
                cancelled = [item[2] for item in self.waiting]
                self.waiting.clear()
            self.not_empty.notify_all()
        for future in cancelled:
            future.cancel()
        if wait:
            for thread in self.threads:
                thread.join()
