"""Tournament results as the meta reads them: an event, and the entries that finished in it with their cards.

Where they come from sits behind `TournamentSource`; `topdeck.py` is the one provider.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Protocol


class EntrySort(Enum):
    """The orders a commander's entries are read in: newest event first, or best placings first."""

    NEW = "NEW"
    TOP = "TOP"


@dataclass(slots=True, frozen=True)
class Tournament:
    tid: str
    held_on: date
    size: int
    top_cut: int


@dataclass(slots=True, frozen=True)
class DecklistCard:
    name: str
    count: int


@dataclass(slots=True, frozen=True)
class Entry:
    # 1 for the winner; the order of the event's standings.
    standing: int
    tournament: Tournament
    cards: tuple[DecklistCard, ...]

    @property
    def made_top_cut(self) -> bool:
        return 0 < self.standing <= self.tournament.top_cut

    @property
    def placing(self) -> float:
        """Where the entry finished as a share of the field: 0 is first, near 1 is last."""
        return (self.standing - 1) / max(self.tournament.size, 1)


@dataclass(slots=True, frozen=True)
class CommanderStats:
    name: str
    entries: int
    top_cuts: int
    conversion: float
    meta_share: float


def commander_name(commanders: Sequence[str]) -> str:
    """The name a commander's lists are filed under: partners joined by " / " in alphabetical order."""
    if not commanders:
        raise ValueError("a deck needs a commander")
    return " / ".join(sorted(commanders))


class TournamentSource(Protocol):
    """Where tournament lists and the shares of the commanders played come from."""

    def entries(
        self, commander: str, *, since: date, until: date, min_event_size: int, sort: EntrySort, limit: int
    ) -> tuple[Entry, ...] | None:
        """The commander's entries from events held from `since` to `until` (both inclusive), in `sort`
        order, at most `limit`; None for a commander no event in the window had."""
        ...

    def top_commanders(
        self, *, since: date, until: date, min_event_size: int, count: int
    ) -> tuple[CommanderStats, ...]:
        """The `count` most-played commanders in events from `since` to `until`."""
        ...
