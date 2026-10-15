"""EDHTop16's public GraphQL API: a commander's tournament entries with decklists, and the top commanders.

Its terms (read 2026-10-15) offer the API as-is but forbid excessive automated requests, implying endorsement
and redistributing its statistics, so requests are paced and answers are cached outside the repo.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from urllib.parse import quote

from commander.jsonshape import (
    ShapeError,
    as_object,
    opt_str,
    req_bool,
    req_float,
    req_int,
    req_list,
    req_object,
    req_str,
)
from commander.net import Fetcher, Pacer

API_URL = "https://edhtop16.com/api/graphql"
SITE_URL = "https://edhtop16.com"
# The API's periods are 1, 3, 6 or 12 months, so a shorter window is cut out of three months here.
MAX_WINDOW_DAYS = 90
PAGE_SIZE = 50
MAX_PAGES = 8
# The terms forbid excessive requests; one every 1.5 seconds keeps a full run under a minute.
REQUEST_INTERVAL_SECONDS = 1.5
MAX_SPICE = 4
# Its decklist queries routinely take five to seven seconds; slower than this is worth a warning.
SLOW_QUERY_SECONDS = 20.0
# Asking for twice as many commanders as wanted leaves room to re-rank them by the shorter window.
_RANKING_SLACK = 2

_ENTRY_FIELDS = """
standing wins losses draws decklistUrl
tournament { TID name tournamentDate size topCut }
decklistItems { count spice card { name } }
"""

ENTRIES_QUERY = (
    """
query Entries($name: String!, $first: Int!, $after: String, $sortBy: EntriesSortBy!, $minEventSize: Int!) {
  commander(name: $name) {
    name
    entries(first: $first, after: $after, sortBy: $sortBy,
            filters: {timePeriod: THREE_MONTHS, minEventSize: $minEventSize}) {
      pageInfo { hasNextPage endCursor }
      edges { node {"""
    + _ENTRY_FIELDS
    + """} }
    }
  }
}
"""
)

COMMANDERS_QUERY = """
query Commanders($first: Int!, $minDate: String!, $minSize: Int!) {
  commanders(first: $first, sortBy: POPULARITY, timePeriod: THREE_MONTHS, minTournamentSize: $minSize) {
    edges { node { name stats(filters: {minDate: $minDate, minSize: $minSize}) {
      count topCuts conversionRate metaShare
    } } }
  }
}
"""


class Edhtop16Error(Exception):
    """EDHTop16 answered with something this tool cannot use."""


class EntrySort(Enum):
    """The API's entry orders: newest first, or best placings first."""

    NEW = "NEW"
    TOP = "TOP"


@dataclass(slots=True, frozen=True)
class DecklistCard:
    name: str
    count: int
    # 0 to 4: how unusual the card is among the commander's decklists, 4 being new or rarely played.
    spice: int


@dataclass(slots=True, frozen=True)
class Tournament:
    tid: str
    name: str
    held_on: date
    size: int
    top_cut: int


@dataclass(slots=True, frozen=True)
class Entry:
    standing: int
    wins: int
    losses: int
    draws: int
    decklist_url: str | None
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
    """The name EDHTop16 files a deck under: partners joined by " / " in alphabetical order."""
    if not commanders:
        raise ValueError("a deck needs a commander")
    return " / ".join(sorted(commanders))


def commander_url(name: str) -> str:
    return f"{SITE_URL}/commander/{quote(name, safe='')}"


class Edhtop16Client:
    def __init__(self, fetcher: Fetcher, pacer: Pacer, url: str = API_URL) -> None:
        self._fetcher = fetcher
        self._pacer = pacer
        self._url = url

    def entries(
        self, commander: str, *, since: date, min_event_size: int, sort: EntrySort, limit: int
    ) -> tuple[Entry, ...] | None:
        """The commander's entries from events on or after `since`, in `sort` order, or None for a commander
        EDHTop16 doesn't know. Newest-first reading stops at the first page that reaches past `since`;
        best-first reading reads one page of `limit` and keeps the entries inside the window."""
        found: list[Entry] = []
        after: str | None = None
        for _ in range(MAX_PAGES):
            variables: dict[str, object] = {
                "name": commander,
                "first": min(PAGE_SIZE, limit - len(found)),
                "after": after,
                "sortBy": sort.value,
                "minEventSize": min_event_size,
            }
            data = self._query(ENTRIES_QUERY, variables)
            try:
                page = parse_entries_page(data)
            except (ShapeError, ValueError) as error:
                raise Edhtop16Error(f"unexpected entries for {commander} from EDHTop16: {error}") from error
            if page is None:
                return None
            entries, after = page
            found.extend(entry for entry in entries if entry.tournament.held_on >= since)
            reached_past = any(entry.tournament.held_on < since for entry in entries)
            # Best-first reading takes one page: further pages only hold worse placings.
            if after is None or len(found) >= limit or sort is EntrySort.TOP or reached_past:
                break
        return tuple(found[:limit])

    def top_commanders(self, *, since: date, min_event_size: int, count: int) -> tuple[CommanderStats, ...]:
        """The `count` most-played commanders in events on or after `since`."""
        variables: dict[str, object] = {
            "first": count * _RANKING_SLACK,
            "minDate": since.isoformat(),
            "minSize": min_event_size,
        }
        data = self._query(COMMANDERS_QUERY, variables)
        try:
            ranked = parse_commanders(data)
        except (ShapeError, ValueError) as error:
            raise Edhtop16Error(f"unexpected commander ranking from EDHTop16: {error}") from error
        ranked.sort(key=lambda stats: (-stats.entries, stats.name))
        return tuple(ranked[:count])

    def _query(self, query: str, variables: Mapping[str, object]) -> Mapping[str, object]:
        self._pacer.wait()
        raw = self._fetcher.post_json(self._url, {"query": query, "variables": variables})
        try:
            return graphql_data(raw)
        except ShapeError as error:
            raise Edhtop16Error(f"unexpected answer from EDHTop16: {error}") from error


def graphql_data(raw: object) -> Mapping[str, object]:
    """The `data` of a GraphQL answer. A missing commander comes back as null data beside an error, which the
    caller reads as "not found"; errors without data mean the query itself was refused."""
    answer = as_object(raw, "answer")
    data = answer.get("data")
    if data is None:
        messages = [
            str(as_object(item, "answer.errors[]").get("message", "?"))
            for item in req_list(answer, "errors", "answer")
        ]
        raise ShapeError("no data; errors: " + "; ".join(messages)[:300])
    return as_object(data, "answer.data")


def parse_entries_page(data: Mapping[str, object]) -> tuple[list[Entry], str | None] | None:
    if data.get("commander") is None:
        return None
    commander = req_object(data, "commander", "data")
    connection = req_object(commander, "entries", "data.commander")
    where = "data.commander.entries"
    page_info = req_object(connection, "pageInfo", where)
    after = opt_str(page_info, "endCursor", f"{where}.pageInfo")
    has_next = req_bool(page_info, "hasNextPage", f"{where}.pageInfo")
    entries = [
        parse_entry(
            req_object(as_object(edge, f"{where}.edges[{i}]"), "node", f"{where}.edges[{i}]"), f"{where}[{i}]"
        )
        for i, edge in enumerate(req_list(connection, "edges", where))
    ]
    return entries, after if has_next else None


def parse_entry(node: Mapping[str, object], where: str) -> Entry:
    tournament = req_object(node, "tournament", where)
    at = f"{where}.tournament"
    cards: list[DecklistCard] = []
    for index, item in enumerate(req_list(node, "decklistItems", where)):
        line = as_object(item, f"{where}.decklistItems[{index}]")
        spice = req_int(line, "spice", f"{where}.decklistItems[{index}]")
        if not 0 <= spice <= MAX_SPICE:
            raise ShapeError(f"{where}.decklistItems[{index}].spice: {spice} is outside 0-{MAX_SPICE}")
        cards.append(
            DecklistCard(
                name=req_str(req_object(line, "card", f"{where}.decklistItems[{index}]"), "name", where),
                count=req_int(line, "count", f"{where}.decklistItems[{index}]"),
                spice=spice,
            )
        )
    return Entry(
        standing=req_int(node, "standing", where),
        wins=req_int(node, "wins", where),
        losses=req_int(node, "losses", where),
        draws=req_int(node, "draws", where),
        decklist_url=_site_link(opt_str(node, "decklistUrl", where)),
        tournament=Tournament(
            tid=req_str(tournament, "TID", at),
            name=req_str(tournament, "name", at),
            held_on=datetime.fromisoformat(req_str(tournament, "tournamentDate", at)).date(),
            size=req_int(tournament, "size", at),
            top_cut=req_int(tournament, "topCut", at),
        ),
        cards=tuple(cards),
    )


def _site_link(path: str | None) -> str | None:
    # Decklist links come relative to the site; anything else is not a link to follow.
    if path is None or not path.startswith("/") or path.startswith("//"):
        return None
    return SITE_URL + path


def parse_commanders(data: Mapping[str, object]) -> list[CommanderStats]:
    where = "data.commanders"
    ranked: list[CommanderStats] = []
    for index, edge in enumerate(req_list(req_object(data, "commanders", "data"), "edges", where)):
        node = req_object(as_object(edge, f"{where}.edges[{index}]"), "node", f"{where}.edges[{index}]")
        at = f"{where}[{index}]"
        stats = req_object(node, "stats", at)
        ranked.append(
            CommanderStats(
                name=req_str(node, "name", at),
                entries=req_int(stats, "count", f"{at}.stats"),
                top_cuts=req_int(stats, "topCuts", f"{at}.stats"),
                conversion=req_float(stats, "conversionRate", f"{at}.stats"),
                meta_share=req_float(stats, "metaShare", f"{at}.stats"),
            )
        )
    return ranked
