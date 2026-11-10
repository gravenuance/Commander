"""The Buy screen's engine: from the decks' Don't Have cards to a package of Cardmarket sellers (ADR 0067).

`BuyService` lists the cards to buy by deck, keeps the filters, and runs one scan at a time on a background
thread: fill the app's wants list, read the offers, then work out the cheapest package for each number of
sellers. When Cardmarket needs the owner (a sign-in, Cloudflare's check) the scan window is shown over the
screen's pane and the phase says which. The page polls `status` while it runs and asks `package` for the
detail of a row. A changed price cap needs no new scan: the offers already read are judged again and solved
again. The owner puts a chosen package in their Cardmarket cart in the same kind of run, which keeps the
scan's results and adds a report of what the cart holds. The service remembers the wants list's id from the
last scan, per account and in memory only, so the next one skips the lists page. Everything that touches
Cardmarket or a window goes through `BuyEngine`, so tests run the whole flow without either.
"""

import logging
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, MutableMapping, MutableSet, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Protocol

from commander.buypane import PaneAreaSink, parse_pane_area
from commander.buysettings import BuyError, BuyFilters, BuyStore, check_base_sellers, parse_filters
from commander.carddb import CardDataError, CardDb
from commander.cmcart import CartOutcome, CartProgress, CartRun, CartStatus, add_to_cart, not_sent
from commander.cmpages import PageError
from commander.cmscan import ScanProgress, ScanRequest, ScanResult, ScanStage, ScanStatus, scan
from commander.cmsession import (
    BrowserError,
    Cancelled,
    FormBrowser,
    Handover,
    Interrupted,
    LoadTimeout,
    Need,
)
from commander.cmshipping import CmShippingError, Country, ShippingMethod
from commander.cmwants import WantsCancelled, WantsProgress, WantsSync, sync_wants
from commander.credentials import CredentialError
from commander.deckexport import dont_have_counts
from commander.jsonshape import ShapeError
from commander.models import Deck
from commander.net import NetworkError
from commander.offerlimits import Eligible, OfferLimits, apply_limits
from commander.sellersolve import (
    CurveResult,
    Package,
    SolveCancelled,
    SolveError,
    WantedCard,
    card_key,
    solve_curve,
)

log = logging.getLogger(__name__)

# The most sellers the curve shows: each count is one exact solve, and a package of more is not one to post.
MAX_CURVE_SELLERS = 12

type Json = dict[str, object]
type Key = tuple[object, ...]
type SyncWants = Callable[..., WantsSync]
type Scan = Callable[..., ScanResult]
type Curve = Callable[..., CurveResult]
type AddToCart = Callable[..., CartRun]


class Phase(Enum):
    IDLE = "idle"
    # The owner is signing in to, or passing Cloudflare's check in, the scan window shown over the screen.
    SIGN_IN = "signin"
    CHECK = "check"
    WANTS = "wants"
    SCANNING = "scanning"
    SOLVING = "solving"
    RESULTS = "results"
    CANCELLED = "cancelled"
    STOPPED = "stopped"
    # A package is being put in the cart, and the run that did it has ended; the scan's results stay.
    CART = "cart"
    CARTED = "carted"


RUNNING = frozenset({Phase.SIGN_IN, Phase.CHECK, Phase.WANTS, Phase.SCANNING, Phase.SOLVING, Phase.CART})
# How a finished scan ends the run; a scan that did not complete still shows what it read.
_PHASE_OF = {
    ScanStatus.COMPLETE: Phase.RESULTS,
    ScanStatus.CANCELLED: Phase.CANCELLED,
    ScanStatus.STOPPED: Phase.STOPPED,
}


class RouteSource(Protocol):
    """Cardmarket's shipping tables, as far as the solver's routes need them."""

    def country_id(self, name: str) -> int | None: ...

    def countries(self) -> tuple[Country, ...]: ...

    def methods(self, origin_id: int, destination_id: int) -> tuple[ShippingMethod, ...]: ...


@dataclass(slots=True, frozen=True)
class BuyEngine:
    """Everything the flow reaches outside the process for. The defaults are the real readers; the first three
    have none because they need the app's windows."""

    # Opens the hidden scan window; it is shown through the handover when a page needs the owner.
    open_browser: Callable[[Handover], FormBrowser]
    # Where the page's report of the area for that window goes.
    pane: PaneAreaSink
    shipping: Callable[[], AbstractContextManager[RouteSource]]
    sync_wants: SyncWants = sync_wants
    scan: Scan = scan
    solve_curve: Curve = solve_curve
    add_to_cart: AddToCart = add_to_cart


class Tick(Enum):
    """How much of a deck is ticked for the purchase."""

    ALL = "all"
    SOME = "some"
    NONE = "none"


@dataclass(slots=True, frozen=True)
class _DeckRow:
    deck: Deck
    # Every Don't Have card of the deck, and the part of it that is ticked.
    counts: Counter[str]
    known: bool
    ticked: Counter[str]

    @property
    def copies(self) -> int:
        return self.counts.total()

    @property
    def included(self) -> bool:
        return bool(self.ticked)

    @property
    def tick(self) -> Tick:
        if not self.ticked:
            return Tick.NONE
        return Tick.ALL if len(self.ticked) == len(self.counts) else Tick.SOME


@dataclass(slots=True, frozen=True)
class _Run:
    wanted: tuple[WantedCard, ...]
    filters: BuyFilters
    base_sellers: int
    key: Key
    # The Archidekt account that started the run.
    user: str
    cancel: threading.Event = field(default_factory=threading.Event)
    # The filters' seller countries by name, known once the run has fetched Cardmarket's country list.
    limits: OfferLimits = field(default_factory=OfferLimits)


@dataclass(slots=True, frozen=True)
class _Outcome:
    packages: tuple[Package, ...]
    scan: ScanResult
    wants: WantsSync | None
    # The scanned offers the packages were chosen from.
    eligible: Eligible
    # What the scan was for, with its limits resolved; a new price cap is judged against this scan.
    run: _Run
    # The seller counts the solver found no package for in its time.
    timed_out: tuple[int, ...] = ()


@dataclass(slots=True)
class _State:
    phase: Phase = Phase.IDLE
    progress: str = ""
    # Why a scan was cancelled or stopped, in words for the user; `detail` is the longer reason (a tooltip).
    message: str = ""
    detail: str = ""
    outcome: _Outcome | None = None
    run: _Run | None = None
    # The cards and choices a finished run was for; what it shows is dropped once they change.
    key: Key | None = None
    # What the last package put in the cart came to; it goes with the results it was chosen from.
    cart: CartRun | None = None


class _Ended(Exception):  # noqa: N818 - control flow inside one run, never leaves it
    """The run ends here, as `phase` with `message` for the user and `detail` for the log."""

    def __init__(self, phase: Phase, message: str, detail: str = "") -> None:
        super().__init__(detail or message)
        self.phase = phase
        self.message = message
        self.detail = detail or message


# What the screen says, and the phase it is in, while the scan window is the owner's.
_HANDOVER_SCREEN = {
    Need.SIGN_IN: (Phase.SIGN_IN, "Sign in to Cardmarket below"),
    Need.CHECK: (Phase.CHECK, "Finish Cardmarket's check below"),
}


class _ScreenHandover:
    """The `Handover` of one run: the screen's phase and line say what the owner must do, and return to
    what they were when the window goes back."""

    def __init__(self, swap: Callable[[Phase, str], tuple[Phase, str]], cancel: threading.Event) -> None:
        self._swap = swap
        self._cancel = cancel
        self._resume: tuple[Phase, str] | None = None

    def show(self, need: Need) -> None:
        before = self._swap(*_HANDOVER_SCREEN[need])
        if self._resume is None:
            self._resume = before

    def hide(self) -> None:
        if self._resume is not None:
            self._swap(*self._resume)
            self._resume = None

    def cancelled(self) -> bool:
        return self._cancel.is_set()


def wants_line(progress: WantsProgress) -> str:
    return f"Preparing wants list · {progress.detail}"


def scan_line(progress: ScanProgress) -> str:
    if progress.stage is ScanStage.CARDS:
        return f"Reading {progress.current} · {progress.done + 1} of {progress.planned}"
    return f"Checking {progress.current}'s stock · {progress.done + 1} of {progress.planned}"


def cart_line(progress: CartProgress) -> str:
    return f"Adding {progress.card} · {progress.done + 1} of {progress.planned}"


class BuyService:
    def __init__(
        self,
        *,
        store: BuyStore,
        decks: Callable[[], Sequence[Deck]],
        user: Callable[[], str | None],
        engine: BuyEngine,
        card_db: Path,
        background: Callable[[Callable[[], None]], object],
    ) -> None:
        self._store = store
        self._decks = decks
        self._user = user
        self._engine = engine
        self._card_db = card_db
        self._background = background
        self._lock = threading.Lock()
        self._state = _State()
        # The wants list's id from the last sync, with the account it was for; kept in memory only.
        self._wants_list: tuple[str, int] | None = None

    # --- what the page asks ---------------------------------------------------------------------------

    def view(self) -> Json:
        """The cards to buy by deck, the filters and the state of the scan. Results that were worked out for
        other cards or filters are dropped here, so the page never shows them."""
        user = self._signed_in_user()
        rows = self._rows(user)
        key = self._key(user, self._wanted(rows))
        with self._lock:
            state = self._state
            if state.phase not in RUNNING and state.key is not None and state.key != key:
                log.info("buy: the cards or filters changed since the scan; what it showed is dropped")
                self._state = state = _State()
        return {
            "decks": [_deck_json(row) for row in rows],
            "copies": sum(row.ticked.total() for row in rows),
            "filters": self._store.filters().to_json(),
            "baseSellers": self._store.base_sellers(),
            "status": self.status(),
        }

    # The page's bridge passes the flag positionally, so it is not keyword-only.
    def include(self, deck_id: int, included: bool) -> Json:  # noqa: FBT001
        """Tick all of a deck's cards, or untick all of them."""
        user = self._signed_in_user()
        self._refuse_while_running()
        if not any(row.deck.id == deck_id for row in self._rows(user)):
            raise BuyError("That deck has nothing to buy.")
        left, skipped = self._ticks(user)
        if included:
            left.discard(deck_id)
        else:
            left.add(deck_id)
        skipped.pop(deck_id, None)
        self._store.set_left_out(user, frozenset(left), skipped)
        log.info("buy: deck %d %s the purchase", deck_id, "joins" if included else "leaves")
        return self.view()

    # Positional for the same reason as in `include`.
    def include_card(self, deck_id: int, card: object, included: bool) -> Json:  # noqa: FBT001
        """Tick one of a deck's cards, or untick it; a card in two decks is ticked in each on its own."""
        user = self._signed_in_user()
        self._refuse_while_running()
        row = next((row for row in self._rows(user) if row.deck.id == deck_id), None)
        if row is None or not isinstance(card, str) or card not in row.counts:
            raise BuyError("That card has nothing to buy.")
        left, skipped = self._ticks(user)
        if deck_id in left:
            # Ticking one card of an unticked deck ticks that card alone.
            if included:
                left.discard(deck_id)
                skipped[deck_id] = frozenset(name for name in row.counts if name != card)
        else:
            names = set(skipped.get(deck_id, frozenset()))
            if included:
                names.discard(card)
            else:
                names.add(card)
            skipped[deck_id] = frozenset(names)
        _tidy(deck_id, row.counts, left, skipped)
        self._store.set_left_out(user, frozenset(left), skipped)
        log.info("buy: card %r of deck %d %s the purchase", card, deck_id, "joins" if included else "leaves")
        return self.view()

    def set_filters(
        self,
        language: object,
        condition: object,
        foil: object,
        countries: object,
        delivery: object,
        *,
        max_price: object = None,
    ) -> Json:
        user = self._signed_in_user()
        self._refuse_while_running()
        filters = parse_filters(language, condition, foil, countries, delivery, max_price=max_price)
        old = self._store.filters()
        wanted = self._wanted(self._rows(user))
        before = self._key(user, wanted)
        self._store.set_filters(filters)
        log.info("buy: filters now %s", filters.to_json())
        if filters != old:
            self._rejudge_kept_scan(user, wanted, before, old, filters)
        return self.view()

    def countries(self) -> Json:
        """Cardmarket's countries by name, for the seller-location filter."""
        self._signed_in_user()
        try:
            listed = self._listed_countries()
        except (NetworkError, ShapeError, CmShippingError) as error:
            log.warning("buy: no country list: %s", error)
            raise BuyError("Cardmarket's countries could not be loaded; try again.") from error
        ordered = sorted(listed, key=lambda country: country.name.casefold())
        return {"countries": [{"id": country.id, "name": country.name} for country in ordered]}

    def set_base_sellers(self, count: object) -> Json:
        self._signed_in_user()
        self._refuse_while_running()
        self._store.set_base_sellers(check_base_sellers(count))
        log.info("buy: base sellers now %d", self._store.base_sellers())
        return self.view()

    def start(self) -> Json:
        """Begin a scan of the ticked decks' cards; it runs in the background and `status` follows it."""
        user = self._signed_in_user()
        wanted = self._wanted(self._rows(user))
        if not wanted:
            raise BuyError("No cards to buy; tick a deck first.")
        filters, base = self._store.filters(), self._store.base_sellers()
        run = _Run(tuple(wanted), filters, base, self._key(user, wanted), user)
        with self._lock:
            if self._state.phase in RUNNING:
                raise BuyError("A scan is running already.")
            self._state = _State(Phase.WANTS, "Starting the scan", run=run)
        log.info(
            "buy: scan started for %d cards (%d copies); filters %s; %d base sellers",
            len(wanted),
            sum(want.quantity for want in wanted),
            filters.to_json(),
            base,
        )
        self._background(lambda: self._run(run, lambda: self._scan_and_solve(run)))
        return self.status()

    def add_to_cart(self, sellers: int, confirmed: object) -> Json:
        """Put the package of this many sellers in the owner's Cardmarket cart; it runs in the background and
        `status` follows it. The scan's results stay, and the cart's report joins them."""
        self._signed_in_user()
        with self._lock:
            state = self._state
            if state.phase in RUNNING:
                raise BuyError("Wait for the scan, or cancel it.")
            kept = state.outcome
            chosen = next((p for p in kept.packages if p.seller_count == sellers), None) if kept else None
            if kept is None or chosen is None:
                raise BuyError("That package is gone; scan again.")
            if confirmed != package_check(chosen):
                raise BuyError("That package changed; look at it again before adding.")
            run = replace(kept.run, cancel=threading.Event())
            self._state = _State(
                Phase.CART, "Opening the Cardmarket window", state.message, state.detail, kept, run, state.key
            )
        log.info(
            "buy: putting the package of %d sellers (%d cards) in the cart",
            sellers,
            sum(pick.copies for pick in chosen.picks),
        )
        self._background(lambda: self._run_cart(run, chosen))
        return self.status()

    def cancel(self) -> Json:
        """Stop after the current page or card; what was read or added so far is kept."""
        with self._lock:
            run = self._state.run if self._state.phase in RUNNING else None
        if run is not None and not run.cancel.is_set():
            log.info("buy: cancel asked")
            run.cancel.set()
        return self.status()

    def status(self) -> Json:
        with self._lock:
            state = self._state
            return {
                "phase": state.phase.value,
                "running": state.phase in RUNNING,
                "cancelling": state.phase in RUNNING and state.run is not None and state.run.cancel.is_set(),
                "progress": state.progress,
                "message": state.message,
                "detail": state.detail,
                "results": self._results_json(state.outcome),
                "cart": None if state.cart is None else _cart_json(state.cart),
            }

    def set_pane(self, left: object, top: object, width: object, height: object, scale: object) -> Json:
        """The page's report of the area its pane covers, in CSS pixels; an empty one if it shows none."""
        self._engine.pane.set_area(parse_pane_area(left, top, width, height, scale))
        return {}

    def package(self, sellers: int) -> Json:
        """The detail of the package of this many sellers, with each card's trend price."""
        with self._lock:
            outcome = self._state.outcome
        chosen = next((p for p in outcome.packages if p.seller_count == sellers), None) if outcome else None
        if outcome is None or chosen is None:
            raise BuyError("That package is gone; scan again.")
        trends = self._trends({pick.offer.card for pick in chosen.picks})
        return _package_json(chosen, trends, outcome.eligible)

    def forget_wants_list(self) -> None:
        """The account signed out or changed: the next sync looks the wants list up on the lists page."""
        with self._lock:
            self._wants_list = None

    def close(self) -> None:
        """The app is closing: end a running scan or cart run at its next step. The app destroys the windows
        right after, so a step that fails on a destroyed window ends the run as cancelled."""
        self.cancel()

    # --- the cards ------------------------------------------------------------------------------------

    def _signed_in_user(self) -> str:
        user = self._user()
        if not user:
            raise BuyError("Sign in to Archidekt first.")
        return user

    def _ticks(self, user: str) -> tuple[set[int], dict[int, frozenset[str]]]:
        """The account's unticked decks and, by deck, unticked cards, as copies to change."""
        return set(self._store.left_out(user)), dict(self._store.left_out_cards(user))

    def _rows(self, user: str) -> list[_DeckRow]:
        """The decks with something to buy. A card that is no longer Don't Have is forgotten from the
        unticked cards here, so the file never keeps what the decks no longer show."""
        stored_left, stored_skipped = self._store.left_out(user), self._store.left_out_cards(user)
        left, skipped = self._ticks(user)
        rows: list[_DeckRow] = []
        for deck in self._decks():
            known = deck.labels_known
            counts = dont_have_counts(deck) if known else Counter[str]()
            if known:
                _tidy(deck.id, counts, left, skipped)
            if known and not counts:
                continue
            ticked = Counter[str]() if deck.id in left or not known else _ticked(counts, skipped.get(deck.id))
            rows.append(_DeckRow(deck, counts, known, ticked))
        if frozenset(left) != stored_left or skipped != dict(stored_skipped):
            self._remember(user, left, skipped)
        return sorted(rows, key=lambda row: row.deck.short_name.casefold())

    def _remember(self, user: str, left: set[int], skipped: dict[int, frozenset[str]]) -> None:
        try:
            self._store.set_left_out(user, frozenset(left), skipped)
        except BuyError as error:
            log.warning("buy: the ticks could not be tidied: %s", error)

    @staticmethod
    def _wanted(rows: Sequence[_DeckRow]) -> list[WantedCard]:
        total: Counter[str] = Counter()
        for row in rows:
            total.update(row.ticked)
        return [WantedCard(name, total[name]) for name in sorted(total, key=lambda n: (n.casefold(), n))]

    def _key(self, user: str, wanted: Sequence[WantedCard]) -> Key:
        filters = self._store.filters()
        return (
            user.casefold(),
            tuple((want.name, want.quantity) for want in wanted),
            tuple(filters.to_json().items()),
            self._store.base_sellers(),
        )

    def _refuse_while_running(self) -> None:
        with self._lock:
            if self._state.phase in RUNNING:
                raise BuyError("Wait for the scan, or cancel it.")

    # --- the run --------------------------------------------------------------------------------------

    def _rejudge_kept_scan(
        self, user: str, wanted: Sequence[WantedCard], before: Key, old: BuyFilters, new: BuyFilters
    ) -> None:
        """A new price cap is the one filter Cardmarket is not asked about, so the offers already read are
        judged and solved again instead of scanned again. The base sellers stay those chosen under the old
        cap. A change to any other filter leaves the results to be dropped."""
        if replace(old, max_price_cents=new.max_price_cents) != new:
            return
        with self._lock:
            kept = self._state.outcome
            if self._state.phase in RUNNING or kept is None or self._state.key != before:
                return
            limits = replace(kept.run.limits, max_price_cents=new.max_price_cents)
            key = self._key(user, wanted)
            run = replace(kept.run, filters=new, limits=limits, key=key, cancel=threading.Event())
            self._state = _State(Phase.SOLVING, "Finding the cheapest packages", run=run)
        log.info("buy: the price cap changed; the offers already read are solved again")
        self._background(lambda: self._run(run, lambda: self._judge_and_solve(run, kept.scan, kept.wants)))

    def _run(self, run: _Run, work: Callable[[], _Outcome]) -> None:
        try:
            outcome = work()
        except _Ended as ended:
            self._finish(run, ended.phase, ended.message, ended.detail, None)
            return
        except Exception as error:  # noqa: BLE001 - nothing may escape a background thread unlogged
            if run.cancel.is_set():
                log.info("buy: the run failed after a cancel, which ends it: %r", error)
                self._finish(run, Phase.CANCELLED, "Scan cancelled.", "", None)
            else:
                self._finish_with_error(run, error)
            return
        phase = _PHASE_OF[outcome.scan.status]
        message, detail = _stopped_text(outcome.scan)
        self._finish(run, phase, message, detail, outcome)

    def _scan_and_solve(self, run: _Run) -> _Outcome:
        run = replace(run, limits=self._limits(run.filters))
        wants, found = self._read_cardmarket(run)
        if found.status is not ScanStatus.COMPLETE and not found.offers:
            message, detail = _stopped_text(found)
            raise _Ended(_PHASE_OF[found.status], message, detail)
        return self._judge_and_solve(run, found, wants)

    def _judge_and_solve(self, run: _Run, found: ScanResult, wants: WantsSync | None) -> _Outcome:
        eligible = apply_limits(found.offers, run.limits)
        log.info(
            "buy: limits left out %d offers from other countries, %d that arrive too late, %d with no "
            "delivery estimate and %d above the max price",
            eligible.other_countries,
            eligible.too_slow,
            eligible.no_estimate,
            len(eligible.over_price),
        )
        curve = self._solve(run, found, eligible)
        return _Outcome(curve.packages, found, wants, eligible, run, curve.timed_out)

    def _listed_countries(self) -> tuple[Country, ...]:
        with self._engine.shipping() as shipping:
            return shipping.countries()

    def _limits(self, filters: BuyFilters) -> OfferLimits:
        """What the scanned offers must satisfy: the filters' countries by the names offers spell them."""
        if not filters.countries:
            return OfferLimits(max_days=filters.delivery.max_days, max_price_cents=filters.max_price_cents)
        try:
            listed = self._listed_countries()
        except (NetworkError, ShapeError, CmShippingError) as error:
            raise _Ended(
                Phase.STOPPED, "Cardmarket's countries could not be loaded; scan again.", str(error)
            ) from error
        names = frozenset(country.name.casefold() for country in listed if country.id in filters.countries)
        if not names:
            raise _Ended(
                Phase.STOPPED,
                "Cardmarket does not list the seller countries chosen; choose again.",
                f"no listed country has an id in {sorted(filters.countries)}",
            )
        return OfferLimits(names, filters.delivery.max_days, filters.max_price_cents)

    def _read_cardmarket(self, run: _Run) -> tuple[WantsSync, ScanResult]:
        """Fill the wants list and scan in one window. A sign-in or check that comes in the middle of a wants
        step is done by the owner in that window; the step is then done again, once."""
        self._progress(Phase.WANTS, "Opening the Cardmarket window")
        browser = self._engine.open_browser(_ScreenHandover(self._swap_progress, run.cancel))
        try:
            try:
                return self._sync_and_scan(browser, run)
            except Interrupted as error:
                log.info("buy: a sign-in or check interrupted the wants list; filling it again: %s", error)
            try:
                return self._sync_and_scan(browser, run)
            except Interrupted as error:
                raise _Ended(
                    Phase.STOPPED, "Cardmarket interrupted the wants list again; scan again.", str(error)
                ) from error
        finally:
            _close(browser, "scan")

    def _sync_and_scan(self, browser: FormBrowser, run: _Run) -> tuple[WantsSync, ScanResult]:
        filters = run.filters
        log.info("buy: filling the wants list with %d cards", len(run.wanted))

        def wants_progress(progress: WantsProgress) -> None:
            self._progress(Phase.WANTS, wants_line(progress))

        def scan_progress(progress: ScanProgress) -> None:
            self._progress(Phase.SCANNING, scan_line(progress))

        try:
            wants = self._engine.sync_wants(
                browser,
                list(run.wanted),
                languages=filters.language.cardmarket_ids,
                min_condition=filters.min_condition,
                foil=filters.foil,
                list_id=self._remembered_wants_list(run.user),
                cancel=run.cancel,
                on_progress=wants_progress,
            )
        except (WantsCancelled, Cancelled) as error:
            raise _Ended(Phase.CANCELLED, "Scan cancelled.") from error
        with self._lock:
            self._wants_list = (run.user.casefold(), wants.list_id)
        log.info(
            "buy: wants list %d holds %d wants; %d unmatched, %d quantity mismatches",
            wants.list_id,
            len(wants.imported),
            len(wants.unmatched),
            len(wants.quantity_mismatch),
        )
        self._progress(Phase.SCANNING, "Starting the scan")
        request = ScanRequest(
            list(run.wanted),
            filters.scan_filters(),
            wants_list_id=wants.list_id if wants.imported else None,
            base_sellers=run.base_sellers,
            limits=run.limits,
        )
        found = self._engine.scan(
            browser,
            request,
            cancel=run.cancel,
            on_progress=scan_progress,
        )
        log.info(
            "buy: scan %s after %d pages: %d offers, %d cards not found, %d without offers",
            found.status.value,
            found.pages_read,
            len(found.offers),
            len(found.not_found),
            len(found.no_buyable),
        )
        return wants, found

    def _remembered_wants_list(self, user: str) -> int | None:
        with self._lock:
            remembered = self._wants_list
        return remembered[1] if remembered is not None and remembered[0] == user.casefold() else None

    def _solve(self, run: _Run, found: ScanResult, eligible: Eligible) -> CurveResult:
        self._progress(Phase.SOLVING, "Finding the cheapest packages")
        if not eligible.offers:
            return CurveResult((), ())
        # A cancel pressed during the scan has already ended it with what it read, which is solved; only one
        # pressed from here on ends the solving.
        before = run.cancel.is_set()
        try:
            with self._engine.shipping() as shipping:
                destination = shipping.country_id(found.destination) if found.destination else None
                if destination is None:
                    raise _Ended(
                        Phase.STOPPED,
                        "Cardmarket's shipping table lacks your country; see the log.",
                        f"no shipping table for the destination {found.destination!r}",
                    )

                def routes(country: str) -> tuple[ShippingMethod, ...]:
                    origin = shipping.country_id(country)
                    if origin is None:
                        log.warning("buy: no shipping table for sellers in %s", country)
                        return ()
                    return shipping.methods(origin, destination)

                curve = self._engine.solve_curve(
                    list(run.wanted),
                    eligible.offers,
                    routes,
                    max_sellers=MAX_CURVE_SELLERS,
                    cancelled=lambda: not before and run.cancel.is_set(),
                )
        except SolveCancelled as error:
            raise _Ended(Phase.CANCELLED, "Cancelled while finding packages; scan again.") from error
        except (NetworkError, ShapeError, CmShippingError) as error:
            raise _Ended(
                Phase.STOPPED, "Shipping prices could not be fetched; scan again.", str(error)
            ) from error
        except SolveError as error:
            raise _Ended(
                Phase.STOPPED, "The solver failed; the details are in the log.", str(error)
            ) from error
        log.info(
            "buy: %d packages, from %d to %d sellers; no package found in time for %d counts",
            len(curve.packages),
            *_seller_range(curve.packages),
            len(curve.timed_out),
        )
        return curve

    # --- state ----------------------------------------------------------------------------------------

    def _progress(self, phase: Phase, line: str) -> None:
        with self._lock:
            self._state.phase = phase
            self._state.progress = line
        log.debug("buy: %s", line)

    def _swap_progress(self, phase: Phase, line: str) -> tuple[Phase, str]:
        """Set the phase and line, and return the ones they replace."""
        with self._lock:
            before = (self._state.phase, self._state.progress)
            self._state.phase = phase
            self._state.progress = line
        log.debug("buy: %s", line)
        return before

    def _finish(self, run: _Run, phase: Phase, message: str, detail: str, outcome: _Outcome | None) -> None:
        log.log(
            logging.INFO if phase in (Phase.RESULTS, Phase.CANCELLED) else logging.WARNING,
            "buy: %s%s",
            phase.value,
            f": {detail}" if detail else "",
        )
        with self._lock:
            self._state = _State(phase, "", message, detail, outcome, key=run.key)

    def _run_cart(self, run: _Run, package: Package) -> None:
        """Add the package in the scan window and end the run with a report, whatever happened: a failure
        before anything was sent is a report of nothing sent."""
        self._progress(Phase.CART, "Opening the Cardmarket window")
        try:
            report = self._add_in_window(run, package)
        except Cancelled:
            log.info("buy: cancelled before anything was added")
            report = not_sent(package, CartStatus.CANCELLED, "")
        except Exception as error:  # noqa: BLE001 - nothing may escape a background thread unlogged
            if run.cancel.is_set():
                log.info("buy: the cart run failed after a cancel, which ends it: %r", error)
                report = not_sent(package, CartStatus.CANCELLED, _CART_CANCELLED_MIDWAY)
            else:
                report = not_sent(package, CartStatus.STOPPED, _cart_failure(error))
        log.log(
            logging.INFO if report.status is CartStatus.COMPLETE else logging.WARNING,
            "buy: the cart run %s: %d cards added, %d already there, %d gone, %d refused, %d short, "
            "%d missing",
            report.status.value,
            report.copies(CartOutcome.ADDED),
            report.copies(CartOutcome.ALREADY),
            report.copies(CartOutcome.GONE),
            report.copies(CartOutcome.REFUSED),
            report.copies(CartOutcome.SHORT),
            report.copies(CartOutcome.MISSING),
        )
        with self._lock:
            state = self._state
            self._state = _State(
                Phase.CARTED, "", state.message, state.detail, state.outcome, key=state.key, cart=report
            )

    def _add_in_window(self, run: _Run, package: Package) -> CartRun:
        def cart_progress(progress: CartProgress) -> None:
            self._progress(Phase.CART, cart_line(progress))

        browser = self._engine.open_browser(_ScreenHandover(self._swap_progress, run.cancel))
        try:
            return self._engine.add_to_cart(browser, package, cancel=run.cancel, on_progress=cart_progress)
        finally:
            _close(browser, "cart")

    def _finish_with_error(self, run: _Run, error: Exception) -> None:
        if isinstance(error, LoadTimeout):
            text = "Cardmarket did not answer; check your connection, then scan again."
        elif isinstance(error, BrowserError):
            text = "The Cardmarket window failed; the details are in the log."
        elif isinstance(error, PageError):
            text = "Cardmarket's page was not as expected; the details are in the log."
        elif isinstance(error, CredentialError):
            text = "The saved Cardmarket sign-in could not be read; see the log."
        else:
            log.error("buy: the scan failed unexpectedly", exc_info=error)
            self._finish(
                run, Phase.STOPPED, "Something went wrong; the details are in the log.", repr(error), None
            )
            return
        self._finish(run, Phase.STOPPED, text, str(error), None)

    # --- the numbers ----------------------------------------------------------------------------------

    def _results_json(self, outcome: _Outcome | None) -> Json | None:
        if outcome is None:
            return None
        found, wants = outcome.scan, outcome.wants
        rows: list[Json] = []
        previous: int | None = None
        cheapest = min((p.total_cents for p in outcome.packages), default=None)
        for package in outcome.packages:
            rows.append(
                {
                    "sellers": package.seller_count,
                    "totalCents": package.total_cents,
                    "cardsCents": package.cards_cents,
                    "shippingCents": package.shipping_cents,
                    "cards": sum(pick.copies for pick in package.picks),
                    "savingCents": None if previous is None else previous - package.total_cents,
                    "cheapest": package.total_cents == cheapest,
                    "optimal": package.optimal,
                }
            )
            previous = package.total_cents
        return {
            "rows": rows,
            "timedOut": list(outcome.timed_out),
            "partial": found.status is not ScanStatus.COMPLETE,
            "pages": found.pages_read,
            "destination": found.destination,
            "notFound": list(found.not_found),
            "noOffers": [*found.no_buyable, *_left_without_offers(found, outcome.eligible)],
            "overPrice": _over_price(outcome.eligible),
            "unmatched": list(wants.unmatched) if wants else [],
            "quantityMismatch": [
                {"card": m.card, "wanted": m.wanted, "got": m.got}
                for m in (wants.quantity_mismatch if wants else ())
            ],
        }

    def _trends(self, names: Collection[str]) -> dict[str, int | None]:
        """Each card's trend price: the cheapest EUR price Scryfall lists over its printings, which is
        Cardmarket's."""
        try:
            with CardDb(self._card_db) as db:
                found = db.cards_named(names)
        except CardDataError as error:
            log.warning("buy: no trend prices: %s", error)
            return {}
        return {name: info.cheapest_cents for name, info in found.items()}


def _ticked(counts: Counter[str], skipped: frozenset[str] | None) -> Counter[str]:
    """The cards of a deck that are bought: all but the ones left out."""
    return Counter({name: quantity for name, quantity in counts.items() if name not in (skipped or ())})


def _tidy(
    deck_id: int,
    counts: Counter[str],
    left: MutableSet[int],
    skipped: MutableMapping[int, frozenset[str]],
) -> None:
    """Keep a deck's unticked cards to the ones still Don't Have, and say 'the deck is unticked' rather than
    'every card is unticked', so there is one way to leave a whole deck out."""
    kept = frozenset(name for name in skipped.get(deck_id, ()) if name in counts)
    if counts and len(kept) == len(counts):
        left.add(deck_id)
    if deck_id in left or not kept:
        skipped.pop(deck_id, None)
    else:
        skipped[deck_id] = kept


def _deck_json(row: _DeckRow) -> Json:
    deck = row.deck
    cards = [
        {"name": name, "quantity": row.counts[name], "included": name in row.ticked}
        for name in sorted(row.counts, key=str.casefold)
    ]
    return {
        "id": deck.id,
        "short": deck.short_name,
        "name": deck.name,
        "known": row.known,
        "included": row.included,
        "tick": row.tick.value,
        "copies": row.copies,
        "tickedCopies": row.ticked.total(),
        "cards": cards,
    }


def _left_without_offers(found: ScanResult, eligible: Eligible) -> list[str]:
    """Wanted cards that had offers but none from an allowed country or seller who arrives in time. A card
    left only by the price cap is not one of them."""
    kept = {card_key(offer.card) for offer in (*eligible.offers, *eligible.over_price)}
    names = {card_key(offer.card): offer.card for offer in found.offers}
    return sorted((name for key, name in names.items() if key not in kept), key=str.casefold)


def _over_price(eligible: Eligible) -> list[Json]:
    """The wanted cards whose every otherwise allowed offer costs more than the price cap, each with the
    cheapest price found; pass 1 reads a card's cheapest offers, so that is the card's lowest price."""
    kept = {card_key(offer.card) for offer in eligible.offers}
    cheapest: dict[str, tuple[str, int]] = {}
    for offer in eligible.over_price:
        key = card_key(offer.card)
        if key not in kept and (key not in cheapest or offer.price_cents < cheapest[key][1]):
            cheapest[key] = (offer.card, offer.price_cents)
    ordered = sorted(cheapest.values(), key=lambda found: (found[0].casefold(), found[0]))
    return [{"card": name, "cheapestCents": cents} for name, cents in ordered]


def package_check(package: Package) -> Json:
    """What identifies a package: its total and its offers with their copies, in a fixed order. The page
    sends it back with the package it confirms, so a package that changed meanwhile is not the one added."""
    picks = sorted([pick.offer.article_id, pick.copies] for pick in package.picks)
    return {"totalCents": package.total_cents, "picks": picks}


def _package_json(package: Package, trends: dict[str, int | None], eligible: Eligible) -> Json:
    picks_of: dict[str, list[Json]] = defaultdict(list)
    for pick in sorted(package.picks, key=lambda p: p.offer.card.casefold()):
        offer = pick.offer
        picks_of[offer.seller].append(
            {
                "card": offer.card,
                "copies": pick.copies,
                "expansion": offer.expansion,
                "condition": offer.condition.value,
                "language": offer.language,
                "foil": offer.foil,
                "priceCents": offer.price_cents,
                "trendCents": trends.get(offer.card.casefold()),
            }
        )
    estimates = [eligible.delivery.get(shipment.seller) for shipment in package.shipments]
    shipments = [
        {
            "seller": shipment.seller,
            "country": shipment.country,
            "cards": shipment.cards,
            "valueCents": shipment.value_cents,
            "method": shipment.quote.method_name,
            "shippingCents": shipment.quote.price_cents,
            "tracked": shipment.quote.tracked,
            "deliveryDays": estimate.days if estimate else None,
            "deliveryBasis": estimate.basis.value if estimate else None,
            "picks": picks_of[shipment.seller],
        }
        for shipment, estimate in zip(package.shipments, estimates, strict=True)
    ]
    known = [estimate.days for estimate in estimates if estimate]
    return {
        "sellers": package.seller_count,
        "totalCents": package.total_cents,
        "cardsCents": package.cards_cents,
        "shippingCents": package.shipping_cents,
        "optimal": package.optimal,
        "check": package_check(package),
        "slowestDays": max(known, default=None),
        "withoutEstimate": len(estimates) - len(known),
        "leftOutNoEstimate": eligible.no_estimate,
        "shipments": shipments,
        "missing": list(package.missing),
        "short": [{"card": s.card, "wanted": s.wanted, "available": s.available} for s in package.short],
    }


def _cart_json(report: CartRun) -> Json:
    """What the cart run did: card counts by outcome, each offer that did not simply go in with Cardmarket's
    reason, and each package seller's shipping in the cart against the estimate."""
    return {
        "status": report.status.value,
        "reason": report.reason,
        "added": report.copies(CartOutcome.ADDED),
        "already": report.copies(CartOutcome.ALREADY),
        "gone": report.copies(CartOutcome.GONE),
        "refused": report.copies(CartOutcome.REFUSED),
        "short": report.copies(CartOutcome.SHORT),
        "missing": report.copies(CartOutcome.MISSING),
        "noAnswer": report.copies(CartOutcome.NO_ANSWER),
        "notSent": report.copies(CartOutcome.NOT_SENT),
        "items": [
            {
                "seller": item.pick.offer.seller,
                "card": item.pick.offer.card,
                "copies": item.pick.copies,
                "outcome": item.outcome.value,
                "inCart": item.in_cart,
                "message": item.message,
            }
            for item in report.items
            if item.outcome is not CartOutcome.ADDED
        ],
        "shipping": [
            {
                "seller": row.seller,
                "estimateMethod": row.estimate_method,
                "estimateCents": row.estimate_cents,
                "cartMethod": row.cart_method,
                "cartCents": row.cart_cents,
                "differs": row.differs,
            }
            for row in report.shipping
        ],
    }


_CART_CANCELLED_MIDWAY = "Cancelled while a card was being added; check your cart."


def _cart_failure(error: Exception) -> str:
    """The line for a cart run that failed before anything was sent; the error itself goes to the log, where
    the line points."""
    if isinstance(error, (LoadTimeout, BrowserError, PageError, CredentialError)):
        log.warning("buy: the cart run stopped before sending anything: %s", error)
    if isinstance(error, LoadTimeout):
        return "Cardmarket did not answer; check your connection and try again."
    if isinstance(error, BrowserError):
        return "The Cardmarket window failed; the details are in the log."
    if isinstance(error, PageError):
        return "Cardmarket's page was not as expected; the details are in the log."
    if isinstance(error, CredentialError):
        return "The saved Cardmarket sign-in could not be read; see the log."
    log.error("buy: the cart run failed unexpectedly", exc_info=error)
    return "Something went wrong; check your cart on Cardmarket."


def _stopped_text(found: ScanResult) -> tuple[str, str]:
    """The line a cancelled or stopped scan shows, and its longer reason; empty for a complete one."""
    if found.status is ScanStatus.COMPLETE:
        return "", ""
    pages = f"{found.pages_read} {'page' if found.pages_read == 1 else 'pages'}"
    if found.status is ScanStatus.CANCELLED:
        return f"Scan cancelled after {pages}; results are partial.", ""
    return f"Scan stopped after {pages}; results are partial.", found.stop_reason or ""


def _seller_range(packages: Sequence[Package]) -> tuple[int, int]:
    counts = [package.seller_count for package in packages]
    return (min(counts), max(counts)) if counts else (0, 0)


def _close(browser: FormBrowser, what: str) -> None:
    """Close a window; one that will not close must not hide why the run ended."""
    try:
        browser.close()
    except Exception:
        log.exception("buy: could not close the %s window", what)
