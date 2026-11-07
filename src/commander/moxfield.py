"""Other players' public Moxfield decks, read through Moxfield's undocumented JSON API (ADR 0066).

Moxfield has no public API and its API host's robots.txt disallows everything; the owner chose to read it
anyway, for a few paced requests a run that identify as cmdr, cached so each deck is fetched once. Nothing
here works around bot protection: a 403 or a challenge page on the card lookup or a search stops every
Moxfield request for the run, and the callers carry on with the decks they have. A challenge page on a deck
read does the same; a 403 on one costs that deck alone, until three deck reads in a row are refused. Every
answer is shape-checked before use.
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
from commander.net import Fetcher, HttpStatusError, NotJsonError, Pacer
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
# Private decks answer 403, so a few refused deck reads in a row are not yet a block.
DECK_REFUSALS_TO_BLOCK = 3
_REFUSED = "Moxfield refused cmdr's requests (HTTP 403)"
_CHALLENGE = "Moxfield answered cmdr's request with a challenge page, not JSON"


class MoxfieldError(Exception):
    """Moxfield answered with something this tool cannot use."""


class MoxfieldBlockedError(Exception):
    """Moxfield refused cmdr's requests (a 403 or a challenge page, as a block or a bot challenge answers; for
    deck reads, a challenge page or three 403s in a row); no more requests are sent this run. Not a
    MoxfieldError, so a caller skipping one unreadable deck never skips past a block."""


class MoxfieldSearch:
    """Other players' public Moxfield decks: a commander's card id, its most-viewed decks, and their lists."""

    def __init__(self, fetcher: Fetcher, pacer: Pacer, base_url: str = API_BASE) -> None:
        self._fetcher = fetcher
        self._base_url = base_url.rstrip("/")
        self._guard = LimitGuard(pacer, Site.MOXFIELD)
        self._blocked: str | None = None
        self._deck_refusals = 0

    def commander_card_id(self, name: str) -> str | None:
        """Moxfield's id for the card named exactly `name`, a double-faced card by both its faces (its search
        matches no front face alone); None when Moxfield knows no such card."""
        if '"' in name:
            log.info("not looking up %r on Moxfield: its name has a quote mark", name)
            return None
        raw = self._get(f"{self._base_url}/v2/cards/search", {"q": f'!"{name}"', "page": 1})
        try:
            return parse_card_id(raw, name)
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
        """A deck's list. A 403 here is that deck's alone (a private deck answers 403) and raises a
        MoxfieldError, until `DECK_REFUSALS_TO_BLOCK` in a row block the run; a challenge page blocks it at
        once."""
        raw = self._get(f"{self._base_url}/v3/decks/all/{public_id}", None, deck=True)
        try:
            return parse_deck(raw)
        except (ShapeError, ValueError) as error:
            raise MoxfieldError(f"unexpected data for Moxfield deck {public_id}: {error}") from error

    def _get(self, url: str, params: Mapping[str, str | int] | None, *, deck: bool = False) -> object:
        """One paced request, held by a 429 as `LimitGuard` says. A refusal (`_refusal`) stops every later
        request this run, except a 403 of a deck read (`deck`), which fails that read alone until
        `DECK_REFUSALS_TO_BLOCK` in a row; a deck read that succeeds starts that count again."""
        if self._blocked is not None:
            raise MoxfieldBlockedError(self._blocked)
        try:
            answer = self._guard.send(
                lambda: self._fetcher.get_json(url, params, surface_rate_limit=True), target(url, params)
            )
        except (HttpStatusError, NotJsonError) as error:
            refusal = _refusal(error)
            if refusal is None:
                raise
            if deck and isinstance(error, HttpStatusError):
                self._deck_refusals += 1
                if self._deck_refusals < DECK_REFUSALS_TO_BLOCK:
                    raise MoxfieldError(f"{refusal} for {url}") from error
            self._blocked = refusal
            log.warning("%s for %s; no more Moxfield requests are sent this run", refusal, url)
            raise MoxfieldBlockedError(refusal) from error
        if deck:
            self._deck_refusals = 0
        return answer


def _refusal(error: HttpStatusError | NotJsonError) -> str | None:
    """What a block or a bot challenge says: a 403, or a 200 that is a page and not JSON; None for any other
    failure."""
    if isinstance(error, NotJsonError):
        return _CHALLENGE
    return _REFUSED if error.status == _FORBIDDEN else None


def parse_card_id(raw: object, name: str) -> str | None:
    """The id of the search's exact match when it is the card named `name` (in any case)."""
    found = as_object(raw, "cards").get("exactMatch")
    if found is None:
        return None
    card = as_object(found, "cards.exactMatch")
    if req_str(card, "name", "cards.exactMatch").casefold() != name.casefold():
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
