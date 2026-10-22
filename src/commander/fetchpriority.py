"""Which deck the sync fetches next, when the user's attention moved to one (ADR 0034)."""

import threading
from collections.abc import Callable, Sequence
from contextvars import ContextVar

# The deck a read is for, set by the thread that reads it; the threads it starts inherit it (`background`).
# A request made with none, like the Meta panel's, is no deck's.
fetching_deck: ContextVar[int | None] = ContextVar("fetching_deck", default=None)


class FetchPriority:
    """The deck the user has open, which the sync fetches ahead of the rest.

    The app's bridge thread sets it as decks are opened; the sync thread reads it before it picks each deck.
    """

    def __init__(self, selected: int | None = None) -> None:
        self._lock = threading.Lock()
        self._selected = selected
        self._listeners: list[Callable[[], None]] = []

    def watch(self, listener: Callable[[], None]) -> None:
        """Call `listener` after each change of preference, for as long as this object lives (one sync)."""
        with self._lock:
            self._listeners.append(listener)

    def prefer(self, deck_id: int) -> None:
        with self._lock:
            self._selected = deck_id
            listeners = tuple(self._listeners)
        # Outside the lock: a listener may take its own, which is held while it reads `rank`.
        for listener in listeners:
            listener()

    def rank(self, deck_id: int, order: Sequence[int]) -> int:
        """Where the deck stands for requests that wait their turn, lowest first: the preferred deck, then the
        decks in `order`, then any not in it. Read afresh each time, so a deck opened later moves up."""
        with self._lock:
            selected = self._selected
        if deck_id == selected:
            return 0
        return 1 + (order.index(deck_id) if deck_id in order else len(order))

    def pick[T](self, waiting: Sequence[T], deck_id: Callable[[T], int]) -> T:
        """The preferred deck if it still waits, else the first of those waiting, which is in their order."""
        with self._lock:
            selected = self._selected
        return next((deck for deck in waiting if deck_id(deck) == selected), waiting[0])
