"""Read access to Archidekt's undocumented JSON API; every response is shape-checked before use."""

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from commander.jsonshape import (
    ShapeError,
    as_object,
    opt_bool,
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
from commander.net import HTTP_NOT_FOUND, Fetcher, HttpStatusError, Pacer
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

API_BASE = "https://archidekt.com/api"
_PLAIN_API_BASE = "http://archidekt.com/api"
COMMANDER_FORMAT = 3
# Archidekt's deck listings come 60 to a page whatever `pageSize` asks for (ADR 0048), so none is sent.
MAX_PAGES = 20
# A deck-tag search spans every commander, so it is smaller still: only the deck's MAX_SEARCH_TAGS most
# prevalent theme tags, each with this many pages per bracket and at most this many deck reads (and never
# more than an alike commander's). Fixed on purpose: no setting raises them.
MAX_SEARCH_TAGS = 3
TAG_SEARCH_PAGES_PER_BRACKET = 2
TAG_SEARCH_READS = 10
_UNAUTHORIZED = frozenset({401, 403})


class ArchidektError(Exception):
    """Archidekt answered with something this tool cannot use."""


class AuthError(Exception):
    """Signing in or using the login failed."""


class SignInRequiredError(AuthError):
    """No usable Archidekt login: none saved, or Archidekt refused it. The message states the problem and
    never holds the token; each front end adds how to sign in."""


def auth_headers(token: str | None) -> dict[str, str] | None:
    """The request headers that sign a call in as the token's owner; None for an anonymous call."""
    return {"Authorization": f"JWT {token}"} if token else None


def raise_if_unauthorized(error: HttpStatusError, message: str | None = None) -> None:
    """Turn a 401 or 403 into SignInRequiredError; any other status returns, so the caller can re-raise."""
    if error.status in _UNAUTHORIZED:
        raise SignInRequiredError(message or f"Archidekt refused the login (HTTP {error.status})") from None


class _ListingDrifted(Exception):  # noqa: N818 - control flow inside list_commander_decks, never escapes
    """A paged listing showed one deck on two pages."""


class ArchidektClient:
    """The owner's decks. With an access token the listing and the reads also cover the owner's private and
    unlisted decks (ADR 0040); without one they cover public decks only, as plain requests.

    Requests go out at once; only a 429 holds them, for every thread of the client (the `pacer`, which the
    client makes at no interval when none is given).
    """

    def __init__(
        self,
        fetcher: Fetcher,
        base_url: str = API_BASE,
        *,
        token: str | None = None,
        pacer: Pacer | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._base_url = base_url.rstrip("/")
        self._headers = auth_headers(token)
        self._guard = LimitGuard(pacer or Pacer(0.0))

    @property
    def signed_in(self) -> bool:
        return self._headers is not None

    def anonymous(self) -> "ArchidektClient":
        """The same client without the token."""
        return ArchidektClient(self._fetcher, self._base_url, pacer=self._guard.pacer)

    def list_commander_decks(self, user: str) -> list[DeckSummary]:
        """Every Commander deck the user owns that this client may see, following pagination.

        Raises SignInRequiredError when Archidekt answers a signed-in listing with 401 or 403.
        """
        if not user:
            raise ValueError("user is empty")
        try:
            return self._read_listing(user, restart_on_repeat=True)
        except _ListingDrifted:
            return self._read_listing(user, restart_on_repeat=False)

    def _read_listing(self, user: str, *, restart_on_repeat: bool) -> list[DeckSummary]:
        """One pass over the listing's pages. A deck edited between two page reads moves between pages, so it
        shows twice and another may be missing: the pass then raises _ListingDrifted when `restart_on_repeat`,
        and otherwise keeps the first row of each deck."""
        url: str | None = f"{self._base_url}/decks/v3/"
        params: Mapping[str, str | int] | None = {"ownerUsername": user}
        decks: list[DeckSummary] = []
        seen: set[int] = set()
        for _ in range(MAX_PAGES):
            if url is None:
                return decks
            raw = self._get(url, params)
            try:
                page, url = parse_deck_page(raw)
            except ShapeError as error:
                raise ArchidektError(f"unexpected deck list from Archidekt: {error}") from error
            # The "next" link already carries the query string.
            params = None
            for deck in page:
                if deck.id in seen:
                    if restart_on_repeat:
                        log.info(
                            "listing for %s drifted: deck %d is on two pages; reading it again from page 1",
                            user,
                            deck.id,
                        )
                        raise _ListingDrifted
                    log.info(
                        "listing for %s drifted again: deck %d is on two pages; keeping its first row",
                        user,
                        deck.id,
                    )
                    continue
                seen.add(deck.id)
                decks.append(deck)
        raise ArchidektError(f"deck list for {user} is longer than {MAX_PAGES} pages")

    def get_deck(self, deck_id: int) -> Deck:
        return _read_deck(self._get(f"{self._base_url}/decks/{deck_id}/", None), deck_id)

    def deck_is_gone(self, deck_id: int) -> bool:
        """Whether Archidekt answers 404 for the deck's page, the one answer that means it was deleted.

        Any other status (a refused login included, as SignInRequiredError) or network failure raises, and a
        page that reads, even an odd one, is False: only a 404 may count as gone.
        """
        try:
            self._get(f"{self._base_url}/decks/{deck_id}/", None)
        except HttpStatusError as error:
            if error.status == HTTP_NOT_FOUND:
                return True
            raise
        return False

    def _get(self, url: str, params: Mapping[str, str | int] | None) -> object:
        return self._guard.send(lambda: self._read(url, params), target(url, params))

    def _read(self, url: str, params: Mapping[str, str | int] | None) -> object:
        if self._headers is None:
            return self._fetcher.get_json(url, params, surface_rate_limit=True)
        try:
            return self._fetcher.get_json(url, params, headers=self._headers, surface_rate_limit=True)
        except HttpStatusError as error:
            raise_if_unauthorized(error)
            raise


def _read_deck(raw: object, deck_id: int) -> Deck:
    try:
        return parse_deck(raw)
    except (ShapeError, ValueError) as error:
        raise ArchidektError(f"unexpected data for deck {deck_id}: {error}") from error


class ArchidektSearch:
    """Other players' public decks.

    Archidekt's written terms forbid automated searches, but its co-founder has said the API is open to use
    (ADR 0004), so this stays at a few paced requests a run and callers cache what it finds.
    """

    def __init__(self, fetcher: Fetcher, pacer: Pacer, base_url: str = API_BASE) -> None:
        self._fetcher = fetcher
        self._base_url = base_url.rstrip("/")
        self._guard = LimitGuard(pacer)

    def popular_decks(
        self,
        commander: str | None,
        *,
        brackets: Sequence[int | None],
        since: datetime,
        label: str,
        tag: str | None = None,
        max_pages: int = MAX_SEARCH_PAGES,
        limit: int = MAX_DECK_READS,
        readable: Callable[[PeerDeck], bool] = lambda _: True,
    ) -> list[PeerDeck]:
        """Commander decks led by `commander` (full name; None for any commander) and updated at or after
        `since`, most viewed first per bracket, optionally only those with the deck tag; `label` names the
        search in the log.

        One search per bracket (None searches every bracket), each paging at most `max_pages` pages down its
        view-ordered listing, as `peersearch.descend` reads them.
        """
        # The first request carries the filters; the "next" links already hold them.
        cursors = [
            Cursor(
                (
                    f"{self._base_url}/decks/v3/",
                    {"formats": COMMANDER_FORMAT, "orderBy": "-viewCount"}
                    | ({"commanderName": commander} if commander is not None else {})
                    | ({"edhBracket": bracket} if bracket is not None else {})
                    | ({"deckTagName": tag} if tag is not None else {}),
                )
            )
            for bracket in brackets
        ]
        return descend(
            cursors,
            self._read_search_page,
            since=since,
            site=Site.ARCHIDEKT,
            label=label,
            max_pages=max_pages,
            limit=limit,
            readable=readable,
        )

    def _read_search_page(self, request: PageRequest) -> tuple[list[PeerDeck], PageRequest | None]:
        raw = self._get(*request)
        try:
            page, next_url = parse_search_page(raw)
        except (ShapeError, ValueError) as error:
            raise ArchidektError(f"unexpected search answer from Archidekt: {error}") from error
        return page, None if next_url is None else (next_url, None)

    def deck(self, deck_id: int) -> Deck:
        return _read_deck(self._get(f"{self._base_url}/decks/{deck_id}/", None), deck_id)

    def _get(self, url: str, params: Mapping[str, str | int] | None) -> object:
        """One paced request, held by a 429 as `LimitGuard` says."""
        return self._guard.send(
            lambda: self._fetcher.get_json(url, params, surface_rate_limit=True), target(url, params)
        )


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
                private=opt_bool(deck, "private", where) or False,
                unlisted=opt_bool(deck, "unlisted", where) or False,
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
        private=opt_bool(deck, "private", "deck") or False,
        unlisted=opt_bool(deck, "unlisted", "deck") or False,
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
