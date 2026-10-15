"""Work on daemon threads, so Ctrl-C ends the process without waiting for a slow request to finish."""

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future


def in_background[T](work: Callable[[], T], *, name: str) -> Future[T]:
    """Run one piece of work on its own daemon thread."""
    [future] = run_all([work], limit=1, name=name)
    return future


def run_all[T](jobs: Sequence[Callable[[], T]], *, limit: int, name: str) -> list[Future[T]]:
    """Run jobs on at most `limit` daemon threads; the futures come back in job order."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    futures: list[Future[T]] = [Future() for _ in jobs]
    pending = iter(zip(jobs, futures, strict=True))
    lock = threading.Lock()

    def worker() -> None:
        while True:
            with lock:
                item = next(pending, None)
            if item is None:
                return
            job, future = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(job())
            except BaseException as error:  # noqa: BLE001 — handed to the caller through the future
                future.set_exception(error)

    for index in range(min(limit, len(jobs))):
        threading.Thread(target=worker, name=f"{name}-{index}", daemon=True).start()
    return futures
