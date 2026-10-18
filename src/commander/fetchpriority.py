"""Which deck the sync fetches next, when the user's attention moved to one (ADR 0034)."""

import threading
from collections.abc import Callable, Sequence


class FetchPriority:
    """The deck the user has open, which the sync fetches ahead of the rest.

    The app's bridge thread sets it as decks are opened; the sync thread reads it before it picks each deck.
    """

    def __init__(self, selected: int | None = None) -> None:
        self._lock = threading.Lock()
        self._selected = selected

    def prefer(self, deck_id: int) -> None:
        with self._lock:
            self._selected = deck_id

    def pick[T](self, waiting: Sequence[T], deck_id: Callable[[T], int]) -> T:
        """The preferred deck if it still waits, else the first of those waiting, which is in their order."""
        with self._lock:
            selected = self._selected
        return next((deck for deck in waiting if deck_id(deck) == selected), waiting[0])
