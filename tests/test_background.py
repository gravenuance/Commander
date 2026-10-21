import threading
from contextvars import ContextVar

import pytest

from commander.background import in_background, run_all


def test_results_come_back_in_job_order() -> None:
    jobs = [lambda n=n: n * n for n in range(10)]
    assert [f.result(timeout=5) for f in run_all(jobs, limit=3, name="t")] == [n * n for n in range(10)]


def test_no_more_threads_than_the_limit_run_at_once() -> None:
    running, peak = 0, 0
    lock = threading.Lock()
    release = threading.Barrier(2, timeout=5)

    def job() -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        release.wait()
        with lock:
            running -= 1

    for future in run_all([job] * 6, limit=2, name="t"):
        future.result(timeout=5)
    assert peak == 2


def test_pick_chooses_which_waiting_job_starts_next_and_the_futures_stay_in_job_order() -> None:
    started: list[int] = []
    jobs = [lambda n=n: started.append(n) or n for n in range(4)]
    futures = run_all(jobs, limit=1, name="t", pick=lambda waiting: waiting[-1])
    assert [f.result(timeout=5) for f in futures] == [0, 1, 2, 3]
    assert started == [3, 2, 1, 0]


def test_an_error_reaches_the_caller_through_its_future() -> None:
    def fail() -> int:
        raise KeyError("boom")

    ok, failed = run_all([lambda: 1, fail], limit=2, name="t")
    assert ok.result(timeout=5) == 1
    with pytest.raises(KeyError, match="boom"):
        failed.result(timeout=5)


def test_work_runs_on_a_daemon_thread() -> None:
    assert in_background(lambda: threading.current_thread().daemon, name="t").result(timeout=5) is True


def test_no_jobs_gives_no_futures() -> None:
    assert run_all([], limit=4, name="t") == []


def test_limit_below_one_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        run_all([lambda: 1], limit=0, name="t")


def test_a_job_sees_the_context_variables_of_the_code_that_started_it_and_leaves_them_unchanged() -> None:
    answer: ContextVar[str] = ContextVar("answer", default="none")
    answer.set("outer")

    def job() -> tuple[str, str]:
        seen = answer.get()
        answer.set("inner")
        # A job's own jobs inherit from it, not from the first caller.
        return seen, in_background(answer.get, name="t").result(timeout=5)

    assert in_background(job, name="t").result(timeout=5) == ("outer", "inner")
    assert answer.get() == "outer"
