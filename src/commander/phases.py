"""The phases of a sync that run at once, shown as the one line the app and the terminal have room for."""

import threading
from collections.abc import Callable, Sequence

SEPARATOR = " · "
# The most words a status line holds (ui.md); past it, the first phase and a count of the others.
MAX_WORDS = 12


class ActivePhases:
    """Keeps the phases that are running, in the order they started, and tells `show` the line for them.

    Phases are told apart by their text. Two of the same text running at once count twice. When the last one
    ends, `idle` is shown, so the line never names work that is over.
    """

    def __init__(self, show: Callable[[str], None], *, idle: str) -> None:
        self._show = show
        self._idle = idle
        self._steps: list[str] = []
        self._lock = threading.Lock()

    def start(self, step: str) -> None:
        with self._lock:
            self._steps.append(step)
            self._show(_line(self._steps))

    def done(self, step: str) -> None:
        with self._lock:
            if step not in self._steps:
                return
            self._steps.remove(step)
            self._show(_line(self._steps) if self._steps else self._idle)

    def replace(self, old: str, new: str) -> bool:
        """Put `new` in the place of the running phase `old`; False, and nothing changed, if `old` is not
        running."""
        with self._lock:
            if old not in self._steps:
                return False
            self._steps[self._steps.index(old)] = new
            self._show(_line(self._steps))
            return True


def _line(steps: Sequence[str]) -> str:
    if len(steps) == 1 or sum(len(step.split()) for step in steps) <= MAX_WORDS:
        return SEPARATOR.join(steps)
    return f"{steps[0]} +{len(steps) - 1} more"
