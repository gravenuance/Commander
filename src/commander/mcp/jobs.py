"""Background jobs for the commands that take minutes: one at a time, polled by id."""

import json
import logging
import secrets
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypedDict

from commander.background import in_background
from commander.clibase import Clock
from commander.mcp.runner import ProgressSink, Runner
from commander.mcp.text import clean_json, clean_text

log = logging.getLogger(__name__)

PROGRESS_LINES_SHOWN = 20
MAX_LINE_CHARS = 500
MAX_JOBS_KEPT = 20
_WARNING_PREFIXES = ("cmdr:", "WARNING:", "ERROR:", "CRITICAL:")


class JobKind(StrEnum):
    SYNC = "sync"
    META = "meta"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class JobView(TypedDict):
    id: str
    kind: str
    state: str
    startedAt: str
    finishedAt: str | None
    exitCode: int | None
    progress: list[str]
    warnings: list[str]
    result: object
    error: str | None


class JobBusyError(Exception):
    def __init__(self, job: "Job") -> None:
        super().__init__(
            f"the {job.kind.value} job {job.id} is still running; poll it with job_status, "
            "and start another when it is done"
        )
        self.job = job


class UnknownJobError(Exception):
    def __init__(self, job_id: str) -> None:
        super().__init__(f"no job {job_id!r}; start_sync and start_meta return the id")


@dataclass(slots=True)
class Job:
    id: str
    kind: JobKind
    argv: list[str]
    started_at: str
    sink: ProgressSink = field(default_factory=ProgressSink)
    state: JobState = JobState.QUEUED
    finished_at: str | None = None
    exit_code: int | None = None
    result: object = None
    error: str | None = None
    finished: threading.Event = field(default_factory=threading.Event)

    @property
    def active(self) -> bool:
        return self.state in {JobState.QUEUED, JobState.RUNNING}


class JobBoard:
    def __init__(self, runner: Runner, now: Clock) -> None:
        self._runner = runner
        self._now = now
        self._guard = threading.Lock()
        self._jobs: dict[str, Job] = {}

    def running(self) -> Job | None:
        with self._guard:
            return next((job for job in self._jobs.values() if job.active), None)

    def start(self, kind: JobKind, argv: list[str]) -> Job:
        """Queue the command on its own thread; refused while another job is queued or running."""
        with self._guard:
            active = next((job for job in self._jobs.values() if job.active), None)
            if active is not None:
                raise JobBusyError(active)
            job = Job(secrets.token_hex(4), kind, argv, self._stamp())
            self._jobs[job.id] = job
            self._forget_old()
        in_background(lambda: self._work(job), name=f"cmdr-mcp-{kind.value}")
        return job

    def get(self, job_id: str) -> Job:
        with self._guard:
            job = self._jobs.get(job_id)
        if job is None:
            raise UnknownJobError(job_id)
        return job

    def view(self, job: Job) -> JobView:
        with self._guard:
            state, finished_at, exit_code, result, error = (
                job.state,
                job.finished_at,
                job.exit_code,
                job.result,
                job.error,
            )
        warnings, progress = _split(job.sink.getvalue())
        return JobView(
            id=job.id,
            kind=job.kind.value,
            state=state.value,
            startedAt=job.started_at,
            finishedAt=finished_at,
            exitCode=exit_code,
            progress=progress[-PROGRESS_LINES_SHOWN:],
            warnings=warnings,
            result=result,
            error=error,
        )

    def _work(self, job: Job) -> None:
        try:
            with self._runner.exclusive():
                self._set_state(job, JobState.RUNNING)
                outcome = self._runner.execute(job.argv, err=job.sink)
        except Exception as error:  # a bug in a command: `main` handles the failures it knows
            log.exception("the %s job %s crashed", job.kind.value, job.id)
            self._finish(job, JobState.FAILED, None, None, clean_text(f"{type(error).__name__}: {error}"))
            return
        result = _parse(outcome.out)
        if outcome.code == 0:
            self._finish(job, JobState.DONE, outcome.code, result, None)
            return
        warnings, _ = _split(outcome.err)
        self._finish(job, JobState.FAILED, outcome.code, result, "\n".join(warnings) or "cmdr failed")

    def _finish(self, job: Job, state: JobState, code: int | None, result: object, error: str | None) -> None:
        with self._guard:
            job.state, job.exit_code, job.result, job.error = state, code, result, error
            job.finished_at = self._stamp()
        job.finished.set()
        log.info("the %s job %s ended %s", job.kind.value, job.id, state.value)

    def _set_state(self, job: Job, state: JobState) -> None:
        with self._guard:
            job.state = state

    def _stamp(self) -> str:
        return self._now().isoformat(timespec="seconds")

    def _forget_old(self) -> None:
        finished = [job_id for job_id, job in self._jobs.items() if not job.active]
        for job_id in finished[: max(0, len(finished) - MAX_JOBS_KEPT)]:
            del self._jobs[job_id]


def _parse(out: str) -> object:
    if not out.strip():
        return None
    try:
        return clean_json(json.loads(out))
    except ValueError:
        return clean_text(out)


def _split(stderr: str) -> tuple[list[str], list[str]]:
    """The warnings, and then the progress lines, of what the command wrote to stderr."""
    warnings: list[str] = []
    progress: list[str] = []
    for raw in stderr.splitlines():
        line = clean_text(raw).strip()[:MAX_LINE_CHARS]
        if not line:
            continue
        (warnings if line.startswith(_WARNING_PREFIXES) else progress).append(line)
    return warnings, progress
