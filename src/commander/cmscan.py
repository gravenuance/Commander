"""The Cardmarket scan: reads the offers of the wanted cards, two passes, one page at a time (ADR 0067).

Pass 1 opens one card page per wanted card, with the owner's language and condition in the address, and reads
its cheapest offers. Pass 2, given the wants list's id, opens the wants-filtered stock page of the base
sellers (`choose_base_sellers`) and merges what it shows into the offers of pass 1.

The scan only loads pages through a `Browser`, 4 to 6 seconds apart and never in parallel. It stops at the
first page that is not the expected one (a challenge that stays, a sign-in page, markup the readers do not
recognise) and reports why; it never retries a page or works around one. A card Cardmarket has no page for is
the exception: that card is reported as not found and the scan goes on.
"""

import logging
import random
import re
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from fractions import Fraction
from urllib.parse import quote, urlencode

from commander.cmpages import (
    CardOffers,
    Condition,
    Offer,
    PageError,
    PageNotFound,
    SellerOffers,
    read_card_offers,
    read_seller_offers,
)
from commander.cmsession import Browser, BrowserError
from commander.sellersolve import WantedCard, card_key, fold_marks

log = logging.getLogger(__name__)

CARDS_URL = "https://www.cardmarket.com/en/Magic/Cards"
USERS_URL = "https://www.cardmarket.com/en/Magic/Users"
ENGLISH = 1
DEFAULT_BASE_SELLERS = 15
# Seconds between one page load and the next, drawn evenly from this range.
MIN_GAP_S = 4.0
MAX_GAP_S = 6.0

_APOSTROPHES = "'\N{RIGHT SINGLE QUOTATION MARK}\N{LEFT SINGLE QUOTATION MARK}`"
_NOT_SLUG = re.compile(r"[^A-Za-z0-9]+")


def card_slug(name: str) -> str:
    """The card's address segment: "Lim-Dûl's Vault" is `Lim-Duls-Vault`, "Fire // Ice" `Fire-Ice`. Marks
    fold into their letters (as `card_key` folds them), apostrophes drop, and every other run of punctuation
    and spaces is a hyphen."""
    letters_only = "".join(c for c in fold_marks(name) if c not in _APOSTROPHES)
    return _NOT_SLUG.sub("-", letters_only).strip("-")


@dataclass(slots=True, frozen=True)
class ScanFilters:
    """What the offers must be, as the card page's own filters take it in the address.

    One language only: Cardmarket's form names a field per language (`language[N]`), and how several go in an
    address is not confirmed against the live site.
    """

    # Cardmarket's language id (1 is English); None for any.
    language: int | None = ENGLISH
    min_condition: Condition | None = None

    def query(self) -> str:
        params: dict[str, int] = {}
        if self.language is not None:
            params["language"] = self.language
        if self.min_condition is not None:
            # Cardmarket numbers the conditions from 1 (Mint) to 7 (Poor).
            params["minCondition"] = list(Condition).index(self.min_condition) + 1
        return urlencode(params)


def card_page_url(name: str, filters: ScanFilters) -> str:
    query = filters.query()
    return f"{CARDS_URL}/{card_slug(name)}" + (f"?{query}" if query else "")


def seller_page_url(seller: str, wants_list_id: int, page: int = 1) -> str:
    """The seller's stock of the wants list's cards; page 2 and on go in `site`, which is not confirmed
    against the live site, so the scan reads page 1 only."""
    url = f"{USERS_URL}/{quote(seller, safe='')}/Offers/Singles?idWantslist={wants_list_id}"
    return url if page == 1 else f"{url}&site={page}"


@dataclass(slots=True, frozen=True)
class ScanRequest:
    wanted: Sequence[WantedCard]
    filters: ScanFilters = field(default_factory=ScanFilters)
    # The wants list holding the wanted cards; without it there is no pass 2.
    wants_list_id: int | None = None
    base_sellers: int = DEFAULT_BASE_SELLERS

    def __post_init__(self) -> None:
        if self.base_sellers < 1:
            raise ValueError(f"the scan needs at least one base seller, got {self.base_sellers}")


class ScanStage(Enum):
    CARDS = "cards"
    SELLERS = "sellers"


@dataclass(slots=True, frozen=True)
class ScanProgress:
    """Pages read of pages planned (pass 2's pages are planned once pass 1 has chosen the sellers), and the
    card or seller being opened."""

    stage: ScanStage
    done: int
    planned: int
    current: str


class ScanStatus(Enum):
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    STOPPED = "stopped"


@dataclass(slots=True, frozen=True)
class ScanResult:
    # Every buyable offer read, named by the wanted card it is for.
    offers: tuple[Offer, ...]
    # The buyer's country, as Cardmarket's delivery estimates name it.
    destination: str | None
    status: ScanStatus
    # Why the scan stopped; None unless it did.
    stop_reason: str | None
    pages_read: int
    # Wanted cards Cardmarket has no page for.
    not_found: tuple[str, ...]
    # Wanted cards whose page held no buyable offer.
    no_buyable: tuple[str, ...]
    base_sellers: tuple[str, ...]
    # Wanted cards with more offers than the 50 read ("Show more results").
    cards_with_more: tuple[str, ...]
    # Base sellers whose wants-filtered stock has more pages than the one read.
    sellers_with_more_pages: tuple[str, ...]


def choose_base_sellers(offers: Iterable[Offer], count: int) -> tuple[str, ...]:
    """The sellers pass 2 opens, from the buyable offers pass 1 read. The rule:

    * A seller's coverage is the number of distinct wanted cards they offer; their scarcity is the sum, over
      those cards, of 1 divided by the number of sellers offering the card (a card only they carry adds 1, a
      card fifty sellers carry adds 1/50), so the sellers of the rarest cards score highest.
    * The first ceil(count / 2) places go to the highest scarcity, ties to the wider coverage; the remaining
      places go to the widest coverage among the others, ties to the higher scarcity.
    * Remaining ties go to the name in alphabetical order, so the choice is the same on every run.

    The result lists the rare-card sellers first, then the wide-coverage ones; fewer sellers than `count` give
    all of them. Offers that are not buyable do not count.
    """
    sellers_of: dict[str, set[str]] = defaultdict(set)
    for offer in offers:
        if offer.buyable:
            sellers_of[card_key(offer.card)].add(offer.seller)
    coverage: Counter[str] = Counter()
    scarcity: dict[str, Fraction] = defaultdict(Fraction)
    for sellers in sellers_of.values():
        for seller in sellers:
            coverage[seller] += 1
            scarcity[seller] += Fraction(1, len(sellers))
    rare_places = (count + 1) // 2
    by_scarcity = sorted(coverage, key=lambda s: (-scarcity[s], -coverage[s], s))
    rare = by_scarcity[:rare_places]
    wide = sorted((s for s in coverage if s not in rare), key=lambda s: (-coverage[s], -scarcity[s], s))
    return tuple(rare + wide[: count - len(rare)])


class _Stop(Exception):  # noqa: N818 - control flow inside one scan, never leaves it
    def __init__(self, status: ScanStatus, reason: str | None) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class _Scan:
    def __init__(
        self,
        browser: Browser,
        request: ScanRequest,
        *,
        cancel: threading.Event,
        on_progress: Callable[[ScanProgress], None],
        sleep: Callable[[float], object],
        uniform: Callable[[float, float], float],
    ) -> None:
        self._browser = browser
        self._request = request
        self._cancel = cancel
        self._on_progress = on_progress
        self._sleep = sleep
        self._uniform = uniform
        # One entry per wanted card, by card key: the name the want gave.
        self._names: dict[str, str] = {}
        for want in request.wanted:
            self._names.setdefault(card_key(want.name), want.name)
        self._offers: dict[int, Offer] = {}
        self._destination: str | None = None
        self._not_found: list[str] = []
        self._cards_with_more: list[str] = []
        self._sellers_with_more: list[str] = []
        self._base: tuple[str, ...] = ()
        # Pages read, and loads begun (a load that failed counts toward the gap, not the pages).
        self._pages = 0
        self._loads = 0
        self._planned = len(self._names)

    def run(self) -> ScanResult:
        status, reason = ScanStatus.COMPLETE, None
        try:
            self._pass_one()
            if self._request.wants_list_id is not None:
                self._pass_two(self._request.wants_list_id)
        except _Stop as stop:
            status, reason = stop.status, stop.reason
            log.log(
                logging.INFO if status is ScanStatus.CANCELLED else logging.WARNING,
                "scan %s after %d pages: %s",
                status.value,
                self._pages,
                reason or "cancelled",
            )
        return self._result(status, reason)

    def _result(self, status: ScanStatus, reason: str | None) -> ScanResult:
        offers = tuple(o for o in self._offers.values() if o.buyable)
        bought = {card_key(o.card) for o in offers}
        searched = status is ScanStatus.COMPLETE
        return ScanResult(
            offers=offers,
            destination=self._destination,
            status=status,
            stop_reason=reason,
            pages_read=self._pages,
            not_found=tuple(self._not_found),
            # A scan that stopped early cannot say which cards have no offer yet.
            no_buyable=tuple(
                name
                for key, name in self._names.items()
                if searched and key not in bought and name not in self._not_found
            ),
            base_sellers=self._base,
            cards_with_more=tuple(self._cards_with_more),
            sellers_with_more_pages=tuple(self._sellers_with_more),
        )

    def _load(self, url: str, stage: ScanStage, current: str) -> str:
        """The page's HTML: waits out the gap since the last page, and stops the scan on cancel or on a
        browser failure."""
        self._check_cancel()
        if self._loads > 0:
            self._sleep(self._uniform(MIN_GAP_S, MAX_GAP_S))
            self._check_cancel()
        self._loads += 1
        self._on_progress(ScanProgress(stage, self._pages, self._planned, current))
        log.debug("loading %s", url)
        try:
            return self._browser.load(url)
        except BrowserError as error:
            raise _Stop(ScanStatus.STOPPED, f"{current}: {error}") from error

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise _Stop(ScanStatus.CANCELLED, None)

    def _read[T](self, read: Callable[[str], T], html: str, current: str) -> T:
        try:
            return read(html)
        except PageNotFound:
            raise
        except PageError as error:
            raise _Stop(ScanStatus.STOPPED, f"{current}: {error}") from error

    def _pass_one(self) -> None:
        for key, name in self._names.items():
            url = card_page_url(name, self._request.filters)
            html = self._load(url, ScanStage.CARDS, name)
            try:
                page: CardOffers = self._read(read_card_offers, html, name)
            except PageNotFound:
                log.info("no Cardmarket page for %s (%s)", name, url)
                self._not_found.append(name)
                self._pages += 1
                continue
            self._pages += 1
            if page.destination is not None and self._destination is None:
                self._destination = page.destination
            if page.has_more:
                # The seam for "Show more results": 50 offers a click, 300 at most.
                self._cards_with_more.append(name)
            for offer in page.offers:
                self._offers[offer.article_id] = replace(offer, card=self._names[key])

    def _pass_two(self, wants_list_id: int) -> None:
        self._base = choose_base_sellers(self._offers.values(), self._request.base_sellers)
        self._planned = self._pages + len(self._base)
        for seller in self._base:
            html = self._load(seller_page_url(seller, wants_list_id), ScanStage.SELLERS, seller)
            stock: SellerOffers = self._read(read_seller_offers, html, seller)
            self._pages += 1
            if stock.pages > 1:
                log.info("%s has %d pages of wanted stock; the first was read", seller, stock.pages)
                self._sellers_with_more.append(seller)
            self._merge_stock(stock)

    def _merge_stock(self, stock: SellerOffers) -> None:
        """Add the seller's stock to the offers; an article pass 1 also read keeps what only a card page
        shows (the seller's sales and items)."""
        unknown = 0
        for offer in stock.offers:
            name = self._names.get(card_key(offer.card))
            if name is None:
                unknown += 1
                continue
            seen = self._offers.get(offer.article_id)
            merged = replace(offer, card=name)
            if seen is not None:
                days = merged.delivery_days if merged.delivery_days is not None else seen.delivery_days
                merged = replace(
                    merged, seller_sales=seen.seller_sales, seller_items=seen.seller_items, delivery_days=days
                )
            self._offers[offer.article_id] = merged
        if unknown:
            log.warning("%s: %d wants-list offers are for no wanted card by name", stock.seller, unknown)


def scan(
    browser: Browser,
    request: ScanRequest,
    *,
    cancel: threading.Event | None = None,
    on_progress: Callable[[ScanProgress], None] | None = None,
    sleep: Callable[[float], object] | None = None,
    uniform: Callable[[float, float], float] = random.uniform,
) -> ScanResult:
    """Run the scan and return what it read, whether it finished, was cancelled or stopped.

    `cancel` ends it before the next page. `sleep` waits out the gap between pages; the default waits on
    `cancel`, so a cancel ends the wait too.
    """
    stop = cancel if cancel is not None else threading.Event()
    return _Scan(
        browser,
        request,
        cancel=stop,
        on_progress=on_progress if on_progress is not None else _ignore,
        sleep=sleep if sleep is not None else stop.wait,
        uniform=uniform,
    ).run()


def _ignore(_: ScanProgress) -> None:
    return
