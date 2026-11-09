"""The Buy screen's engine: from the decks' Don't Have cards to a package of Cardmarket sellers (ADR 0067).

`BuyService` lists the cards to buy by deck, keeps the filters, and runs one scan at a time on a background
thread: sign in to Cardmarket if the session has ended, fill the app's wants list, read the offers, then work
out the cheapest package for each number of sellers. The page polls `status` while it runs and asks `package`
for the detail of a row. Everything that touches Cardmarket or a window goes through `BuyEngine`, so tests
run the whole flow without either.
"""

import logging
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from commander.buysettings import BuyError, BuyFilters, BuyStore, check_base_sellers, parse_filters
from commander.carddb import CardDataError, CardDb
from commander.cmpages import PageError
from commander.cmscan import ScanProgress, ScanRequest, ScanResult, ScanStage, ScanStatus, scan
from commander.cmsession import (
    BrowserError,
    ChallengeError,
    FormBrowser,
    LoadTimeout,
    SignedOut,
)
from commander.cmshipping import CmShippingError, ShippingMethod
from commander.cmwants import WantsCancelled, WantsProgress, WantsSync, sync_wants
from commander.credentials import CredentialError, SessionStore
from commander.deckexport import dont_have_counts
from commander.jsonshape import ShapeError
from commander.models import Deck
from commander.net import NetworkError
from commander.sellersolve import Package, SolveError, SolveTimeout, WantedCard, curve

log = logging.getLogger(__name__)

# The most sellers the curve shows: each count is one exact solve, and a package of more is not one to post.
MAX_CURVE_SELLERS = 12
SIGN_IN_POLL_S = 0.25

type Json = dict[str, object]
type Key = tuple[object, ...]
type SyncWants = Callable[..., WantsSync]
type Scan = Callable[..., ScanResult]
type Curve = Callable[..., tuple[Package, ...]]


class Phase(Enum):
    IDLE = "idle"
    SIGN_IN = "signin"
    WANTS = "wants"
    SCANNING = "scanning"
    SOLVING = "solving"
    RESULTS = "results"
    CANCELLED = "cancelled"
    STOPPED = "stopped"


RUNNING = frozenset({Phase.SIGN_IN, Phase.WANTS, Phase.SCANNING, Phase.SOLVING})
# How a finished scan ends the run; a scan that did not complete still shows what it read.
_PHASE_OF = {
    ScanStatus.COMPLETE: Phase.RESULTS,
    ScanStatus.CANCELLED: Phase.CANCELLED,
    ScanStatus.STOPPED: Phase.STOPPED,
}


class Closable(Protocol):
    def close(self) -> None: ...


class RouteSource(Protocol):
    """Cardmarket's shipping tables, as far as the solver's routes need them."""

    def country_id(self, name: str) -> int | None: ...

    def methods(self, origin_id: int, destination_id: int) -> tuple[ShippingMethod, ...]: ...


@dataclass(slots=True, frozen=True)
class BuyEngine:
    """Everything the flow reaches outside the process for. The defaults are the real readers; the first four
    have none because they need the app's windows and credentials."""

    open_browser: Callable[[], FormBrowser]
    # Opens the visible sign-in window; its callbacks fire when a signed-in page loads and when the window is
    # closed by the user.
    open_sign_in: Callable[[Callable[[], None], Callable[[], None]], Closable]
    sessions: SessionStore
    shipping: Callable[[], AbstractContextManager[RouteSource]]
    sync_wants: SyncWants = sync_wants
    scan: Scan = scan
    curve: Curve = curve


@dataclass(slots=True, frozen=True)
class _DeckRow:
    deck: Deck
    counts: Counter[str]
    known: bool
    included: bool

    @property
    def copies(self) -> int:
        return self.counts.total()


@dataclass(slots=True, frozen=True)
class _Run:
    wanted: tuple[WantedCard, ...]
    filters: BuyFilters
    base_sellers: int
    key: Key
    cancel: threading.Event = field(default_factory=threading.Event)


@dataclass(slots=True, frozen=True)
class _Outcome:
    packages: tuple[Package, ...]
    scan: ScanResult
    wants: WantsSync | None


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


class _Ended(Exception):  # noqa: N818 - control flow inside one run, never leaves it
    """The run ends here, as `phase` with `message` for the user and `detail` for the log."""

    def __init__(self, phase: Phase, message: str, detail: str = "") -> None:
        super().__init__(detail or message)
        self.phase = phase
        self.message = message
        self.detail = detail or message


def wants_line(progress: WantsProgress) -> str:
    return f"Preparing wants list · {progress.detail}"


def scan_line(progress: ScanProgress) -> str:
    if progress.stage is ScanStage.CARDS:
        return f"Reading {progress.current} · {progress.done + 1} of {progress.planned}"
    return f"Checking {progress.current}'s stock · {progress.done + 1} of {progress.planned}"


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
            "copies": sum(row.copies for row in rows if row.included),
            "filters": self._store.filters().to_json(),
            "baseSellers": self._store.base_sellers(),
            "status": self.status(),
        }

    def include(self, deck_id: int, included: bool) -> Json:  # noqa: FBT001 — the page's bridge passes positionals
        user = self._signed_in_user()
        self._refuse_while_running()
        if not any(row.deck.id == deck_id for row in self._rows(user)):
            raise BuyError("That deck has nothing to buy.")
        left = set(self._store.left_out(user))
        if included:
            left.discard(deck_id)
        else:
            left.add(deck_id)
        self._store.set_left_out(user, frozenset(left))
        log.info("buy: deck %d %s the purchase", deck_id, "joins" if included else "leaves")
        return self.view()

    def set_filters(self, language: object, condition: object, foil: object) -> Json:
        self._signed_in_user()
        self._refuse_while_running()
        filters = parse_filters(language, condition, foil)
        self._store.set_filters(filters)
        log.info("buy: filters now %s", filters.to_json())
        return self.view()

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
        run = _Run(tuple(wanted), filters, base, self._key(user, wanted))
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
        self._background(lambda: self._run(run))
        return self.status()

    def cancel(self) -> Json:
        """Stop after the current page; what was read so far is kept."""
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
            }

    def package(self, sellers: int) -> Json:
        """The detail of the package of this many sellers, with each card's trend price."""
        with self._lock:
            outcome = self._state.outcome
        chosen = next((p for p in outcome.packages if p.seller_count == sellers), None) if outcome else None
        if chosen is None:
            raise BuyError("That package is gone; scan again.")
        trends = self._trends({pick.offer.card for pick in chosen.picks})
        return _package_json(chosen, trends)

    def close(self) -> None:
        """The app is closing: end a running scan at its next page, which closes its windows."""
        self.cancel()

    # --- the cards ------------------------------------------------------------------------------------

    def _signed_in_user(self) -> str:
        user = self._user()
        if not user:
            raise BuyError("Sign in to Archidekt first.")
        return user

    def _rows(self, user: str) -> list[_DeckRow]:
        left = self._store.left_out(user)
        rows: list[_DeckRow] = []
        for deck in self._decks():
            known = deck.labels_known
            counts = dont_have_counts(deck) if known else Counter[str]()
            if known and not counts:
                continue
            rows.append(_DeckRow(deck, counts, known, known and deck.id not in left))
        return sorted(rows, key=lambda row: row.deck.short_name.casefold())

    @staticmethod
    def _wanted(rows: Sequence[_DeckRow]) -> list[WantedCard]:
        total: Counter[str] = Counter()
        for row in rows:
            if row.included:
                total.update(row.counts)
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

    def _run(self, run: _Run) -> None:
        try:
            outcome = self._scan_and_solve(run)
        except _Ended as ended:
            self._finish(run, ended.phase, ended.message, ended.detail, None)
            return
        except Exception as error:  # noqa: BLE001 - nothing may escape a background thread unlogged
            self._finish_with_error(run, error)
            return
        phase = _PHASE_OF[outcome.scan.status]
        message, detail = _stopped_text(outcome.scan)
        self._finish(run, phase, message, detail, outcome)

    def _scan_and_solve(self, run: _Run) -> _Outcome:
        wants, found = self._read_cardmarket_signed_in(run)
        if found.status is not ScanStatus.COMPLETE and not found.offers:
            message, detail = _stopped_text(found)
            raise _Ended(_PHASE_OF[found.status], message, detail)
        return _Outcome(self._solve(run, found), found, wants)

    def _read_cardmarket_signed_in(self, run: _Run) -> tuple[WantsSync, ScanResult]:
        """Fill the wants list and scan, signing in first when there is no session, and once more when the
        saved one turns out to have ended."""
        needs_sign_in = self._engine.sessions.get() is None
        signed_in_here = False
        while True:
            if needs_sign_in:
                self._sign_in(run)
                signed_in_here = True
            try:
                return self._read_cardmarket(run)
            except SignedOut as error:
                if signed_in_here:
                    raise _Ended(
                        Phase.STOPPED, "Cardmarket still asks you to sign in; scan again.", str(error)
                    ) from error
                log.info("buy: the saved Cardmarket session has ended")
                needs_sign_in = True

    def _sign_in(self, run: _Run) -> None:
        self._progress(Phase.SIGN_IN, "Sign in to Cardmarket in the window that opened")
        signed_in, dismissed = threading.Event(), threading.Event()
        window = self._engine.open_sign_in(signed_in.set, dismissed.set)
        log.info("buy: waiting for the sign-in to Cardmarket")
        try:
            while not signed_in.is_set():
                if run.cancel.is_set():
                    raise _Ended(Phase.CANCELLED, "Sign-in cancelled.")
                if dismissed.is_set():
                    raise _Ended(Phase.CANCELLED, "Sign-in window closed; scan to sign in again.")
                signed_in.wait(SIGN_IN_POLL_S)
        finally:
            _close(window, "sign-in")
        log.info("buy: signed in to Cardmarket")

    def _read_cardmarket(self, run: _Run) -> tuple[WantsSync, ScanResult]:
        self._progress(Phase.WANTS, "Opening the Cardmarket window")
        browser = self._engine.open_browser()
        try:
            return self._sync_and_scan(browser, run)
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
                language=filters.language.cardmarket_id,
                min_condition=filters.min_condition,
                foil=filters.foil,
                cancel=run.cancel,
                on_progress=wants_progress,
            )
        except WantsCancelled as error:
            raise _Ended(Phase.CANCELLED, "Scan cancelled.") from error
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

    def _solve(self, run: _Run, found: ScanResult) -> tuple[Package, ...]:
        self._progress(Phase.SOLVING, "Finding the cheapest packages")
        if not found.offers:
            return ()
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

                packages = self._engine.curve(
                    list(run.wanted), found.offers, routes, max_sellers=MAX_CURVE_SELLERS
                )
        except (NetworkError, ShapeError, CmShippingError) as error:
            raise _Ended(
                Phase.STOPPED, "Shipping prices could not be fetched; scan again.", str(error)
            ) from error
        except (SolveError, SolveTimeout) as error:
            raise _Ended(
                Phase.STOPPED, "The solver failed; the details are in the log.", str(error)
            ) from error
        log.info("buy: %d packages, from %d to %d sellers", len(packages), *_seller_range(packages))
        return packages

    # --- state ----------------------------------------------------------------------------------------

    def _progress(self, phase: Phase, line: str) -> None:
        with self._lock:
            self._state.phase = phase
            self._state.progress = line
        log.debug("buy: %s", line)

    def _finish(self, run: _Run, phase: Phase, message: str, detail: str, outcome: _Outcome | None) -> None:
        log.log(
            logging.INFO if phase in (Phase.RESULTS, Phase.CANCELLED) else logging.WARNING,
            "buy: %s%s",
            phase.value,
            f": {detail}" if detail else "",
        )
        with self._lock:
            self._state = _State(phase, "", message, detail, outcome, key=run.key)

    def _finish_with_error(self, run: _Run, error: Exception) -> None:
        if isinstance(error, ChallengeError):
            text = "Cardmarket is checking your browser; wait a minute, then scan again."
        elif isinstance(error, LoadTimeout):
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
            "pages": found.pages_read,
            "destination": found.destination,
            "notFound": list(found.not_found),
            "noOffers": list(found.no_buyable),
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


def _deck_json(row: _DeckRow) -> Json:
    deck = row.deck
    cards = [{"name": name, "quantity": row.counts[name]} for name in sorted(row.counts, key=str.casefold)]
    return {
        "id": deck.id,
        "short": deck.short_name,
        "name": deck.name,
        "known": row.known,
        "included": row.included,
        "copies": row.copies,
        "cards": cards,
    }


def _package_json(package: Package, trends: dict[str, int | None]) -> Json:
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
    shipments = [
        {
            "seller": shipment.seller,
            "country": shipment.country,
            "cards": shipment.cards,
            "valueCents": shipment.value_cents,
            "method": shipment.quote.method_name,
            "shippingCents": shipment.quote.price_cents,
            "tracked": shipment.quote.tracked,
            "picks": picks_of[shipment.seller],
        }
        for shipment in package.shipments
    ]
    return {
        "sellers": package.seller_count,
        "totalCents": package.total_cents,
        "cardsCents": package.cards_cents,
        "shippingCents": package.shipping_cents,
        "optimal": package.optimal,
        "shipments": shipments,
        "missing": list(package.missing),
        "short": [{"card": s.card, "wanted": s.wanted, "available": s.available} for s in package.short],
    }


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


def _close(closable: Closable, what: str) -> None:
    """Close a window; one that will not close must not hide why the run ended."""
    try:
        closable.close()
    except Exception:
        log.exception("buy: could not close the %s window", what)
