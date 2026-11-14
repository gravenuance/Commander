"""What every tool goes through: one `cmdr` command in, cleaned JSON or text out, failures as tool errors."""

import json
from typing import TypedDict

from mcp.server.mcpserver.exceptions import ToolError

from commander.mcp.jobs import Job, JobBoard, JobBusyError, JobKind, JobView, UnknownJobError
from commander.mcp.runner import Outcome, Runner, Services
from commander.mcp.text import clean_json, clean_text

MAX_WARNINGS = 20
MAX_WARNING_CHARS = 500
MAX_ERROR_CHARS = 4000
MAX_ERROR_OUTPUT_CHARS = 8000


class ToolResult(TypedDict):
    """What a tool answers: the command's data (parsed JSON, or its text), and its stderr warnings."""

    result: object
    warnings: list[str]


class Session:
    def __init__(self, services: Services | None = None) -> None:
        self.services = services or Services()
        self._runner = Runner(self.services)
        self.jobs = JobBoard(self._runner, self.services.now)

    def json_call(self, argv: list[str], *, stdin: str = "") -> ToolResult:
        """Run a command that ends in `--json` and answer its parsed output."""
        outcome = self._run(argv, stdin)
        try:
            document: object = json.loads(outcome.out)
        except ValueError:
            raise ToolError("cmdr printed output that is not JSON") from None
        return ToolResult(result=clean_json(document), warnings=_warnings(outcome))

    def text_call(self, argv: list[str], *, stdin: str = "") -> ToolResult:
        """Run a command that has no JSON form and answer its text."""
        outcome = self._run(argv, stdin)
        return ToolResult(result=clean_text(outcome.out), warnings=_warnings(outcome))

    def start_job(self, kind: JobKind, argv: list[str]) -> JobView:
        try:
            return self.jobs.view(self.jobs.start(kind, argv))
        except JobBusyError as busy:
            raise ToolError(str(busy)) from None

    def job(self, job_id: str) -> Job:
        try:
            return self.jobs.get(job_id)
        except UnknownJobError as unknown:
            raise ToolError(str(unknown)) from None

    def _run(self, argv: list[str], stdin: str) -> Outcome:
        running = self.jobs.running()
        if running is not None:
            raise ToolError(
                f"busy: the {running.kind.value} job {running.id} is running and holds every deck file; "
                "wait for job_status to report it done, then ask again"
            )
        with self._runner.exclusive():
            outcome = self._runner.execute(argv, stdin=stdin.encode("utf-8"))
        if outcome.code != 0:
            raise ToolError(_failure(outcome))
        return outcome


def _warnings(outcome: Outcome) -> list[str]:
    lines = (clean_text(line).strip() for line in outcome.err.splitlines())
    return [line[:MAX_WARNING_CHARS] for line in lines if line][:MAX_WARNINGS]


def _failure(outcome: Outcome) -> str:
    """The `cmdr: ...` message, then whatever the command still printed: a save that failed its check has
    already written, and a plan with problems lists them on stdout."""
    message = clean_text(outcome.err).strip()[:MAX_ERROR_CHARS] or "cmdr failed without a message"
    printed = clean_text(outcome.out).strip()
    if printed:
        message += f"\noutput:\n{printed[:MAX_ERROR_OUTPUT_CHARS]}"
    return f"{message}\n(exit code {outcome.code})"
