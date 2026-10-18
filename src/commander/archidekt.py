"""Read access to Archidekt's undocumented JSON API; every response is shape-checked before use."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from commander.jsonshape import (
    ShapeError,
    as_object,
    opt_int,
    opt_str,
    req_bool,
    req_int,
    req_list,
    req_object,
    req_str,
    str_list,
)
from commander.models import ARCHIDEKT_DECK_URL, Deck, DeckCard, DeckSummary
from commander.net import Fetcher, Pacer

API_BASE = "https://archidekt.com/api"
_PLAIN_API_BASE = "http://archidekt.com/api"
COMMANDER_FORMAT = 3
PAGE_SIZE = 50
MAX_PAGES = 20
# Reading other players' decks: newest first, at most this many search pages per commander, one request every
# five seconds.
MAX_SEARCH_PAGES = 20
SEARCH_INTERVAL_SECONDS = 5.0
# The top of Archidekt's bracket scale.
TOP_BRACKET = 5
# Which decks are read: every one with at least the first view count of VIEW_FLOORS that gives MIN_DECKS
# decks, else every deck found; at most MAX_DECK_READS of them, most viewed first.
VIEW_FLOORS = (1000, 750, 500)
MIN_DECKS = 10
MAX_DECK_READS = 60


class ArchidektError(Exception):
    """Archidekt answered with something this tool cannot use."""


class ArchidektClient:
    def __init__(self, fetcher: Fetcher, base_url: str = API_BASE) -> None:
        self._fetcher = fetcher
        self._base_url = base_url.rstrip("/")

    def list_commander_decks(self, user: str) -> list[DeckSummary]:
        """Every public Commander deck the user owns, following pagination."""
        if not user:
            raise ValueError("user is empty")
        url: str | None = f"{self._base_url}/decks/v3/"
        params: Mapping[str, str | int] | None = {"ownerUsername": user, "pageSize": PAGE_SIZE}
        decks: list[DeckSummary] = []
        for _ in range(MAX_PAGES):
            if url is None:
                return decks
            raw = self._fetcher.get_json(url, params)
            try:
                page, url = parse_deck_page(raw)
            except ShapeError as error:
                raise ArchidektError(f"unexpected deck list from Archidekt: {error}") from error
            # The "next" link already carries the query string.
            params = None
            decks.extend(page)
        raise ArchidektError(f"deck list for {user} is longer than {MAX_PAGES} pages")

    def get_deck(self, deck_id: int) -> Deck:
        raw = self._fetcher.get_json(f"{self._base_url}/decks/{deck_id}/")
        try:
            return parse_deck(raw)
        except (ShapeError, ValueError) as error:
            raise ArchidektError(f"unexpected data for deck {deck_id}: {error}") from error


@dataclass(slots=True, frozen=True)
class PeerDeck:
    """Another player's deck as the search lists it."""

    id: int
    name: str
    owner: str
    views: int
    updated_at: datetime
    bracket: int | None

    @property
    def url(self) -> str:
        return ARCHIDEKT_DECK_URL.format(id=self.id)


class ArchidektSearch:
    """Other players' public decks.

    Archidekt's written terms forbid automated searches, but its co-founder has said the API is open to use
    (ADR 0004), so this stays at a few paced requests a run and callers cache what it finds.
    """

    def __init__(self, fetcher: Fetcher, pacer: Pacer, base_url: str = API_BASE) -> None:
        self._fetcher = fetcher
        self._pacer = pacer
        self._base_url = base_url.rstrip("/")
        self._decks = ArchidektClient(fetcher, base_url)

    def recent_decks(
        self, commander: str, *, brackets: Sequence[int | None], since: datetime
    ) -> list[PeerDeck]:
        """Commander decks led by `commander` (full name) and updated at or after `since`, newest first per
        bracket.

        One search per bracket (None searches every bracket), all sharing MAX_SEARCH_PAGES pages.
        """
        found: dict[int, PeerDeck] = {}
        pages_left = MAX_SEARCH_PAGES
        for bracket in brackets:
            url: str | None = f"{self._base_url}/decks/v3/"
            params: Mapping[str, str | int] | None = {
                "commanderName": commander,
                "formats": COMMANDER_FORMAT,
                "orderBy": "-updatedAt",
                "pageSize": PAGE_SIZE,
            } | ({"edhBracket": bracket} if bracket is not None else {})
            while url is not None and pages_left > 0:
                pages_left -= 1
                self._pacer.wait()
                raw = self._fetcher.get_json(url, params)
                try:
                    page, url = parse_search_page(raw)
                except (ShapeError, ValueError) as error:
                    raise ArchidektError(f"unexpected search answer from Archidekt: {error}") from error
                params = None
                found.update((deck.id, deck) for deck in page if deck.updated_at >= since)
                if any(deck.updated_at < since for deck in page):
                    break
        return list(found.values())

    def deck(self, deck_id: int) -> Deck:
        self._pacer.wait()
        return self._decks.get_deck(deck_id)


def brackets_to_read(bracket: int | None) -> tuple[int | None, ...]:
    """A deck's own bracket and the one above it, whose decks play what this one could grow into; None (any
    bracket) stays one search, and the top bracket has none above."""
    if bracket is None or bracket >= TOP_BRACKET:
        return (bracket,)
    return (bracket, bracket + 1)


def most_viewed(decks: Sequence[PeerDeck], *, limit: int = MAX_DECK_READS) -> list[PeerDeck]:
    """The decks worth reading, most viewed first: those with at least 1000 views, or 750, or 500 when fewer
    than MIN_DECKS reach the bar, or all of them; with any deck found there is always something to read."""
    ranked = sorted(decks, key=lambda deck: (-deck.views, deck.id))
    for floor in VIEW_FLOORS:
        chosen = [deck for deck in ranked if deck.views >= floor]
        if len(chosen) >= MIN_DECKS:
            return chosen[:limit]
    return ranked[:limit]


def parse_search_page(raw: object) -> tuple[list[PeerDeck], str | None]:
    page = as_object(raw, "page")
    decks: list[PeerDeck] = []
    for index, item in enumerate(req_list(page, "results", "page")):
        where = f"page.results[{index}]"
        deck = as_object(item, where)
        if req_int(deck, "deckFormat", where) != COMMANDER_FORMAT:
            continue
        updated_at = datetime.fromisoformat(req_str(deck, "updatedAt", where))
        if updated_at.tzinfo is None:
            raise ShapeError(f"{where}.updatedAt: no time zone")
        decks.append(
            PeerDeck(
                id=req_int(deck, "id", where),
                name=req_str(deck, "name", where),
                owner=req_str(req_object(deck, "owner", where), "username", f"{where}.owner"),
                views=req_int(deck, "viewCount", where),
                updated_at=updated_at,
                bracket=opt_int(deck, "edhBracket", where),
            )
        )
    return decks, _next_link(page)


def _next_link(page: Mapping[str, object]) -> str | None:
    next_url = opt_str(page, "next", "page")
    if next_url is None:
        return None
    # Archidekt writes next links with http://; the request still goes over https.
    if next_url.startswith(_PLAIN_API_BASE + "/"):
        next_url = API_BASE + next_url.removeprefix(_PLAIN_API_BASE)
    if not next_url.startswith(API_BASE + "/"):
        raise ShapeError(f"page.next points outside the Archidekt API: {next_url}")
    return next_url


def parse_deck_page(raw: object) -> tuple[list[DeckSummary], str | None]:
    page = as_object(raw, "page")
    next_url = _next_link(page)
    decks: list[DeckSummary] = []
    for index, item in enumerate(req_list(page, "results", "page")):
        where = f"page.results[{index}]"
        deck = as_object(item, where)
        if req_int(deck, "deckFormat", where) != COMMANDER_FORMAT:
            continue
        decks.append(
            DeckSummary(
                id=req_int(deck, "id", where),
                name=req_str(deck, "name", where),
                updated_at=req_str(deck, "updatedAt", where),
                tags=_tag_names(deck, "tags", where) if "tags" in deck else None,
            )
        )
    return decks, next_url


def parse_deck(raw: object) -> Deck:
    deck = as_object(raw, "deck")
    excluded = frozenset(name for name, counts in category_flags(raw).items() if not counts)
    cards: list[DeckCard] = []
    for index, item in enumerate(req_list(deck, "cards", "deck")):
        card = _parse_card(as_object(item, f"deck.cards[{index}]"), f"deck.cards[{index}]", excluded)
        if card is not None:
            cards.append(card)
    cards.sort(key=lambda c: (c.name.casefold(), c.set_code, c.collector_number, c.entry_id))
    return Deck(
        id=req_int(deck, "id", "deck"),
        name=req_str(deck, "name", "deck"),
        owner=req_str(req_object(deck, "owner", "deck"), "username", "deck.owner"),
        bracket=opt_int(deck, "edhBracket", "deck"),
        updated_at=req_str(deck, "updatedAt", "deck"),
        cards=tuple(cards),
        tags=_deck_tags(deck),
    )


def _deck_tags(deck: Mapping[str, object]) -> tuple[str, ...]:
    """The deck tag names; a deck with none may leave the field out."""
    return _tag_names(deck, "deckTags", "deck") if "deckTags" in deck else ()


def _tag_names(obj: Mapping[str, object], key: str, where: str) -> tuple[str, ...]:
    """Tag names in Archidekt's order, each once."""
    names = (
        req_str(as_object(item, f"{where}.{key}[{i}]"), "name", f"{where}.{key}[{i}]")
        for i, item in enumerate(req_list(obj, key, where))
    )
    return tuple(dict.fromkeys(names))


def category_flags(raw: object) -> dict[str, bool]:
    """Each of the deck's categories and whether its cards count in the deck; each deck sets its own."""
    flags: dict[str, bool] = {}
    for index, item in enumerate(req_list(as_object(raw, "deck"), "categories", "deck")):
        where = f"deck.categories[{index}]"
        category = as_object(item, where)
        flags[req_str(category, "name", where)] = req_bool(category, "includedInDeck", where)
    return flags


def _parse_card(entry: Mapping[str, object], where: str, excluded: frozenset[str]) -> DeckCard | None:
    if entry.get("deletedAt") is not None:
        return None
    card = req_object(entry, "card", where)
    oracle = req_object(card, "oracleCard", f"{where}.card")
    edition = req_object(card, "edition", f"{where}.card")
    categories = str_list(entry, "categories", where)
    raw_label = opt_str(entry, "label", where)
    return DeckCard(
        name=req_str(oracle, "name", f"{where}.card.oracleCard"),
        quantity=req_int(entry, "quantity", where),
        categories=categories,
        in_deck=not categories or categories[0] not in excluded,
        scryfall_id=req_str(card, "uid", f"{where}.card"),
        archidekt_card_id=req_int(card, "id", f"{where}.card"),
        entry_id=req_int(entry, "id", where),
        set_code=req_str(edition, "editioncode", f"{where}.card.edition"),
        collector_number=req_str(card, "collectorNumber", f"{where}.card"),
        finish=opt_str(entry, "modifier", where) or "Normal",
        label=label_name(raw_label),
        label_colour=label_colour(raw_label),
    )


def label_name(raw: str | None) -> str:
    """The name in Archidekt's "name,#colour" label; "" for a card without one (",#656565")."""
    if not raw:
        return ""
    return raw.rsplit(",", 1)[0].strip()


def label_colour(raw: str | None) -> str:
    """The colour in Archidekt's "name,#colour" label; "" for a card without a label."""
    if not raw or not label_name(raw) or "," not in raw:
        return ""
    return raw.rsplit(",", 1)[1].strip()
