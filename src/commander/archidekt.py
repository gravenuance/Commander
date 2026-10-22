"""Read access to Archidekt's undocumented JSON API; every response is shape-checked before use."""

import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
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
from commander.models import ARCHIDEKT_DECK_URL, Deck, DeckCard, DeckSummary
from commander.net import HTTP_NOT_FOUND, Fetcher, HttpStatusError, Pacer, RateLimitedError

log = logging.getLogger(__name__)

API_BASE = "https://archidekt.com/api"
_PLAIN_API_BASE = "http://archidekt.com/api"
COMMANDER_FORMAT = 3
# Archidekt's deck listings come 60 to a page whatever `pageSize` asks for (ADR 0048), so none is sent.
MAX_PAGES = 20
# Reading other players' decks: most viewed first, at most this many search pages per bracket searched (ADR
# 0050), and a request every 6.5 seconds, just under the ten a minute Archidekt answers before it starts
# sending 429s (ADR 0048).
MAX_SEARCH_PAGES = 5
SEARCH_INTERVAL_SECONDS = 6.5
# After a 429 Archidekt gets this long without a request, or the Retry-After if that is longer; its limit
# lifts within the minute.
RATE_LIMIT_PAUSE_SECONDS = 60.0
# A request that was rate limited is tried again this many times after the pause.
RATE_LIMIT_RETRIES = 1
# The top of Archidekt's bracket scale.
TOP_BRACKET = 5
# Which decks are read: every one with at least the first view count of VIEW_FLOORS that gives MIN_DECKS
# decks, else every one with at least the last; at most MAX_DECK_READS of them, most viewed first.
VIEW_FLOORS = (1000, 750, 500)
MIN_DECKS = 10
MAX_DECK_READS = 60
# A deck-tag search spans every commander, so it is kept small: this many pages per bracket, and a quarter
# of a commander's deck reads (rounded up).
TAG_SEARCH_PAGES_PER_BRACKET = 3
TAG_READ_DIVISOR = 4
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


class _LimitGuard:
    """Sends Archidekt reads through a pacer and keeps away after a 429.

    After a 429 nothing goes out for RATE_LIMIT_PAUSE_SECONDS or the Retry-After if longer, which holds the
    next request of any caller of the pacer; the limited one is tried again after it. A request limited again
    gives up: every later request of this guard fails at once, so callers fall back to their cache without
    more waiting.
    """

    def __init__(self, pacer: Pacer) -> None:
        self.pacer = pacer
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
                    log.warning("Archidekt is still limiting requests; no more are sent this run")
                    self._gave_up = error
                    raise
                pause = max(RATE_LIMIT_PAUSE_SECONDS, error.retry_after or 0.0)
                log.warning("Archidekt asked for quiet; waiting %.0fs before the next request", pause)
                self.pacer.cool_down(pause)
            finally:
                log.debug("Archidekt request %s took %.2fs", target, time.perf_counter() - started)
        raise AssertionError("unreachable: the last attempt raises")  # pragma: no cover


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
        self._guard = _LimitGuard(pacer or Pacer(0.0))

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
        return self._guard.send(lambda: self._read(url, params), _target(url, params))

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
        self._base_url = base_url.rstrip("/")
        self._guard = _LimitGuard(pacer)

    def popular_decks(
        self,
        commander: str | None,
        *,
        brackets: Sequence[int | None],
        since: datetime,
        label: str,
        tag: str | None = None,
        max_pages: int = MAX_SEARCH_PAGES,
    ) -> list[PeerDeck]:
        """Commander decks led by `commander` (full name; None for any commander) and updated at or after
        `since`, most viewed first per bracket, optionally only those with the deck tag; `label` names the
        search in the log.

        One search per bracket (None searches every bracket), each paging at most `max_pages` pages down its
        view-ordered listing. The searches go down the view floors together: to the first floor that
        MIN_DECKS of the decks found reach, and never below the last, so a deck under it is never listed.
        """
        found: dict[int, PeerDeck] = {}
        cursors = [_Cursor(self._base_url, commander, bracket, tag) for bracket in brackets]
        pages = 0
        cut_short = False
        for floor in VIEW_FLOORS:
            for cursor in cursors:
                while (url := cursor.pending(floor)) is not None:
                    if cursor.pages == max_pages:
                        cut_short = True
                        break
                    cursor.pages += 1
                    pages += 1
                    page, cursor.next_url = self._read_search_page(url, cursor.params)
                    cursor.params = None
                    cursor.lowest = min((deck.views for deck in page), default=0)
                    found.update((deck.id, deck) for deck in page if deck.updated_at >= since)
            if reaches_floor(found.values(), floor):
                break
        log.info(
            "Archidekt search for %s by views: %d pages, %d decks in the window", label, pages, len(found)
        )
        if cut_short:
            log.info("Archidekt search for %s by views was cut short at %d pages a bracket", label, max_pages)
        return list(found.values())

    def _read_search_page(
        self, url: str, params: Mapping[str, str | int] | None
    ) -> tuple[list[PeerDeck], str | None]:
        raw = self._get(url, params)
        try:
            return parse_search_page(raw)
        except (ShapeError, ValueError) as error:
            raise ArchidektError(f"unexpected search answer from Archidekt: {error}") from error

    def deck(self, deck_id: int) -> Deck:
        return _read_deck(self._get(f"{self._base_url}/decks/{deck_id}/", None), deck_id)

    def _get(self, url: str, params: Mapping[str, str | int] | None) -> object:
        """One paced request, held by a 429 as `_LimitGuard` says."""
        return self._guard.send(
            lambda: self._fetcher.get_json(url, params, surface_rate_limit=True), _target(url, params)
        )


class _Cursor:
    """Where one bracket's view-ordered listing has been read to."""

    def __init__(self, base_url: str, commander: str | None, bracket: int | None, tag: str | None) -> None:
        self.next_url: str | None = f"{base_url}/decks/v3/"
        # The first request carries the filters; the "next" links already hold them.
        self.params: Mapping[str, str | int] | None = (
            {"formats": COMMANDER_FORMAT, "orderBy": "-viewCount"}
            | ({"commanderName": commander} if commander is not None else {})
            | ({"edhBracket": bracket} if bracket is not None else {})
            | ({"deckTagName": tag} if tag is not None else {})
        )
        self.pages = 0
        # The fewest views on the last page read; None before the first.
        self.lowest: int | None = None

    def pending(self, floor: int) -> str | None:
        """The page to read next to find every deck with at least `floor` views, if there is one: the listing
        is most viewed first, so a page ending under the floor has every such deck above it."""
        if self.next_url is None or (self.lowest is not None and self.lowest < floor):
            return None
        return self.next_url


def _target(url: str, params: Mapping[str, str | int] | None) -> str:
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
    ranked = sorted(decks, key=lambda deck: (-deck.views, deck.id))
    floor = next((bar for bar in VIEW_FLOORS if reaches_floor(ranked, bar)), VIEW_FLOORS[-1])
    return [deck for deck in ranked if deck.views >= floor][:limit]


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
