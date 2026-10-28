"""The phases of a sync that run at once, shown as the one line the app and the terminal have room for."""

import threading
from collections.abc import Callable, Sequence

SEPARATOR = " · "
# The most words a status line holds (ui.md); past it, the first phase, cut short if need be, and a count of
# the others.
MAX_WORDS = 12
ELLIPSIS = "…"


class ActivePhases:
    """Keeps the phases that are running, in the order they started, and tells `show` the line for them.

    Phases are told apart by their text. Two of the same text running at once count twice. When the last one
    ends, `idle` is shown, so the line never names work that is over. The phase whose request went out last
    (`sending`) leads the line, because the others may only be queuing behind it.
    """

    def __init__(self, show: Callable[[str], None], *, idle: str) -> None:
        self._show = show
        self._idle = idle
        self._steps: list[str] = []
        self._sending: str | None = None
        self._lock = threading.Lock()

    def start(self, step: str) -> None:
        with self._lock:
            self._steps.append(step)
            self._show(_line(self._in_line_order()))

    def done(self, step: str) -> None:
        with self._lock:
            if step not in self._steps:
                return
            self._steps.remove(step)
            if self._sending == step and step not in self._steps:
                self._sending = None
            self._show(_line(self._in_line_order()) if self._steps else self._idle)

    def sending(self, step: str) -> None:
        """Lead the line with the running phase `step`, whose request has just gone out; nothing changes, and
        nothing is shown, if it is not running or already leads."""
        with self._lock:
            if step not in self._steps:
                return
            before = self._in_line_order()
            self._sending = step
            after = self._in_line_order()
            if after != before:
                self._show(_line(after))

    def replace(self, old: str, new: str) -> bool:
        """Put `new` in the place of the running phase `old`; False, and nothing changed, if `old` is not
        running."""
        with self._lock:
            if old not in self._steps:
                return False
            self._steps[self._steps.index(old)] = new
            if self._sending == old:
                self._sending = new
            self._show(_line(self._in_line_order()))
            return True

    def _in_line_order(self) -> list[str]:
        if self._sending is None:
            return list(self._steps)
        rest = list(self._steps)
        rest.remove(self._sending)
        return [self._sending, *rest]


def _line(steps: Sequence[str]) -> str:
    if sum(len(step.split()) for step in steps) <= MAX_WORDS:
        return SEPARATOR.join(steps)
    if len(steps) == 1:
        return _cut(steps[0], MAX_WORDS)
    more = f"+{len(steps) - 1} more"
    return f"{_cut(steps[0], MAX_WORDS - len(more.split()))} {more}"


def _cut(step: str, words: int) -> str:
    kept = step.split()
    if len(kept) <= words:
        return step
    return " ".join(kept[:words]) + ELLIPSIS
