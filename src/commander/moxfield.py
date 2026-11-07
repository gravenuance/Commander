"""Other players' public Moxfield decks, read through Moxfield's undocumented JSON API (ADR 0066).

Moxfield has no public API and its API host's robots.txt disallows everything; the owner chose to read it
anyway, for a few paced requests a run that identify as cmdr, cached so each deck is fetched once. Nothing
here works around bot protection: a 403 (a block or a challenge) stops every Moxfield request for the run,
and the callers carry on with Archidekt alone. Every answer is shape-checked before use.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from commander.deckreads import DeckRead
from commander.jsonshape import (
    ShapeError,
    as_list,
    as_object,
    opt_int,
    opt_str,
    req_int,
    req_list,
    req_object,
    req_str,
)
from commander.models import plan_category
from commander.net import Fetcher, HttpStatusError, Pacer
from commander.peersearch import (
    MAX_DECK_READS,
    MAX_SEARCH_PAGES,
    Cursor,
    LimitGuard,
    PageRequest,
    PeerDeck,
    Site,
    descend,
    target,
)

log = logging.getLogger(__name__)

API_BASE = "https://api2.moxfield.com"
COMMANDER_FORMAT = "commander"
# As many decks a page as Archidekt's listings hold, so MAX_SEARCH_PAGES reaches as deep on both (ADR 0066).
SEARCH_PAGE_SIZE = 60
_FORBIDDEN = 403


class MoxfieldError(Exception):
    """Moxfield answered with something this tool cannot use."""


class MoxfieldBlockedError(Exception):
    """Moxfield refused cmdr's requests (a 403, as a block or a bot challenge answers); no more are sent this
    run. Not a MoxfieldError, so a caller skipping one unreadable deck never skips past a block."""


class MoxfieldSearch:
    """Other players' public Moxfield decks: a commander's card id, its most-viewed decks, and their lists."""

    def __init__(self, fetcher: Fetcher, pacer: Pacer, base_url: str = API_BASE) -> None:
        self._fetcher = fetcher
        self._base_url = base_url.rstrip("/")
        self._guard = LimitGuard(pacer, Site.MOXFIELD)
        self._blocked: str | None = None

    def commander_card_id(self, name: str) -> str | None:
        """Moxfield's id for the card named exactly `name` (a double-faced card by its front face too); None
        when Moxfield knows no such card."""
        front = name.split(" // ", maxsplit=1)[0]
        if '"' in front:
            log.info("not looking up %r on Moxfield: its name has a quote mark", name)
            return None
        raw = self._get(f"{self._base_url}/v2/cards/search", {"q": f'!"{front}"', "page": 1})
        try:
            return parse_card_id(raw, {name.casefold(), front.casefold()})
        except ShapeError as error:
            raise MoxfieldError(f"unexpected card search answer from Moxfield: {error}") from error

    def popular_decks(
        self,
        commander_card_id: str,
        *,
        brackets: Sequence[int | None],
        since: datetime,
        label: str,
        max_pages: int = MAX_SEARCH_PAGES,
        limit: int = MAX_DECK_READS,
        readable: Callable[[PeerDeck], bool] = lambda _: True,
    ) -> list[PeerDeck]:
        """Public Commander decks led by the card and updated at or after `since`, most viewed first per
        bracket (None searches every bracket), as `peersearch.descend` reads them; `label` names the search
        in the log."""
        cursors = [
            Cursor(
                (
                    f"{self._base_url}/v2/decks/search",
                    {
                        "pageNumber": 1,
                        "pageSize": SEARCH_PAGE_SIZE,
                        "sortType": "views",
                        "sortDirection": "Descending",
                        "fmt": COMMANDER_FORMAT,
                        "commanderCardId": commander_card_id,
                    }
                    | ({"minBracket": bracket, "maxBracket": bracket} if bracket is not None else {}),
                )
            )
            for bracket in brackets
        ]
        return descend(
            cursors,
            self._read_search_page,
            since=since,
            site=Site.MOXFIELD,
            label=label,
            max_pages=max_pages,
            limit=limit,
            readable=readable,
        )

    def _read_search_page(self, request: PageRequest) -> tuple[list[PeerDeck], PageRequest | None]:
        url, params = request
        raw = self._get(url, params)
        try:
            decks, more = parse_search_page(raw)
        except (ShapeError, ValueError) as error:
            raise MoxfieldError(f"unexpected search answer from Moxfield: {error}") from error
        if not more or params is None:
            return decks, None
        return decks, (url, {**params, "pageNumber": int(params["pageNumber"]) + 1})

    def deck(self, public_id: str) -> DeckRead:
        raw = self._get(f"{self._base_url}/v3/decks/all/{public_id}", None)
        try:
            return parse_deck(raw)
        except (ShapeError, ValueError) as error:
            raise MoxfieldError(f"unexpected data for Moxfield deck {public_id}: {error}") from error

    def _get(self, url: str, params: Mapping[str, str | int] | None) -> object:
        """One paced request, held by a 429 as `LimitGuard` says; after a 403 none goes out this run."""
        if self._blocked is not None:
            raise MoxfieldBlockedError(self._blocked)
        try:
            return self._guard.send(
                lambda: self._fetcher.get_json(url, params, surface_rate_limit=True), target(url, params)
            )
        except HttpStatusError as error:
            if error.status != _FORBIDDEN:
                raise
            self._blocked = "Moxfield refused cmdr's requests (HTTP 403)"
            log.warning("%s for %s; no more Moxfield requests are sent this run", self._blocked, url)
            raise MoxfieldBlockedError(self._blocked) from error


def parse_card_id(raw: object, names: set[str]) -> str | None:
    """The id of the search's exact match when its name (casefolded) is one of `names`."""
    found = as_object(raw, "cards").get("exactMatch")
    if found is None:
        return None
    card = as_object(found, "cards.exactMatch")
    if req_str(card, "name", "cards.exactMatch").casefold() not in names:
        return None
    return req_str(card, "id", "cards.exactMatch")


def parse_search_page(raw: object) -> tuple[list[PeerDeck], bool]:
    """The page's public Commander decks, and whether a page follows it."""
    page = as_object(raw, "page")
    decks: list[PeerDeck] = []
    for index, item in enumerate(req_list(page, "data", "page")):
        where = f"page.data[{index}]"
        deck = as_object(item, where)
        public = opt_str(deck, "visibility", where) == "public"
        if req_str(deck, "format", where) != COMMANDER_FORMAT or not public:
            continue
        updated_at = datetime.fromisoformat(req_str(deck, "lastUpdatedAtUtc", where))
        if updated_at.tzinfo is None:
            raise ShapeError(f"{where}.lastUpdatedAtUtc: no time zone")
        decks.append(
            PeerDeck(
                id=req_str(deck, "publicId", where),
                name=req_str(deck, "name", where),
                owner=req_str(req_object(deck, "createdByUser", where), "userName", f"{where}.createdByUser"),
                views=req_int(deck, "viewCount", where),
                updated_at=updated_at,
                bracket=opt_int(deck, "bracket", where),
                site=Site.MOXFIELD,
            )
        )
    more = req_int(page, "pageNumber", "page") < req_int(page, "totalPages", "page")
    return decks, more


def parse_deck(raw: object) -> DeckRead:
    """What the meta keeps of a Moxfield deck: its commanders and the main deck's cards, commanders included,
    with the cards its author tags with a word naming the plan ("combo", "wincon"; ADR 0017)."""
    deck = as_object(raw, "deck")
    if req_str(deck, "format", "deck") != COMMANDER_FORMAT:
        raise ShapeError("deck.format: not a Commander deck")
    boards = req_object(deck, "boards", "deck")
    commanders = _board(boards, "commanders")
    counts: dict[str, int] = {}
    for name, quantity in (*commanders, *_board(boards, "mainboard")):
        counts[name] = counts.get(name, 0) + quantity
    tags = _author_tags(deck)
    return DeckRead(
        commanders=tuple(sorted({name for name, _ in commanders}, key=str.casefold)),
        cards=tuple(sorted(counts)),
        planned=tuple(sorted(name for name in counts if any(map(plan_category, tags.get(name, ()))))),
        multiples=tuple(sorted((name, count) for name, count in counts.items() if count > 1)),
    )


def _board(boards: Mapping[str, object], key: str) -> list[tuple[str, int]]:
    where = f"deck.boards.{key}"
    cards = req_object(req_object(boards, key, "deck.boards"), "cards", where)
    found: list[tuple[str, int]] = []
    for entry_id, item in cards.items():
        place = f"{where}.cards.{entry_id}"
        entry = as_object(item, place)
        card = req_object(entry, "card", place)
        found.append((req_str(card, "name", f"{place}.card"), req_int(entry, "quantity", place)))
    return found


def _author_tags(deck: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    """The author's tags by card name; a deck without any may leave the field out."""
    raw = deck.get("authorTags")
    if raw is None:
        return {}
    tags: dict[str, tuple[str, ...]] = {}
    for name, names in as_object(raw, "deck.authorTags").items():
        items = as_list(names, f"deck.authorTags.{name}")
        if not all(isinstance(tag, str) for tag in items):
            raise ShapeError(f"deck.authorTags.{name}: expected tag names")
        tags[name] = tuple(str(tag) for tag in items)
    return tags
