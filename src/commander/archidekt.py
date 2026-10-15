"""Read access to Archidekt's undocumented JSON API; every response is shape-checked before use."""

from collections.abc import Mapping

from commander.jsonshape import (
    ShapeError,
    as_list,
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
from commander.models import Deck, DeckCard, DeckSummary
from commander.net import Fetcher

API_BASE = "https://archidekt.com/api"
COMMANDER_FORMAT = 3
PAGE_SIZE = 50
MAX_PAGES = 20


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


def parse_deck_page(raw: object) -> tuple[list[DeckSummary], str | None]:
    page = as_object(raw, "page")
    next_url = opt_str(page, "next", "page")
    if next_url is not None and not next_url.startswith(API_BASE + "/"):
        raise ShapeError(f"page.next points outside the Archidekt API: {next_url}")
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
            )
        )
    return decks, next_url


def parse_deck(raw: object) -> Deck:
    deck = as_object(raw, "deck")
    excluded = _excluded_categories(req_list(deck, "categories", "deck"))
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
    )


def _excluded_categories(categories: object) -> frozenset[str]:
    excluded: set[str] = set()
    for index, item in enumerate(as_list(categories, "deck.categories")):
        where = f"deck.categories[{index}]"
        category = as_object(item, where)
        if not req_bool(category, "includedInDeck", where):
            excluded.add(req_str(category, "name", where))
    return frozenset(excluded)


def _parse_card(entry: Mapping[str, object], where: str, excluded: frozenset[str]) -> DeckCard | None:
    if entry.get("deletedAt") is not None:
        return None
    card = req_object(entry, "card", where)
    oracle = req_object(card, "oracleCard", f"{where}.card")
    edition = req_object(card, "edition", f"{where}.card")
    categories = str_list(entry, "categories", where)
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
    )
