"""One lock per deck folder, so the app's writes and a sync's pull and ideas merge never interleave on a deck
(ADR 0042).

The board's own lock file (ADR 0006) covers one read-modify-write of board.json; this covers a whole
write, such as a save that reads the board, writes to Archidekt and then rewrites the deck folder.
"""

import threading
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import Protocol


class DeckLock(Protocol):
    def __call__(self, deck_id: int, /) -> AbstractContextManager[object]: ...


def no_deck_lock(_deck_id: int, /) -> AbstractContextManager[object]:
    """For a caller that writes alone, such as `cmdr sync`."""
    return nullcontext()


class DeckLocks:
    """A lock for each deck id, made on first use and kept for the life of the app."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        # Re-entrant, so a write that ends up calling another on the same deck cannot hang its own thread.
        self._locks: dict[int, threading.RLock] = {}

    @contextmanager
    def hold(self, deck_id: int) -> Generator[None]:
        with self._guard:
            lock = self._locks.setdefault(deck_id, threading.RLock())
        with lock:
            yield
