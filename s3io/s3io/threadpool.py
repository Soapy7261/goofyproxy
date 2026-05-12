import os
import sys
import threading
import queue
from collections.abc import Callable
from typing import Any


class JobFuture:
    """holds the result (or exception) of a job and an event for waiting."""

    _event: threading.Event
    _result: Any

    _exc_info: tuple | None
    """stored as (exception_type, exception_value, traceback)"""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._result: Any = None
        self._exc_info: tuple | None = None

    def _set_result(self, result: Any) -> None:
        self._result = result
        self._event.set()

    def _set_exception(self, exc_info: tuple) -> None:
        self._exc_info = exc_info
        self._event.set()

    def get(self) -> Any:
        """
        block until the job is done.

        returns the result if the job completed normally, otherwise
        re-raises the captured exception with the original traceback.
        """
        self._event.wait()
        if self._exc_info is not None:
            # re-raise with the original traceback
            raise self._exc_info[1].with_traceback(self._exc_info[2])
        return self._result


class ThreadPool:
    """singleton thread pool"""

    _instance: ThreadPool | None = None
    _lock = threading.Lock()

    def __init__(self, num_threads: int | None = None) -> None:
        self.num_threads = num_threads or os.cpu_count() or 4
        self.task_queue: queue.Queue = queue.Queue()
        self.threads: list[threading.Thread] = []

        for i in range(self.num_threads):
            t = threading.Thread(
                target=self._worker,
                name=f"ThreadPool #{i+1}/{self.num_threads}",
                daemon=True,
            )
            self.threads.append(t)
            t.start()

    @classmethod
    def _get_instance(cls) -> ThreadPool:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @staticmethod
    def enqueue(
        job: Callable[..., Any],
        *args: Any
    ) -> JobFuture:
        """
        enqueue a callable with positional arguments and return a `JobFuture`.
        """

        pool = ThreadPool._get_instance()
        future = JobFuture()
        pool.task_queue.put((future, job, args))
        return future

    def _worker(self) -> None:
        """main loop for every pool thread"""
        while True:
            future, func, args = self.task_queue.get()

            try:
                result = func(*args)
            except BaseException:
                # capture the full exception information
                future._set_exception(sys.exc_info())
            else:
                future._set_result(result)

            self.task_queue.task_done()
