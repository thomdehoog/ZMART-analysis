"""
A thread pool that starts the most urgent job first.

The engine runs each submitted image on a thread from this pool. A plain
``ThreadPoolExecutor`` runs jobs in the order they arrived; this one keeps a
priority queue instead, so a submit with a higher ``priority`` is started
before the ones already waiting. Among equals, the earlier submit goes
first. Only the Engine uses it.
"""

from __future__ import annotations

import heapq
import threading
from concurrent.futures import Future


class PriorityThreadPool:
    """Thread pool that dispatches tasks in priority order.

    Higher priority dispatches first; FIFO within the same priority.
    Compatible subset of ThreadPoolExecutor: ``submit()`` returns a
    ``concurrent.futures.Future``; ``shutdown(wait=True)`` drains workers.
    """

    def __init__(self, max_workers):
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self._heap = []
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._counter = 0
        self._shutdown = False
        self._workers = []
        for i in range(max_workers):
            t = threading.Thread(
                target=self._run,
                name=f"engine-worker-{i}",
                daemon=True,
            )
            self._workers.append(t)
            t.start()

    def submit(self, fn, *args, priority=0, **kwargs):
        future = Future()
        with self._not_empty:
            if self._shutdown:
                raise RuntimeError("Priority pool has been shut down")
            self._counter += 1
            heapq.heappush(
                self._heap,
                (-priority, self._counter, future, fn, args, kwargs),
            )
            self._not_empty.notify()
        return future

    def _run(self):
        while True:
            with self._not_empty:
                while not self._heap and not self._shutdown:
                    self._not_empty.wait()
                if not self._heap:
                    return
                _, _, future, fn, args, kwargs = heapq.heappop(self._heap)
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as e:
                future.set_exception(e)

    def shutdown(self, wait=True):
        to_cancel = []
        with self._not_empty:
            self._shutdown = True
            if not wait:
                to_cancel = [item[2] for item in self._heap]
                self._heap.clear()
            self._not_empty.notify_all()
        for future in to_cancel:
            future.cancel()
        if wait:
            for t in self._workers:
                t.join()
