"""Work on daemon threads, so Ctrl-C ends the process without waiting for a slow request to finish."""

import contextvars
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future


def in_background[T](work: Callable[[], T], *, name: str) -> Future[T]:
    """Run one piece of work on its own daemon thread."""
    [future] = run_all([work], limit=1, name=name)
    return future


def _first(waiting: Sequence[int]) -> int:
    return waiting[0]


def run_all[T](
    jobs: Sequence[Callable[[], T]],
    *,
    limit: int,
    name: str,
    pick: Callable[[Sequence[int]], int] = _first,
) -> list[Future[T]]:
    """Run jobs on at most `limit` daemon threads; the futures come back in job order.

    As a thread frees up, `pick` chooses the next job from the indexes of those not yet started (in job
    order); by default the first. Each job runs in a copy of the caller's context variables, so what the
    caller set is seen by the job and by the jobs it starts, and what the job sets stays with it.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    # A context cannot be entered by two threads at once, so each job gets its own copy.
    contexts = [contextvars.copy_context() for _ in jobs]
    futures: list[Future[T]] = [Future() for _ in jobs]
    waiting = list(range(len(jobs)))
    lock = threading.Lock()

    def worker() -> None:
        while True:
            with lock:
                if not waiting:
                    return
                index = pick(waiting)
                waiting.remove(index)
            job, future = jobs[index], futures[index]
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(contexts[index].run(job))
            except BaseException as error:  # noqa: BLE001 — handed to the caller through the future
                future.set_exception(error)

    for index in range(min(limit, len(jobs))):
        threading.Thread(target=worker, name=f"{name}-{index}", daemon=True).start()
    return futures
