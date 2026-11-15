"""Runs one `cmdr` command in this process, the way the tools do."""

import io
import logging
import sys
import threading
import time
from collections.abc import Generator, Mapping
from contextlib import contextmanager, redirect_stdout
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime

from commander.cli import main
from commander.clibase import ClientFactory, Clock, PacerFactory
from commander.credentials import TokenStore
from commander.net import Pacer, make_client

log = logging.getLogger(__name__)

MAX_LOGGED_STDERR = 1000


# The stderr of the command whose code is running in this context, which worker threads it starts inherit.
_running_for: ContextVar["CommandStderr | None"] = ContextVar("cmdr_mcp_running_for", default=None)


class CommandStderr(io.StringIO):
    """What a command writes to stderr, and can be read meanwhile.

    `main` points the root logger at it, so while the command runs every thread's log records would land
    here, the SDK's and httpx's included. Writes from code the command did not start are dropped then.
    """

    def __init__(self) -> None:
        super().__init__()
        self.running = False

    def write(self, s: str, /) -> int:
        if self.running and _running_for.get() is not self:
            return len(s)
        return super().write(s)


class ProgressSink(CommandStderr):
    """A stderr that looks like a terminal, so `cmdr` prints its progress lines."""

    def isatty(self) -> bool:
        return True


@dataclass(slots=True, frozen=True)
class Outcome:
    code: int
    out: str
    err: str


def _no_prompt(_text: str) -> str:
    # No tool signs in: a password is never asked for, and so never passes through here.
    raise RuntimeError("cmdr-mcp never asks for a password")


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True, frozen=True)
class Services:
    """What `cmdr.main` takes from outside; tests swap the network, clock and secret store."""

    env: Mapping[str, str] | None = None
    client_factory: ClientFactory = make_client
    token_store: TokenStore | None = None
    now: Clock = _utc_now
    pacer_factory: PacerFactory = Pacer


class Runner:
    """Calls `cmdr.main` with captured output, one command at a time.

    `main` sets up the root logger afresh on every call (`basicConfig(force=True)`), so two commands at once
    would take each other's handler, and one command would drop the server's own. Commands therefore take
    turns, and the root logger is put back as it was after each. The server's log file hangs on the
    `commander.mcp` logger, which the root logger never touches.
    """

    def __init__(self, services: Services | None = None) -> None:
        self._services = services or Services()
        self._turn = threading.Lock()

    @contextmanager
    def exclusive(self) -> Generator[None]:
        with self._turn:
            yield

    @contextmanager
    def try_exclusive(self, wait_seconds: float) -> Generator[bool]:
        """Wait up to the time for the turn; yields whether it is held, and releases it on exit if so."""
        held = self._turn.acquire(timeout=wait_seconds)
        try:
            yield held
        finally:
            if held:
                self._turn.release()

    def execute(self, argv: list[str], *, stdin: bytes = b"", err: CommandStderr | None = None) -> Outcome:
        """Run the command; the caller holds `exclusive()`. `err` is where stderr goes, default a buffer."""
        out = io.StringIO()
        sink = err if err is not None else CommandStderr()
        started = time.monotonic()
        # stdout carries the protocol: a stray print anywhere under main lands on stderr instead.
        with _root_logging_kept(), redirect_stdout(sys.stderr), _owning(sink):
            code = main(
                argv,
                env=self._services.env,
                client_factory=self._services.client_factory,
                stdout=out,
                stderr=sink,
                now=self._services.now,
                token_store=self._services.token_store,
                prompt_password=_no_prompt,
                stdin=io.BytesIO(stdin),
                pacer_factory=self._services.pacer_factory,
            )
        outcome = Outcome(code, out.getvalue(), sink.getvalue())
        log.log(
            logging.INFO if code == 0 else logging.WARNING,
            "cmdr %s finished with exit code %d in %.1fs",
            " ".join(argv[:3]),
            code,
            time.monotonic() - started,
        )
        if code != 0 and outcome.err.strip():
            log.warning("cmdr %s said: %s", argv[0], outcome.err.strip()[:MAX_LOGGED_STDERR])
        return outcome


@contextmanager
def _owning(sink: CommandStderr) -> Generator[None]:
    token = _running_for.set(sink)
    sink.running = True
    try:
        yield
    finally:
        sink.running = False
        _running_for.reset(token)


@contextmanager
def _root_logging_kept() -> Generator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        yield
    finally:
        for added in [h for h in root.handlers if h not in handlers]:
            root.removeHandler(added)
        for kept in handlers:
            if kept not in root.handlers:
                root.addHandler(kept)
        root.setLevel(level)
