"""The rules every peer-deck search follows, whichever site it reads (Archidekt, Moxfield; ADR 0066).

A site lists Commander decks most viewed first, a bracket at a time; the searches go down the view floors
together and stop as soon as more pages cannot change which decks are read (ADR 0050). Each site's client
sends its requests through its own pacer and `LimitGuard`, so one site's limits never hold the other's.
"""

import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from commander.models import ARCHIDEKT_DECK_URL
from commander.net import Pacer, RateLimitedError

log = logging.getLogger(__name__)

# At most this many search pages per bracket searched (ADR 0050), and a request every 6 seconds, the ten a
# minute Archidekt answers before it starts sending 429s (ADR 0048, which says to go back to 6.5 on any 429
# in the log). Moxfield gets the same pace (ADR 0066).
MAX_SEARCH_PAGES = 5
SEARCH_INTERVAL_SECONDS = 6.0
# After a 429 a site gets this long without a request, or the Retry-After if that is longer; Archidekt's
# limit lifts within the minute.
RATE_LIMIT_PAUSE_SECONDS = 60.0
# A request that was rate limited is tried again this many times after the pause.
RATE_LIMIT_RETRIES = 1
# The top of the bracket scale.
TOP_BRACKET = 5
# Which decks are read: every one with at least the first view count of VIEW_FLOORS that gives MIN_DECKS
# decks, else every one with at least the last; at most MAX_DECK_READS of them, most viewed first.
VIEW_FLOORS = (1000, 750, 500)
MIN_DECKS = 10
MAX_DECK_READS = 60
# A commander the profile names as playing alike is searched smaller than the deck's own: this many pages per
# bracket, and a quarter of the deck reads (rounded up).
ALIKE_SEARCH_PAGES_PER_BRACKET = 3
ALIKE_READ_DIVISOR = 4

MOXFIELD_DECK_URL = "https://moxfield.com/decks/{id}"


class Site(Enum):
    """Where a peer deck is listed."""

    ARCHIDEKT = "Archidekt"
    MOXFIELD = "Moxfield"


@dataclass(slots=True, frozen=True)
class PeerDeck:
    """Another player's deck as a site's search lists it: Archidekt by its number, Moxfield by its public
    id."""

    id: int | str
    name: str
    owner: str
    views: int
    updated_at: datetime
    bracket: int | None
    site: Site = Site.ARCHIDEKT

    def __post_init__(self) -> None:
        if (self.site is Site.ARCHIDEKT) != isinstance(self.id, int) or self.id == "":
            raise ValueError(f"{self.site.value} deck id {self.id!r} has the wrong kind")

    @property
    def url(self) -> str:
        template = ARCHIDEKT_DECK_URL if self.site is Site.ARCHIDEKT else MOXFIELD_DECK_URL
        return template.format(id=self.id)

    @property
    def rank(self) -> tuple[int, int, str]:
        """Most viewed first; ties by id, Archidekt's numerically."""
        number = self.id if isinstance(self.id, int) else 0
        text = self.id if isinstance(self.id, str) else ""
        return (-self.views, number, text)


# A search page to read: its URL and, for the first page of an Archidekt search, its query.
PageRequest = tuple[str, Mapping[str, str | int] | None]
# Reads one page: the decks on it and the page after it, None at the end.
PageReader = Callable[[PageRequest], tuple[list[PeerDeck], PageRequest | None]]


class LimitGuard:
    """Sends a site's reads through a pacer and keeps away after a 429.

    After a 429 nothing goes out for RATE_LIMIT_PAUSE_SECONDS or the Retry-After if longer, which holds the
    next request of any caller of the pacer; the limited one is tried again after it. A request limited again
    gives up: every later request of this guard fails at once, so callers fall back to their cache without
    more waiting.
    """

    def __init__(self, pacer: Pacer, site: Site = Site.ARCHIDEKT) -> None:
        self.pacer = pacer
        self._site = site.value
        self._gave_up: RateLimitedError | None = None

    def send[T](self, send: Callable[[], T], target: str) -> T:
        """`send` is a fetcher read made with surface_rate_limit=True."""
        if self._gave_up is not None:
            raise RateLimitedError(str(self._gave_up), self._gave_up.retry_after)
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            self.pacer.wait()
            started = time.perf_counter()
            try:
                return send()
            except RateLimitedError as error:
                if attempt == RATE_LIMIT_RETRIES:
                    log.warning(
                        "%s answered 429 again for %s; no more requests are sent this run", self._site, target
                    )
                    self._gave_up = error
                    raise
                pause = max(RATE_LIMIT_PAUSE_SECONDS, error.retry_after or 0.0)
                log.warning(
                    "%s answered 429 (too many requests) for %s; waiting %.0fs before the next one",
                    self._site,
                    target,
                    pause,
                )
                self.pacer.cool_down(pause)
            finally:
                log.debug("%s request %s took %.2fs", self._site, target, time.perf_counter() - started)
        raise AssertionError("unreachable: the last attempt raises")  # pragma: no cover


class Cursor:
    """Where one bracket's view-ordered listing has been read to."""

    def __init__(self, first: PageRequest) -> None:
        self.next: PageRequest | None = first
        self.pages = 0
        # The fewest views on the last page read; None before the first.
        self.lowest: int | None = None

    def pending(self, floor: int) -> PageRequest | None:
        """The page to read next to find every deck with at least `floor` views, if there is one: the listing
        is most viewed first, so a page ending under the floor has every such deck above it."""
        if self.next is None or (self.lowest is not None and self.lowest < floor):
            return None
        return self.next

    def settled(self, readable: Sequence[PeerDeck], floor: int, limit: int) -> bool:
        """Whether no deck further down this listing can change what `most_viewed(readable, limit=limit)`
        takes: at least max(limit, MIN_DECKS) of the decks reach `floor`, so the bar is `floor`, and the
        `limit`-th most viewed has more views than the last deck read, so every later deck is below it. The
        views must be strictly more, because a later deck with as many could win the tie by its id."""
        if self.lowest is None or limit < 1:
            return False
        views = sorted((deck.views for deck in readable if deck.views >= floor), reverse=True)
        return len(views) >= max(limit, MIN_DECKS) and views[limit - 1] > self.lowest


def descend(
    cursors: Sequence[Cursor],
    read_page: PageReader,
    *,
    since: datetime,
    site: Site,
    label: str,
    max_pages: int,
    limit: int,
    readable: Callable[[PeerDeck], bool],
) -> list[PeerDeck]:
    """The decks updated at or after `since` that the cursors' listings show, read down the view floors
    together: to the first floor that MIN_DECKS of the decks found reach, and never below the last, so a
    deck under it is never listed. Each listing pages at most `max_pages` pages, and stops earlier once
    `most_viewed(..., limit=limit)` over the `readable` decks cannot change (`Cursor.settled`), so the decks
    to read are the same as from the full descent (ADR 0050). `label` names the search in the log."""
    found: dict[int | str, PeerDeck] = {}
    pages = 0
    cut_short = False
    for floor in VIEW_FLOORS:
        for cursor in cursors:
            while (request := cursor.pending(floor)) is not None:
                if cursor.pages == max_pages:
                    cut_short = True
                    break
                cursor.pages += 1
                pages += 1
                page, cursor.next = read_page(request)
                cursor.lowest = min((deck.views for deck in page), default=0)
                found.update((deck.id, deck) for deck in page if deck.updated_at >= since)
                if cursor.settled([d for d in found.values() if readable(d)], floor, limit):
                    break
        if reaches_floor(found.values(), floor):
            break
    log.info(
        "%s search for %s by views: %d pages, %d decks in the window", site.value, label, pages, len(found)
    )
    if cut_short:
        log.info(
            "%s search for %s by views was cut short at %d pages a bracket", site.value, label, max_pages
        )
    return list(found.values())


def target(url: str, params: Mapping[str, str | int] | None) -> str:
    """The request as the log names it."""
    import httpx  # noqa: PLC0415

    return f"{url}?{httpx.QueryParams(params)}" if params else url


def brackets_to_read(bracket: int | None) -> tuple[int | None, ...]:
    """A deck's own bracket and the one above it, whose decks play what this one could grow into; None (any
    bracket) stays one search, and the top bracket has none above."""
    if bracket is None or bracket >= TOP_BRACKET:
        return (bracket,)
    return (bracket, bracket + 1)


def reaches_floor(decks: Iterable[PeerDeck], floor: int) -> bool:
    """Whether at least MIN_DECKS of the decks have `floor` views or more."""
    return sum(deck.views >= floor for deck in decks) >= MIN_DECKS


def most_viewed(decks: Sequence[PeerDeck], *, limit: int = MAX_DECK_READS) -> list[PeerDeck]:
    """The decks worth reading, most viewed first: those with at least 1000 views, or 750, or 500 when fewer
    than MIN_DECKS reach the bar; when fewer than MIN_DECKS reach even 500, those that do, however few."""
    ranked = sorted(decks, key=lambda deck: deck.rank)
    floor = next((bar for bar in VIEW_FLOORS if reaches_floor(ranked, bar)), VIEW_FLOORS[-1])
    return [deck for deck in ranked if deck.views >= floor][:limit]
