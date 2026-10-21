"""Archidekt search hits kept between runs, so a refresh pages only through what changed (ADR 0027).

A search lists decks newest-updated first, so every deck above the newest one already held is new or edited.
"""

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from commander.archidekt import PeerDeck
from commander.peerschedule import full_search_due

log = logging.getLogger(__name__)

# Pages through the listing, stopping per bracket at the given time; None pages to the end of the window.
Searcher = Callable[[Mapping[int | None, datetime] | None], Sequence[PeerDeck]]


@dataclass(slots=True, frozen=True)
class SearchHits:
    """What a search found: every deck it listed in the window, with the view counts it saw, and when the
    listing was last paged in full."""

    decks: tuple[PeerDeck, ...]
    full_at: datetime


def update_hits(
    previous: SearchHits | None,
    search: Searcher,
    *,
    item: str,
    brackets: Sequence[int | None],
    since: datetime,
    now: datetime,
) -> SearchHits:
    """The hits now: a full search when there are none yet or the group's weekly full search is due, else
    the cached hits still in the window with the new and edited decks laid over them by deck id."""
    if previous is None or full_search_due(item, previous.full_at, now):
        decks = tuple(search(None))
        log.info("Archidekt search for %s: full search, %d decks", item, len(decks))
        return SearchHits(decks, now)
    kept = {deck.id: deck for deck in previous.decks if deck.updated_at >= since}
    fresh = search(newest_per_bracket(kept.values(), brackets))
    log.info(
        "Archidekt search for %s paged through the new decks only: %d new or edited, %d kept",
        item,
        len(fresh),
        len(kept),
    )
    kept.update((deck.id, deck) for deck in fresh)
    ordered = sorted(kept.values(), key=lambda deck: (-deck.updated_at.timestamp(), deck.id))
    return SearchHits(tuple(ordered), previous.full_at)


def newest_per_bracket(
    decks: Iterable[PeerDeck], brackets: Sequence[int | None]
) -> dict[int | None, datetime]:
    """The newest update among the decks of each searched bracket (of every deck for a search of any
    bracket); a bracket with no deck is left out and so is searched to the end of the window."""
    held = list(decks)
    newest: dict[int | None, datetime] = {}
    for bracket in brackets:
        times = [deck.updated_at for deck in held if bracket is None or deck.bracket == bracket]
        if times:
            newest[bracket] = max(times)
    return newest
