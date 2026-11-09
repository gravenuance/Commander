"""The Buy screen's engine, from the cards to buy to a package of sellers, with Cardmarket and its windows
replaced by fakes (the real readers have their own tests); the solver and the card data are real."""

import json
import logging
import re
import threading
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from commander.buy import BuyEngine, BuyService, RouteSource
from commander.buypane import PaneArea
from commander.buysettings import BuyError, BuyStore
from commander.buywindows import CardmarketPane, production_engine
from commander.cmcart import CartOutcome, CartProgress, CartRun, CartStatus, ItemResult, ShippingResult
from commander.cmpages import Condition, Offer, PageError, Want
from commander.cmscan import Foil, ScanProgress, ScanRequest, ScanResult, ScanStage, ScanStatus
from commander.cmsession import BrowserError, Cancelled, FormBrowser, Handover, Interrupted, LoadTimeout, Need
from commander.cmshipping import CardmarketShipping, Country, ShippingMethod
from commander.cmwants import QuantityMismatch, WantsCancelled, WantsProgress, WantsStep, WantsSync
from commander.credentials import CredentialError
from commander.models import Deck, DeckCard
from commander.net import NetworkError
from commander.offerlimits import OfferLimits
from commander.sellersolve import CurveResult, Package, WantedCard, solve_curve
from test_sellersolve import LETTER, offer

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)

DONT_HAVE = "Don't Have"
MAX_WORDS = 12
Json = dict[str, Any]
DEFAULT_FILTERS: Json = {
    "language": "english",
    "condition": "NM",
    "foil": "any",
    "countries": [],
    "delivery": "any",
    "maxPriceCents": None,
}


def card(name: str, quantity: int, label: str | None, *categories: str, in_deck: bool = True) -> DeckCard:
    return DeckCard(
        name, quantity, categories or ("Ramp",), in_deck, "sid", 1, 1, "tst", "1", "Normal", label
    )


def make_deck(deck_id: int, commander: str, *cards: DeckCard) -> Deck:
    return Deck(
        deck_id,
        f"{commander} deck",
        "MAKKIS",
        4,
        "2026-11-01",
        (card(commander, 1, "", "Commander"), *cards),
    )


def want(name: str, quantity: int = 1) -> DeckCard:
    return card(name, quantity, DONT_HAVE)


def fresh_decks() -> list[Deck]:
    return [
        make_deck(
            2, "Pantlaza, Sun-Favored", want("Mind Stone"), want("Arcane Signet", 2), card("Island", 9, "")
        ),
        make_deck(
            1,
            "Ellivere of the Wild Court",
            want("Arcane Signet"),
            want("Lotus Petal"),
            card("Maybe", 1, DONT_HAVE, "Maybeboard", in_deck=False),
        ),
        make_deck(3, "Owned Commander", card("Sol Ring", 1, "")),
    ]


def assert_short(message: object) -> None:
    assert isinstance(message, str)
    assert 0 < len(message.split()) <= MAX_WORDS, message


@dataclass(slots=True)
class Closer:
    events: list[str]
    name: str
    # Set once the window is closed, for a test that waits for a scan on its own thread to end.
    done: threading.Event | None = None

    def close(self) -> None:
        self.events.append(f"{self.name} closed")
        if self.done is not None:
            self.done.set()


class Routes:
    """Cardmarket's shipping tables: Sweden to France, every route the same letter."""

    def __init__(self) -> None:
        self.asked: list[tuple[int, int]] = []

    COUNTRIES = (
        Country(1, "Austria"),
        Country(7, "Germany"),
        Country(12, "France"),
        Country(17, "Italy"),
        Country(23, "Netherlands"),
        Country(28, "Sweden"),
    )

    def country_id(self, name: str) -> int | None:
        return {country.name: country.id for country in self.COUNTRIES}.get(name)

    def countries(self) -> tuple[Country, ...]:
        return self.COUNTRIES

    def methods(self, origin_id: int, destination_id: int) -> tuple[ShippingMethod, ...]:
        self.asked.append((origin_id, destination_id))
        return (LETTER,)


class OfflineRoutes(Routes):
    """Shipping tables that cannot be fetched."""

    def methods(self, origin_id: int, destination_id: int) -> tuple[ShippingMethod, ...]:  # noqa: ARG002
        raise NetworkError("offline")


def scan_of(
    offers: Sequence[Offer],
    *,
    status: ScanStatus = ScanStatus.COMPLETE,
    reason: str | None = None,
    destination: str | None = "Sweden",
    pages: int = 5,
    not_found: tuple[str, ...] = (),
    no_buyable: tuple[str, ...] = (),
) -> ScanResult:
    return ScanResult(
        offers=tuple(offers),
        destination=destination,
        status=status,
        stop_reason=reason,
        pages_read=pages,
        not_found=not_found,
        no_buyable=no_buyable,
        base_sellers=(),
        cards_with_more=(),
        sellers_with_more_pages=(),
    )


def imported(list_id: int = 7, names: Sequence[str] = ("Arcane Signet",)) -> WantsSync:
    wants = tuple(Want(f"w{i}", name, 1, ("English",), Condition.NM) for i, name in enumerate(names))
    return WantsSync(list_id, wants, (), ())


@dataclass(slots=True)
class SyncCall:
    wanted: list[WantedCard]
    languages: tuple[int, ...]
    min_condition: Condition | None
    foil: Foil


class FakePane:
    """Records the areas the page reports for the Cardmarket window."""

    def __init__(self) -> None:
        self.areas: list[PaneArea | None] = []

    def set_area(self, area: PaneArea | None) -> None:
        self.areas.append(area)


class Rig:
    """A service whose Cardmarket is a set of fakes, and a record of what it was asked."""

    def __init__(self, tmp_path: Path, card_db: Path) -> None:
        self.decks = fresh_decks()
        self.events: list[str] = []
        self.browser_closed = threading.Event()
        self.progress: list[str] = []
        # The handover the engine gave the scan window; a fake step calls it as the real window would.
        self.handover: Handover | None = None
        self.pane = FakePane()
        self.sync_calls: list[SyncCall] = []
        self.scan_requests: list[ScanRequest] = []
        self.routes = Routes()
        self.settings_path = tmp_path / "settings" / "buy.json"
        self.card_db = card_db
        # What the fakes do; a test replaces what it needs.
        self.sync_result = imported()
        self.scan_result = scan_of([])
        self.sync_effect: Callable[[threading.Event], None] = lambda _cancel: None
        self.scan_effect: Callable[[threading.Event], None] = lambda _cancel: None
        # The `cancelled` callables the solver was given, and the counts it reports as timed out.
        self.curve_cancels: list[Callable[[], bool]] = []
        self.curve_timed_out: tuple[int, ...] = ()
        self.curve_effect: Callable[[], object] = lambda: None
        self.cart_calls: list[Package] = []
        self.cart_effect: Callable[[threading.Event], None] = lambda _cancel: None
        self.cart_result: Callable[[Package], CartRun] = carted_everything
        self.service = self.build()

    def build(self) -> BuyService:
        self.engine = BuyEngine(
            open_browser=self._open_browser,
            pane=self.pane,
            shipping=self._shipping,
            sync_wants=self._sync_wants,
            scan=self._scan,
            solve_curve=self._solve_curve,
            add_to_cart=self._add_to_cart,
        )
        return BuyService(
            store=BuyStore(self.settings_path),
            decks=lambda: self.decks,
            user=lambda: "MAKKIS",
            engine=self.engine,
            card_db=self.card_db,
            background=lambda work: work(),
        )

    def _open_browser(self, handover: Handover) -> FormBrowser:
        self.events.append("browser opened")
        self.handover = handover
        return cast("FormBrowser", Closer(self.events, "browser", self.browser_closed))

    @contextmanager
    def _shipping(self) -> Generator[RouteSource]:
        self.events.append("shipping")
        yield self.routes

    def _sync_wants(
        self,
        _browser: object,
        wanted: list[WantedCard],
        *,
        languages: tuple[int, ...],
        min_condition: Condition | None,
        foil: Foil,
        cancel: threading.Event,
        on_progress: Callable[[WantsProgress], None],
    ) -> WantsSync:
        self.events.append("sync")
        self.sync_calls.append(SyncCall(wanted, languages, min_condition, foil))
        on_progress(WantsProgress(WantsStep.IMPORT, "3 cards"))
        self.progress.append(str(self.service.status()["progress"]))
        self.sync_effect(cancel)
        if cancel.is_set():
            raise WantsCancelled
        return self.sync_result

    def _scan(
        self,
        _browser: object,
        request: ScanRequest,
        *,
        cancel: threading.Event,
        on_progress: Callable[[ScanProgress], None],
    ) -> ScanResult:
        self.events.append("scan")
        self.scan_requests.append(request)
        on_progress(ScanProgress(ScanStage.CARDS, 11, 40, "Sylvan Safekeeper"))
        self.progress.append(str(self.service.status()["progress"]))
        on_progress(ScanProgress(ScanStage.SELLERS, 41, 55, "Seller01"))
        self.progress.append(str(self.service.status()["progress"]))
        self.scan_effect(cancel)
        return self.scan_result

    def _solve_curve(
        self,
        wants: Sequence[WantedCard],
        offers: Sequence[Offer],
        routes: Callable[[str], tuple[ShippingMethod, ...]],
        *,
        max_sellers: int,
        cancelled: Callable[[], bool],
    ) -> CurveResult:
        """The real solver, with the counts a test wants reported as timed out."""
        self.curve_cancels.append(cancelled)
        self.curve_effect()
        found = solve_curve(wants, offers, routes, max_sellers=max_sellers, cancelled=cancelled)
        return replace(found, timed_out=self.curve_timed_out)

    def _add_to_cart(
        self,
        _browser: object,
        package: Package,
        *,
        cancel: threading.Event,
        on_progress: Callable[[CartProgress], None],
    ) -> CartRun:
        self.events.append("cart")
        self.cart_calls.append(package)
        on_progress(CartProgress("Arcane Signet", 0, 2))
        self.progress.append(str(self.service.status()["progress"]))
        self.cart_effect(cancel)
        return self.cart_result(package)


def carted_everything(package: Package) -> CartRun:
    """The cart run of a package whose every offer went in."""
    items = tuple(ItemResult(pick, CartOutcome.ADDED, pick.copies) for pick in package.picks)
    return CartRun(CartStatus.COMPLETE, "", items, ())


@pytest.fixture
def rig(tmp_path: Path, card_db_file: Path) -> Rig:
    return Rig(tmp_path, card_db_file)


def two_sellers() -> list[Offer]:
    """Seller A has both cards (Arcane Signet dear), B has the Signet cheap and the Stone dear."""
    return [
        offer("Arcane Signet", "A", 500, quantity=2),
        offer("Mind Stone", "A", 60),
        offer("Arcane Signet", "B", 30, quantity=3),
        offer("Mind Stone", "B", 900),
    ]


def scanned(rig: Rig) -> Json:
    rig.scan_result = scan_of(two_sellers())
    rig.service.start()
    return rig.service.status()


def test_the_production_engine_reads_shipping_through_the_cache_and_reports_the_pane_to_the_window_cover(
    tmp_path: Path,
) -> None:
    engine = production_engine(httpx.Client, tmp_path, lambda: NOW)

    assert isinstance(engine.pane, CardmarketPane)
    with engine.shipping() as shipping:
        assert isinstance(shipping, CardmarketShipping)


class TestTheCardsToBuy:
    def test_each_deck_lists_its_dont_have_cards_and_the_decks_without_any_are_left_out(
        self, rig: Rig
    ) -> None:
        view = rig.service.view()

        assert view["decks"] == [
            {
                "id": 1,
                "short": "Ellivere",
                "name": "Ellivere of the Wild Court deck",
                "known": True,
                "included": True,
                "tick": "all",
                "copies": 2,
                "tickedCopies": 2,
                "cards": [
                    {"name": "Arcane Signet", "quantity": 1, "included": True},
                    {"name": "Lotus Petal", "quantity": 1, "included": True},
                ],
            },
            {
                "id": 2,
                "short": "Pantlaza",
                "name": "Pantlaza, Sun-Favored deck",
                "known": True,
                "included": True,
                "tick": "all",
                "copies": 3,
                "tickedCopies": 3,
                "cards": [
                    {"name": "Arcane Signet", "quantity": 2, "included": True},
                    {"name": "Mind Stone", "quantity": 1, "included": True},
                ],
            },
        ]
        assert view["copies"] == 5

    def test_a_deck_whose_labels_were_never_kept_is_listed_but_not_ticked(self, rig: Rig) -> None:
        rig.decks.append(make_deck(4, "Old Deck", card("Sol Ring", 1, None), card("Island", 1, None)))

        old = next(d for d in cast("list[Json]", rig.service.view()["decks"]) if d["id"] == 4)

        assert (old["known"], old["included"], old["tick"], old["copies"], old["cards"]) == (
            False,
            False,
            "none",
            0,
            [],
        )

    def test_unticking_a_deck_leaves_its_cards_out_and_is_remembered(self, rig: Rig, tmp_path: Path) -> None:
        view = rig.service.include(2, included=False)

        decks = cast("list[Json]", view["decks"])
        assert [(d["id"], d["included"]) for d in decks] == [(1, True), (2, False)]
        assert view["copies"] == 2
        # A new service on the same file, as after a restart, finds the deck still left out.
        again = Rig(tmp_path, rig.card_db)
        assert [(d["id"], d["included"]) for d in cast("list[Json]", again.service.view()["decks"])] == [
            (1, True),
            (2, False),
        ]

    def test_ticking_a_deck_again_takes_it_back(self, rig: Rig) -> None:
        rig.service.include(2, included=False)

        view = rig.service.include(2, included=True)

        assert [d["included"] for d in cast("list[Json]", view["decks"])] == [True, True]
        saved = json.loads(rig.settings_path.read_text(encoding="utf-8"))
        assert (saved["leftOut"], saved["leftOutCards"]) == ({}, {})

    def test_a_deck_with_nothing_to_buy_cannot_be_ticked(self, rig: Rig) -> None:
        with pytest.raises(BuyError, match="nothing to buy"):
            rig.service.include(3, included=False)

    def test_the_cards_of_ticked_decks_are_summed_into_one_want_each(self, rig: Rig) -> None:
        rig.service.include(1, included=False)
        rig.service.include(1, included=True)

        rig.service.start()

        [call] = rig.sync_calls
        assert call.wanted == [
            WantedCard("Arcane Signet", 3),
            WantedCard("Lotus Petal", 1),
            WantedCard("Mind Stone", 1),
        ]

    def test_with_every_deck_unticked_there_is_nothing_to_scan(self, rig: Rig) -> None:
        rig.service.include(1, included=False)
        rig.service.include(2, included=False)

        with pytest.raises(BuyError, match=re.escape("No cards to buy; tick a deck first.")):
            rig.service.start()

        assert rig.events == []


class TestTheFilters:
    def test_the_defaults_are_english_near_mint_or_better_and_any_finish_with_fifteen_base_sellers(
        self, rig: Rig
    ) -> None:
        view = rig.service.view()

        assert view["filters"] == DEFAULT_FILTERS
        assert view["baseSellers"] == 15

    def test_chosen_filters_are_saved_and_found_again_by_a_new_service(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        rig.service.set_filters("any", "any", "non-foil", [23, 7], "10")
        rig.service.set_base_sellers(20)

        view = Rig(tmp_path, rig.card_db).service.view()

        assert view["filters"] == {
            "language": "any",
            "condition": "any",
            "foil": "non-foil",
            "countries": [7, 23],
            "delivery": "10",
            "maxPriceCents": None,
        }
        assert view["baseSellers"] == 20

    def test_the_scan_gets_the_filters_chosen(self, rig: Rig) -> None:
        rig.service.set_filters("english", "EX", "foil", [], "any")
        rig.service.set_base_sellers(9)

        rig.service.start()

        [call] = rig.sync_calls
        assert (call.languages, call.min_condition, call.foil) == ((1,), Condition.EX, Foil.FOIL)
        [request] = rig.scan_requests
        assert (request.filters.languages, request.filters.min_condition, request.filters.foil) == (
            (1,),
            Condition.EX,
            Foil.FOIL,
        )
        assert request.base_sellers == 9

    def test_any_language_and_condition_reach_the_scan_as_no_filter(self, rig: Rig) -> None:
        rig.service.set_filters("any", "any", "any", [], "any")

        rig.service.start()

        [call] = rig.sync_calls
        assert (call.languages, call.min_condition, call.foil) == ((), None, Foil.ANY)
        [request] = rig.scan_requests
        assert request.filters.languages == ()

    @pytest.mark.parametrize(
        ("choice", "ids"),
        [("english", (1,)), ("japanese", (7,)), ("both", (1, 7)), ("any", ())],
    )
    def test_each_language_choice_reaches_the_wants_list_and_the_scan_as_its_ids(
        self, rig: Rig, choice: str, ids: tuple[int, ...]
    ) -> None:
        rig.service.set_filters(choice, "NM", "any", [], "any")

        rig.service.start()

        [call] = rig.sync_calls
        [request] = rig.scan_requests
        assert (call.languages, request.filters.languages) == (ids, ids)

    @pytest.mark.parametrize("choice", ["english", "japanese", "both", "any"])
    def test_each_language_choice_is_saved_and_found_again(
        self, rig: Rig, tmp_path: Path, choice: str
    ) -> None:
        rig.service.set_filters(choice, "NM", "any", [], "any")

        view = Rig(tmp_path, rig.card_db).service.view()

        assert cast("Json", view["filters"])["language"] == choice

    @pytest.mark.parametrize(
        "choices",
        [
            ("french", "NM", "any", [], "any"),
            ("Japanese", "NM", "any", [], "any"),
            ("english,japanese", "NM", "any", [], "any"),
            (["english", "japanese"], "NM", "any", [], "any"),
            ("english", "XX", "any", [], "any"),
            ("english", "NM", "shiny", [], "any"),
            (1, "NM", "any", [], "any"),
            ("english", "NM", "any", [], "3"),
            ("english", "NM", "any", [], 7),
            ("english", "NM", "any", [], None),
            ("english", "NM", "any", "7", "any"),
            ("english", "NM", "any", None, "any"),
            ("english", "NM", "any", [0], "any"),
            ("english", "NM", "any", [-3], "any"),
            ("english", "NM", "any", ["7"], "any"),
            ("english", "NM", "any", [True], "any"),
            ("english", "NM", "any", [7.0], "any"),
            ("english", "NM", "any", [10_000], "any"),
            ("english", "NM", "any", list(range(1, 102)), "any"),
        ],
    )
    def test_a_choice_that_is_not_offered_is_refused_and_nothing_is_saved(
        self, rig: Rig, choices: tuple[object, object, object, object, object]
    ) -> None:
        with pytest.raises(BuyError, match="not available"):
            rig.service.set_filters(*choices)

        assert not rig.settings_path.exists()

    def test_a_country_chosen_twice_counts_once(self, rig: Rig) -> None:
        view = rig.service.set_filters("english", "NM", "any", [23, 7, 23], "any")

        assert cast("Json", view["filters"])["countries"] == [7, 23]

    @pytest.mark.parametrize("count", [0, 41, -3, "9", None, True, 2.5])
    def test_base_sellers_outside_one_to_forty_are_refused(self, rig: Rig, count: object) -> None:
        with pytest.raises(BuyError, match=re.escape("Base sellers must be 1 to 40.")):
            rig.service.set_base_sellers(count)

    def test_a_damaged_settings_file_is_kept_aside_and_the_defaults_used(
        self, rig: Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        rig.settings_path.parent.mkdir(parents=True)
        rig.settings_path.write_text("{not json", encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            view = rig.build().view()

        assert view["filters"] == DEFAULT_FILTERS
        assert rig.settings_path.with_name("buy.json.bak").read_text(encoding="utf-8") == "{not json"
        assert "unusable" in caplog.text

    def test_a_file_of_a_newer_format_is_kept_aside_too(self, rig: Rig) -> None:
        rig.settings_path.parent.mkdir(parents=True)
        rig.settings_path.write_text('{"version": 4, "baseSellers": 30}', encoding="utf-8")

        view = rig.build().view()

        assert view["baseSellers"] == 15
        assert rig.settings_path.with_name("buy.json.bak").exists()

    def test_a_format_one_file_is_read_with_any_country_and_any_delivery_and_kept_as_it_was(
        self, rig: Rig
    ) -> None:
        old = (
            '{"version": 1, "filters": {"language": "any", "condition": "EX", "foil": "foil"},'
            ' "baseSellers": 30, "leftOut": {"makkis": [2]}}'
        )
        rig.settings_path.parent.mkdir(parents=True)
        rig.settings_path.write_text(old, encoding="utf-8")

        view = rig.build().view()

        assert view["filters"] == {
            "language": "any",
            "condition": "EX",
            "foil": "foil",
            "countries": [],
            "delivery": "any",
            "maxPriceCents": None,
        }
        assert view["baseSellers"] == 30
        assert [d["included"] for d in cast("list[Json]", view["decks"])] == [True, False]
        assert rig.settings_path.with_name("buy.json.v1.bak").read_text(encoding="utf-8") == old
        assert not rig.settings_path.with_name("buy.json.bak").exists()

    def test_a_format_one_file_is_written_back_as_format_three_by_the_next_change(self, rig: Rig) -> None:
        rig.settings_path.parent.mkdir(parents=True)
        rig.settings_path.write_text('{"version": 1, "baseSellers": 30}', encoding="utf-8")
        service = rig.build()

        service.set_filters("english", "NM", "any", [28], "14")

        saved = json.loads(rig.settings_path.read_text(encoding="utf-8"))
        assert saved["version"] == 3
        assert saved["filters"]["countries"] == [28]
        assert saved["filters"]["maxPriceCents"] is None
        assert saved["baseSellers"] == 30

    def test_a_format_two_file_is_read_with_no_price_cap_and_no_cards_left_out_and_kept_as_it_was(
        self, rig: Rig
    ) -> None:
        old = (
            '{"version": 2, "filters": {"language": "english", "condition": "NM", "foil": "any",'
            ' "countries": [7], "delivery": "10"}, "baseSellers": 20, "leftOut": {"makkis": [2]}}'
        )
        rig.settings_path.parent.mkdir(parents=True)
        rig.settings_path.write_text(old, encoding="utf-8")

        view = rig.build().view()

        assert view["filters"] == {**DEFAULT_FILTERS, "countries": [7], "delivery": "10"}
        assert view["baseSellers"] == 20
        decks = cast("list[Json]", view["decks"])
        assert [(d["id"], d["tick"]) for d in decks] == [(1, "all"), (2, "none")]
        assert rig.settings_path.with_name("buy.json.v2.bak").read_text(encoding="utf-8") == old

    def test_a_price_cap_and_the_cards_left_out_are_found_again_by_a_new_service(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        rig.service.set_filters("english", "NM", "any", [], "any", max_price="2,50")
        rig.service.include_card(2, "Mind Stone", included=False)

        view = Rig(tmp_path, rig.card_db).service.view()

        assert cast("Json", view["filters"])["maxPriceCents"] == 250
        decks = cast("list[Json]", view["decks"])
        assert [(d["id"], d["tick"]) for d in decks] == [(1, "all"), (2, "some")]
        saved = json.loads(rig.settings_path.read_text(encoding="utf-8"))
        assert saved["leftOutCards"] == {"makkis": {"2": ["Mind Stone"]}}

    @pytest.mark.parametrize("cents", [0, -5, 1_000_001, "5", 2.5, True, [500]])
    def test_a_price_cap_in_the_file_that_is_no_price_falls_back_to_any_price(
        self, rig: Rig, cents: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        rig.settings_path.parent.mkdir(parents=True)
        document = {"version": 3, "filters": {"foil": "foil", "maxPriceCents": cents}}
        rig.settings_path.write_text(json.dumps(document), encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            view = rig.build().view()

        assert cast("Json", view["filters"])["maxPriceCents"] is None
        assert cast("Json", view["filters"])["foil"] == "foil"
        assert "maxPriceCents" in caplog.text

    @pytest.mark.parametrize(("text", "cents"), [("5", 500), ("2,50", 250), ("2.5", 250), ("0.01", 1)])
    def test_a_price_is_taken_in_euros_with_a_comma_or_a_dot(self, rig: Rig, text: str, cents: int) -> None:
        view = rig.service.set_filters("english", "NM", "any", [], "any", max_price=text)

        assert cast("Json", view["filters"])["maxPriceCents"] == cents

    @pytest.mark.parametrize("text", [None, "", "  ", "€"])
    def test_a_blank_price_is_any_price(self, rig: Rig, text: str | None) -> None:
        rig.service.set_filters("english", "NM", "any", [], "any", max_price="3")

        view = rig.service.set_filters("english", "NM", "any", [], "any", max_price=text)

        assert cast("Json", view["filters"])["maxPriceCents"] is None

    def test_the_boundaries_of_a_price_are_one_cent_and_ten_thousand_euros(self, rig: Rig) -> None:
        low = rig.service.set_filters("english", "NM", "any", [], "any", max_price="0,01")
        high = rig.service.set_filters("english", "NM", "any", [], "any", max_price="10000")

        assert cast("Json", low["filters"])["maxPriceCents"] == 1
        assert cast("Json", high["filters"])["maxPriceCents"] == 1_000_000

    @pytest.mark.parametrize(
        "text",
        ["0", "0,00", "-1", "10000.01", "10001", "2,555", "1,", ",5", "5 euros", "1e2", "٣", "1 000", 5],
    )
    def test_a_price_that_is_not_one_to_ten_thousand_euros_in_two_decimals_is_refused_and_not_saved(
        self, rig: Rig, text: object
    ) -> None:
        said = re.escape("Max price: 0.01 to 10000 euros, at most two decimals.")
        with pytest.raises(BuyError, match=said) as error:
            rig.service.set_filters("english", "NM", "any", [], "any", max_price=text)

        assert_short(str(error.value))
        assert not rig.settings_path.exists()

    def test_only_the_latest_migration_backup_is_kept(self, rig: Rig) -> None:
        rig.settings_path.parent.mkdir(parents=True)
        stale = rig.settings_path.with_name("buy.json.v0.bak")
        stale.write_text("old", encoding="utf-8")
        rig.settings_path.write_text('{"version": 1}', encoding="utf-8")

        rig.build().view()

        assert not stale.exists()
        assert rig.settings_path.with_name("buy.json.v1.bak").exists()

    def test_a_current_format_file_makes_no_backup(self, rig: Rig) -> None:
        rig.service.set_filters("english", "NM", "any", [7], "any")

        rig.build().view()

        assert list(rig.settings_path.parent.glob("*.bak")) == []

    def test_wrong_countries_and_delivery_in_the_file_fall_back_and_the_rest_stays(
        self, rig: Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        rig.settings_path.parent.mkdir(parents=True)
        rig.settings_path.write_text(
            '{"version": 2, "filters": {"countries": [7, "x", 0, true, 23], "delivery": "3"}}',
            encoding="utf-8",
        )

        with caplog.at_level(logging.WARNING):
            view = rig.build().view()

        assert cast("Json", view["filters"])["countries"] == [7, 23]
        assert cast("Json", view["filters"])["delivery"] == "any"
        assert "no country id" in caplog.text
        assert "'3'" in caplog.text

    def test_countries_that_are_no_list_fall_back_to_any_country(self, rig: Rig) -> None:
        rig.settings_path.parent.mkdir(parents=True)
        rig.settings_path.write_text(
            '{"version": 2, "filters": {"countries": 7, "foil": "foil"}}', encoding="utf-8"
        )

        view = rig.build().view()

        assert cast("Json", view["filters"])["countries"] == []
        assert cast("Json", view["filters"])["foil"] == "foil"

    def test_a_wrong_field_falls_back_to_its_default_and_the_rest_stays(
        self, rig: Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        rig.settings_path.parent.mkdir(parents=True)
        rig.settings_path.write_text(
            '{"version": 1, "filters": {"language": "klingon", "condition": "EX", "foil": "foil"},'
            ' "baseSellers": 500, "leftOut": {"makkis": [2, "x"]}}',
            encoding="utf-8",
        )

        with caplog.at_level(logging.WARNING):
            view = rig.build().view()

        assert view["filters"] == {**DEFAULT_FILTERS, "condition": "EX", "foil": "foil"}
        assert view["baseSellers"] == 15
        assert [(d["id"], d["included"]) for d in cast("list[Json]", view["decks"])] == [
            (1, True),
            (2, False),
        ]
        assert "klingon" in caplog.text

    @pytest.mark.parametrize("language", ["english", "japanese", "both", "any"])
    def test_every_language_choice_in_a_current_file_is_read_as_it_was_written(
        self, rig: Rig, language: str
    ) -> None:
        rig.settings_path.parent.mkdir(parents=True)
        rig.settings_path.write_text(
            f'{{"version": 3, "filters": {{"language": "{language}"}}}}', encoding="utf-8"
        )

        view = rig.build().view()

        assert cast("Json", view["filters"])["language"] == language
        assert not rig.settings_path.with_name("buy.json.v3.bak").exists()

    @pytest.mark.parametrize("language", ["Japanese", "english,japanese", "", 7, ["both"]])
    def test_a_language_that_is_none_of_the_four_falls_back_to_english_and_says_so(
        self, rig: Rig, caplog: pytest.LogCaptureFixture, language: object
    ) -> None:
        rig.settings_path.parent.mkdir(parents=True)
        rig.settings_path.write_text(
            json.dumps({"version": 3, "filters": {"language": language, "foil": "foil"}}), encoding="utf-8"
        )

        with caplog.at_level(logging.WARNING):
            view = rig.build().view()

        assert cast("Json", view["filters"])["language"] == "english"
        assert cast("Json", view["filters"])["foil"] == "foil"
        assert "filters.language" in caplog.text

    def test_the_decks_left_out_belong_to_the_account(self, rig: Rig, tmp_path: Path) -> None:
        rig.service.include(2, included=False)
        other = Rig(tmp_path, rig.card_db)
        other.service = BuyService(
            store=BuyStore(rig.settings_path),
            decks=lambda: rig.decks,
            user=lambda: "SOMEONE",
            engine=rig.engine,
            card_db=rig.card_db,
            background=lambda work: work(),
        )

        assert [d["included"] for d in cast("list[Json]", other.service.view()["decks"])] == [True, True]

    def test_a_save_that_fails_is_told_and_changes_nothing(self, rig: Rig) -> None:
        rig.settings_path.parent.parent.mkdir(parents=True, exist_ok=True)
        rig.settings_path.parent.write_text("a file where the folder should be", encoding="utf-8")

        with pytest.raises(BuyError, match=re.escape("Could not save your choices; see the log.")):
            rig.service.set_base_sellers(9)

        assert rig.service.view()["baseSellers"] == 15


class TestTheScan:
    def test_a_scan_with_a_session_fills_the_list_scans_and_solves_with_every_window_closed(
        self, rig: Rig
    ) -> None:
        status = scanned(rig)

        assert rig.events == ["browser opened", "sync", "scan", "browser closed", "shipping"]
        assert (status["phase"], status["running"], status["message"]) == ("results", False, "")
        [request] = rig.scan_requests
        assert request.wants_list_id == 7
        assert [(w.name, w.quantity) for w in request.wanted] == [
            ("Arcane Signet", 3),
            ("Lotus Petal", 1),
            ("Mind Stone", 1),
        ]

    def test_the_progress_lines_name_the_card_and_count_pages(self, rig: Rig) -> None:
        scanned(rig)

        assert rig.progress == [
            "Preparing wants list · 3 cards",
            "Reading Sylvan Safekeeper · 12 of 40",
            "Checking Seller01's stock · 42 of 55",
        ]

    def test_the_curve_has_a_row_per_seller_count_with_the_saving_against_the_row_before(
        self, rig: Rig
    ) -> None:
        rig.decks = [make_deck(1, "Ellivere", want("Arcane Signet"), want("Mind Stone"))]
        rig.scan_result = scan_of(two_sellers())

        rig.service.start()
        results = cast("Json", rig.service.status()["results"])

        # One seller: A has both, 5,00 + 0,60 and 2,00 shipping. Two: Signet 0,30 from B, Stone 0,60 from A.
        assert results["rows"] == [
            {
                "sellers": 1,
                "totalCents": 760,
                "cardsCents": 560,
                "shippingCents": 200,
                "cards": 2,
                "savingCents": None,
                "cheapest": False,
                "optimal": True,
            },
            {
                "sellers": 2,
                "totalCents": 490,
                "cardsCents": 90,
                "shippingCents": 400,
                "cards": 2,
                "savingCents": 270,
                "cheapest": True,
                "optimal": True,
            },
        ]
        assert results["destination"] == "Sweden"
        assert results["pages"] == 5
        assert results["timedOut"] == []

    def test_the_seller_counts_the_solver_found_nothing_for_in_time_are_reported_beside_the_rows(
        self, rig: Rig
    ) -> None:
        rig.decks = [make_deck(1, "Ellivere", want("Arcane Signet"), want("Mind Stone"))]
        rig.scan_result = scan_of(two_sellers())
        rig.curve_timed_out = (3, 4)

        rig.service.start()
        results = cast("Json", rig.service.status()["results"])

        assert [row["sellers"] for row in results["rows"]] == [1, 2]
        assert results["timedOut"] == [3, 4]

    def test_a_package_shows_each_seller_with_shipping_and_each_card_against_its_trend_price(
        self, rig: Rig
    ) -> None:
        rig.decks = [make_deck(1, "Ellivere", want("Arcane Signet"), want("Mind Stone"))]
        rig.scan_result = scan_of(two_sellers())
        rig.service.start()

        package = rig.service.package(2)

        assert package["totalCents"] == 490
        shipments = {s["seller"]: s for s in cast("list[Json]", package["shipments"])}
        assert set(shipments) == {"A", "B"}
        assert shipments["B"] == {
            "seller": "B",
            "country": "France",
            "cards": 1,
            "valueCents": 30,
            "method": "Letter",
            "shippingCents": 200,
            "tracked": False,
            "deliveryDays": None,
            "deliveryBasis": None,
            "picks": [
                {
                    "card": "Arcane Signet",
                    "copies": 1,
                    "expansion": "Any",
                    "condition": "NM",
                    "language": "English",
                    "foil": False,
                    # The fixture card data lists Arcane Signet at EUR 0.30.
                    "priceCents": 30,
                    "trendCents": 30,
                }
            ],
        }
        # Mind Stone has no EUR price in the card data.
        [stone] = cast("list[Json]", shipments["A"]["picks"])
        assert (stone["card"], stone["priceCents"], stone["trendCents"]) == ("Mind Stone", 60, None)
        assert package["missing"] == []
        assert package["short"] == []

    def test_shipping_is_looked_up_for_the_sellers_country_to_the_buyers(self, rig: Rig) -> None:
        rig.decks = [make_deck(1, "Ellivere", want("Arcane Signet"), want("Mind Stone"))]
        rig.scan_result = scan_of(two_sellers())

        rig.service.start()

        assert set(rig.routes.asked) == {(12, 28)}

    def test_a_card_nobody_offers_enough_of_is_short_in_the_package(self, rig: Rig) -> None:
        rig.scan_result = scan_of([offer("Arcane Signet", "A", 50), offer("Mind Stone", "A", 60)])

        rig.service.start()
        package = rig.service.package(1)

        assert package["missing"] == ["Lotus Petal"]
        assert package["short"] == [{"card": "Arcane Signet", "wanted": 3, "available": 1}]

    def test_what_the_wants_list_could_not_match_and_what_cardmarket_lacks_are_reported(
        self, rig: Rig
    ) -> None:
        rig.sync_result = WantsSync(
            7, imported().imported, ("Lotus Petal",), (QuantityMismatch("Arcane Signet", 3, 2),)
        )
        rig.scan_result = scan_of(two_sellers(), not_found=("Lotus Petal",), no_buyable=("Sideboard Card",))

        rig.service.start()
        results = cast("Json", rig.service.status()["results"])

        assert results["unmatched"] == ["Lotus Petal"]
        assert results["quantityMismatch"] == [{"card": "Arcane Signet", "wanted": 3, "got": 2}]
        assert results["notFound"] == ["Lotus Petal"]
        assert results["noOffers"] == ["Sideboard Card"]

    def test_a_scan_that_finds_no_offer_ends_in_results_with_no_rows_and_asks_shipping_nothing(
        self, rig: Rig
    ) -> None:
        rig.scan_result = scan_of([], no_buyable=("Arcane Signet", "Lotus Petal", "Mind Stone"))

        rig.service.start()
        status = rig.service.status()

        results = cast("Json", status["results"])
        assert (status["phase"], results["rows"]) == ("results", [])
        assert results["noOffers"] == ["Arcane Signet", "Lotus Petal", "Mind Stone"]
        assert "shipping" not in rig.events

    def test_without_matched_wants_there_is_no_second_pass(self, rig: Rig) -> None:
        rig.sync_result = WantsSync(7, (), ("Arcane Signet", "Lotus Petal", "Mind Stone"), ())

        rig.service.start()

        assert rig.scan_requests[0].wants_list_id is None

    def test_results_stay_while_the_cards_and_filters_stay_and_go_when_either_changes(self, rig: Rig) -> None:
        scanned(rig)

        assert cast("Json", rig.service.view()["status"])["phase"] == "results"
        assert cast("Json", rig.service.view()["status"])["results"] is not None
        rig.decks[0] = make_deck(2, "Pantlaza, Sun-Favored", want("Mind Stone"))
        dropped = cast("Json", rig.service.view()["status"])

        assert (dropped["phase"], dropped["results"]) == ("idle", None)

    @pytest.mark.parametrize("change", ["deck", "filters", "countries", "delivery", "base"])
    def test_changing_a_choice_forgets_the_results(self, rig: Rig, change: str) -> None:
        scanned(rig)

        if change == "deck":
            rig.service.include(1, included=False)
        elif change == "filters":
            rig.service.set_filters("english", "NM", "foil", [], "any")
        elif change == "countries":
            rig.service.set_filters("english", "NM", "any", [7], "any")
        elif change == "delivery":
            rig.service.set_filters("english", "NM", "any", [], "10")
        else:
            rig.service.set_base_sellers(10)

        status = rig.service.status()
        assert (status["phase"], status["results"]) == ("idle", None)
        with pytest.raises(BuyError, match=re.escape("That package is gone; scan again.")):
            rig.service.package(1)

    def test_the_same_choices_again_keep_the_results(self, rig: Rig) -> None:
        scanned(rig)

        rig.service.set_filters("english", "NM", "any", [], "any")
        rig.service.set_base_sellers(15)

        assert rig.service.status()["results"] is not None


class TestHandingTheWindowToTheOwner:
    @staticmethod
    def seen(rig: Rig) -> list[tuple[object, object, object]]:
        status = rig.service.status()
        return [(status["phase"], status["progress"], status["running"])]

    @pytest.mark.parametrize(
        ("need", "phase", "line"),
        [
            (Need.SIGN_IN, "signin", "Sign in to Cardmarket below"),
            (Need.CHECK, "check", "Finish Cardmarket's check below"),
        ],
    )
    def test_the_screen_says_what_the_owner_must_do_and_goes_back_to_where_it_was(
        self, rig: Rig, need: Need, phase: str, line: str
    ) -> None:
        seen: list[tuple[object, object, object]] = []

        def hand_over(_: threading.Event) -> None:
            assert rig.handover is not None
            seen.extend(self.seen(rig))
            rig.handover.show(need)
            seen.extend(self.seen(rig))
            rig.handover.hide()
            seen.extend(self.seen(rig))

        rig.sync_effect = hand_over

        rig.service.start()

        before = ("wants", "Preparing wants list · 3 cards", True)
        assert seen == [before, (phase, line, True), before]
        assert rig.service.status()["phase"] == "results"
        assert_short(line)

    def test_a_changed_need_follows_the_page_and_the_screen_returns_to_what_it_showed_before_the_first(
        self, rig: Rig
    ) -> None:
        seen: list[tuple[object, object, object]] = []

        def hand_over(_: threading.Event) -> None:
            assert rig.handover is not None
            rig.handover.show(Need.CHECK)
            rig.handover.show(Need.SIGN_IN)
            seen.extend(self.seen(rig))
            rig.handover.hide()
            seen.extend(self.seen(rig))

        rig.sync_effect = hand_over

        rig.service.start()

        assert seen == [
            ("signin", "Sign in to Cardmarket below", True),
            ("wants", "Preparing wants list · 3 cards", True),
        ]

    def test_the_handover_is_cancelled_exactly_when_the_scan_is(self, rig: Rig) -> None:
        asked: list[bool] = []

        def ask(_: threading.Event) -> None:
            assert rig.handover is not None
            asked.append(rig.handover.cancelled())
            rig.service.cancel()
            asked.append(rig.handover.cancelled())

        rig.sync_effect = ask

        rig.service.start()

        assert asked == [False, True]

    def test_cancelling_while_the_window_is_the_owners_ends_the_scan_as_cancelled(self, rig: Rig) -> None:
        def cancel_in_handover(_: threading.Event) -> None:
            assert rig.handover is not None
            rig.handover.show(Need.SIGN_IN)
            rig.service.cancel()
            assert rig.service.status()["cancelling"] is True
            rig.handover.hide()
            raise Cancelled("cancelled while the wants page waited for the owner")

        rig.sync_effect = cancel_in_handover

        rig.service.start()
        status = rig.service.status()

        assert (status["phase"], status["message"], status["results"]) == (
            "cancelled",
            "Scan cancelled.",
            None,
        )
        assert_short(status["message"])
        assert rig.events == ["browser opened", "sync", "browser closed"]

    def test_a_wants_step_interrupted_by_a_sign_in_is_done_again_in_the_same_window(
        self, rig: Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        attempts: list[int] = []

        def interrupted_once(_: threading.Event) -> None:
            attempts.append(1)
            if len(attempts) == 1:
                raise Interrupted("a sign-in came after the submit")

        rig.sync_effect = interrupted_once

        with caplog.at_level(logging.INFO):
            rig.service.start()

        assert rig.events == ["browser opened", "sync", "sync", "scan", "browser closed"]
        assert len(rig.sync_calls) == 2
        assert rig.service.status()["phase"] == "results"
        assert "a sign-in or check interrupted the wants list" in caplog.text

    def test_a_wants_step_interrupted_twice_stops_the_scan_and_closes_the_window(self, rig: Rig) -> None:
        def interrupted(_: threading.Event) -> None:
            raise Interrupted("a check came after the submit")

        rig.sync_effect = interrupted

        rig.service.start()
        status = rig.service.status()

        assert (status["phase"], status["message"], status["detail"]) == (
            "stopped",
            "Cardmarket interrupted the wants list again; scan again.",
            "a check came after the submit",
        )
        assert_short(status["message"])
        assert rig.events == ["browser opened", "sync", "sync", "browser closed"]

    def test_the_page_reports_the_pane_area_and_that_there_is_none(self, rig: Rig) -> None:
        assert rig.service.set_pane(10, 20.5, 300, 200, 1.5) == {}
        assert rig.service.set_pane(0, 0, 0, 0, 1) == {}

        assert rig.pane.areas == [PaneArea(10.0, 20.5, 300.0, 200.0, 1.5), None]

    def test_a_report_that_is_not_numbers_is_refused_and_not_passed_on(self, rig: Rig) -> None:
        with pytest.raises(BuyError, match="not valid"):
            rig.service.set_pane("10", 20, 300, 200, 1)

        assert rig.pane.areas == []


class TestCancellingAndStopping:
    def test_cancel_during_the_wants_sync_ends_with_nothing_kept_and_the_window_closed(
        self, rig: Rig
    ) -> None:
        def cancel_it(_cancel: threading.Event) -> None:
            rig.service.cancel()

        rig.sync_effect = cancel_it

        rig.service.start()
        status = rig.service.status()

        assert (status["phase"], status["message"], status["results"]) == (
            "cancelled",
            "Scan cancelled.",
            None,
        )
        assert rig.events == ["browser opened", "sync", "browser closed"]

    def test_cancel_during_the_scan_keeps_what_was_read_and_solves_it(self, rig: Rig) -> None:
        rig.decks = [make_deck(1, "Ellivere", want("Arcane Signet"), want("Mind Stone"))]
        rig.scan_result = scan_of(two_sellers(), status=ScanStatus.CANCELLED, pages=9)

        rig.service.start()
        status = rig.service.status()

        assert status["phase"] == "cancelled"
        assert status["message"] == "Scan cancelled after 9 pages; results are partial."
        assert_short(status["message"])
        assert [r["sellers"] for r in cast("Json", status["results"])["rows"]] == [1, 2]

    def test_a_cancel_pressed_during_the_scan_still_solves_what_it_read(self, rig: Rig) -> None:
        rig.decks = [make_deck(1, "Ellivere", want("Arcane Signet"), want("Mind Stone"))]
        rig.scan_result = scan_of(two_sellers(), status=ScanStatus.CANCELLED, pages=9)
        rig.scan_effect = lambda cancel: cancel.set()

        rig.service.start()
        status = rig.service.status()

        assert status["phase"] == "cancelled"
        assert [r["sellers"] for r in cast("Json", status["results"])["rows"]] == [1, 2]
        assert [cancelled() for cancelled in rig.curve_cancels] == [False]

    def test_a_cancel_pressed_while_the_packages_are_found_ends_the_run_as_cancelled(self, rig: Rig) -> None:
        rig.decks = [make_deck(1, "Ellivere", want("Arcane Signet"), want("Mind Stone"))]
        rig.scan_result = scan_of(two_sellers())
        rig.curve_effect = rig.service.cancel

        rig.service.start()
        status = rig.service.status()

        assert (status["phase"], status["running"], status["results"]) == ("cancelled", False, None)
        assert status["message"] == "Cancelled while finding packages; scan again."
        assert_short(status["message"])
        assert [cancelled() for cancelled in rig.curve_cancels] == [True]

    def test_a_scan_cancelled_before_it_read_an_offer_has_no_results(self, rig: Rig) -> None:
        rig.scan_result = scan_of([], status=ScanStatus.CANCELLED, pages=1)

        rig.service.start()
        status = rig.service.status()

        assert (status["phase"], status["results"]) == ("cancelled", None)
        assert status["message"] == "Scan cancelled after 1 page; results are partial."

    def test_a_stopped_scan_says_so_keeps_its_reason_and_what_it_read(self, rig: Rig) -> None:
        reason = "Sylvan Safekeeper: Cloudflare's check was still up after 30 s"
        rig.decks = [make_deck(1, "Ellivere", want("Arcane Signet"), want("Mind Stone"))]
        rig.scan_result = scan_of(two_sellers(), status=ScanStatus.STOPPED, reason=reason, pages=14)

        rig.service.start()
        status = rig.service.status()

        assert status["phase"] == "stopped"
        assert status["message"] == "Scan stopped after 14 pages; results are partial."
        assert status["detail"] == reason
        assert cast("Json", status["results"])["rows"] != []

    def test_a_scan_stopped_before_any_offer_has_no_results_and_is_stopped(self, rig: Rig) -> None:
        rig.scan_result = scan_of([], status=ScanStatus.STOPPED, reason="the page changed", pages=0)

        rig.service.start()
        status = rig.service.status()

        assert (status["phase"], status["results"], status["detail"]) == ("stopped", None, "the page changed")
        assert status["message"] == "Scan stopped after 0 pages; results are partial."

    @pytest.mark.parametrize(
        ("error", "message"),
        [
            (BrowserError("gone"), "The Cardmarket window failed; the details are in the log."),
            (LoadTimeout("slow"), "Cardmarket did not answer; check your connection, then scan again."),
            (ValueError("boom"), "Something went wrong; the details are in the log."),
        ],
    )
    def test_a_failure_in_the_window_stops_with_a_short_message_and_closes_it(
        self, rig: Rig, error: Exception, message: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        def fail(_: threading.Event) -> None:
            raise error

        rig.sync_effect = fail

        with caplog.at_level(logging.WARNING):
            rig.service.start()
        status = rig.service.status()

        assert (status["phase"], status["message"]) == ("stopped", message)
        assert_short(message)
        assert "browser closed" in rig.events
        assert str(error) in caplog.text

    @pytest.mark.parametrize("error", [BrowserError("the window is gone"), RuntimeError("boom")])
    def test_a_failure_after_a_cancel_ends_the_scan_as_cancelled_without_an_error(
        self, rig: Rig, error: Exception, caplog: pytest.LogCaptureFixture
    ) -> None:
        def close_mid_run(cancel: threading.Event) -> None:
            cancel.set()
            raise error

        rig.scan_effect = close_mid_run

        with caplog.at_level(logging.INFO):
            rig.service.start()
        status = rig.service.status()

        assert (status["phase"], status["message"], status["results"]) == (
            "cancelled",
            "Scan cancelled.",
            None,
        )
        assert "browser closed" in rig.events
        assert [r.levelno for r in caplog.records if r.levelno >= logging.WARNING] == []
        assert "unexpectedly" not in caplog.text

    def test_a_shipping_table_that_cannot_be_fetched_stops_the_scan(self, rig: Rig) -> None:
        rig.scan_result = scan_of(two_sellers())
        rig.routes = OfflineRoutes()

        rig.service.start()
        status = rig.service.status()

        assert (status["phase"], status["message"]) == (
            "stopped",
            "Shipping prices could not be fetched; scan again.",
        )
        assert status["detail"] == "offline"

    def test_a_destination_the_shipping_table_lacks_stops_the_scan(self, rig: Rig) -> None:
        rig.scan_result = scan_of(two_sellers(), destination="Atlantis")

        rig.service.start()
        status = rig.service.status()

        assert status["phase"] == "stopped"
        assert_short(status["message"])
        assert "Atlantis" in str(status["detail"])

    def test_scanning_again_after_a_stop_starts_a_clean_run(self, rig: Rig) -> None:
        rig.scan_result = scan_of([], status=ScanStatus.STOPPED, reason="x", pages=1)
        rig.service.start()
        rig.scan_result = scan_of(two_sellers())

        rig.service.start()

        status = rig.service.status()
        assert (status["phase"], status["message"]) == ("results", "")
        assert len(rig.sync_calls) == 2

    def test_no_message_runs_past_twelve_words(self, rig: Rig) -> None:
        for effect in (BrowserError("x"), LoadTimeout("x"), RuntimeError("x")):

            def fail(_: threading.Event, effect: Exception = effect) -> None:
                raise effect

            rig.sync_effect = fail
            rig.service.start()
            assert_short(rig.service.status()["message"])


class TestWhileItRuns:
    def test_the_choices_cannot_change_during_a_scan(self, rig: Rig) -> None:
        refused: list[str] = []

        def change(_: threading.Event) -> None:
            for attempt in (
                lambda: rig.service.include(1, included=False),
                lambda: rig.service.include_card(1, "Lotus Petal", included=False),
                lambda: rig.service.set_filters("any", "any", "any", [], "any"),
                lambda: rig.service.set_base_sellers(3),
                rig.service.start,
            ):
                with pytest.raises(BuyError) as error:
                    attempt()
                refused.append(str(error.value))
            assert rig.service.status()["running"] is True

        rig.sync_effect = change

        rig.service.start()

        assert refused == [
            "Wait for the scan, or cancel it.",
            "Wait for the scan, or cancel it.",
            "Wait for the scan, or cancel it.",
            "Wait for the scan, or cancel it.",
            "A scan is running already.",
        ]

    def test_cancel_marks_the_run_as_cancelling_until_it_ends(self, rig: Rig) -> None:
        seen: list[object] = []

        def cancel(_: threading.Event) -> None:
            seen.append(rig.service.cancel()["cancelling"])

        rig.sync_effect = cancel

        rig.service.start()

        assert seen == [True]
        assert rig.service.status()["cancelling"] is False

    def test_cancel_with_no_scan_running_changes_nothing(self, rig: Rig) -> None:
        assert rig.service.cancel()["phase"] == "idle"

    def test_a_scan_is_logged_by_stage(self, rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
        rig.scan_result = scan_of(two_sellers())

        with caplog.at_level(logging.DEBUG):
            rig.service.start()

        info = [
            r.getMessage() for r in caplog.records if r.levelno == logging.INFO and r.name == "commander.buy"
        ]
        assert any(m.startswith("buy: scan started for 3 cards (5 copies)") for m in info)
        assert any(m.startswith("buy: wants list 7 holds 1 wants") for m in info)
        assert any(m.startswith("buy: scan complete after 5 pages") for m in info)
        assert any(m.startswith("buy: results") for m in info)

    def test_the_view_needs_an_account(self, rig: Rig) -> None:
        service = BuyService(
            store=BuyStore(rig.settings_path),
            decks=lambda: rig.decks,
            user=lambda: None,
            engine=rig.engine,
            card_db=rig.card_db,
            background=lambda work: work(),
        )

        with pytest.raises(BuyError, match=re.escape("Sign in to Archidekt first.")):
            service.view()

    def test_without_card_data_the_package_has_no_trend_prices_and_still_shows(self, rig: Rig) -> None:
        rig.card_db = rig.card_db.with_name("missing.sqlite")
        rig.service = rig.build()
        rig.decks = [make_deck(1, "Ellivere", want("Arcane Signet"))]
        rig.scan_result = scan_of(two_sellers())
        rig.service.start()

        package = rig.service.package(1)

        [shipment] = cast("list[Json]", package["shipments"])
        assert cast("list[Json]", shipment["picks"])[0]["trendCents"] is None


def timed(base: Offer, days: int | None, route: int | None = None, *, country: str | None = None) -> Offer:
    return replace(base, delivery_days=days, route_days=route, seller_country=country or base.seller_country)


class NoCountries(Routes):
    def countries(self) -> tuple[Country, ...]:
        raise NetworkError("offline")


class TestSellerLocationAndDelivery:
    """One Arcane Signet is wanted from sellers whose offers differ in price, country and days; a package
    costs the card plus a 2 € letter, so the cheapest card wins among the sellers that are allowed."""

    @pytest.fixture
    def one_card(self, rig: Rig) -> Rig:
        rig.decks = [make_deck(1, "Ellivere", want("Arcane Signet"))]
        return rig

    @staticmethod
    def sellers_of(rig: Rig) -> list[str]:
        shipments = cast("list[Json]", rig.service.package(1)["shipments"])
        return [str(shipment["seller"]) for shipment in shipments]

    @staticmethod
    def shipment(rig: Rig) -> Json:
        [shipment] = cast("list[Json]", rig.service.package(1)["shipments"])
        return shipment

    def test_the_countries_come_by_name_with_their_ids(self, rig: Rig) -> None:
        assert rig.service.countries() == {
            "countries": [
                {"id": 1, "name": "Austria"},
                {"id": 12, "name": "France"},
                {"id": 7, "name": "Germany"},
                {"id": 17, "name": "Italy"},
                {"id": 23, "name": "Netherlands"},
                {"id": 28, "name": "Sweden"},
            ]
        }

    def test_countries_that_cannot_be_fetched_are_told_in_a_short_message(self, rig: Rig) -> None:
        rig.routes = NoCountries()

        with pytest.raises(BuyError, match="could not be loaded") as error:
            rig.service.countries()

        assert_short(str(error.value))

    def test_the_scan_is_given_the_countries_by_id_for_the_address_and_by_name_for_the_offers(
        self, rig: Rig
    ) -> None:
        rig.service.set_filters("english", "NM", "any", [23, 7], "7")

        rig.service.start()

        [request] = rig.scan_requests
        assert request.filters.seller_countries == frozenset({7, 23})
        assert request.filters.max_days == 7
        assert request.limits == OfferLimits(frozenset({"germany", "netherlands"}), 7)
        assert rig.events[:2] == ["shipping", "browser opened"]

    def test_with_no_country_and_no_limit_the_scan_asks_for_nothing_extra(self, rig: Rig) -> None:
        rig.service.start()

        [request] = rig.scan_requests
        assert request.limits == OfferLimits()
        assert (request.filters.seller_countries, request.filters.max_days) == (frozenset(), None)
        assert rig.events[0] == "browser opened"

    def test_a_country_cardmarket_does_not_list_stops_the_scan_before_any_window(self, rig: Rig) -> None:
        rig.service.set_filters("english", "NM", "any", [99], "any")

        rig.service.start()
        status = rig.service.status()

        assert status["phase"] == "stopped"
        assert_short(status["message"])
        assert "[99]" in str(status["detail"])
        assert "browser opened" not in rig.events

    def test_a_country_list_that_cannot_be_fetched_stops_the_scan_before_any_window(self, rig: Rig) -> None:
        rig.routes = NoCountries()
        rig.service.set_filters("english", "NM", "any", [7], "any")

        rig.service.start()
        status = rig.service.status()

        assert (status["phase"], status["message"], status["detail"]) == (
            "stopped",
            "Cardmarket's countries could not be loaded; scan again.",
            "offline",
        )
        assert "browser opened" not in rig.events

    def test_the_filters_chosen_are_logged_when_the_scan_starts(
        self, rig: Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        rig.service.set_filters("english", "NM", "any", [7, 23], "10")

        with caplog.at_level(logging.INFO):
            rig.service.start()

        started = [r.getMessage() for r in caplog.records if r.getMessage().startswith("buy: scan started")]
        assert len(started) == 1
        assert "'countries': [7, 23]" in started[0]
        assert "'delivery': '10'" in started[0]

    def test_the_solver_gets_only_offers_from_the_countries_chosen(self, one_card: Rig) -> None:
        one_card.service.set_filters("english", "NM", "any", [7], "any")
        cheap_but_austrian = offer("Arcane Signet", "B", 30, country="Austria")
        one_card.scan_result = scan_of(
            [offer("Arcane Signet", "A", 100, country="Germany"), cheap_but_austrian]
        )

        one_card.service.start()

        assert self.sellers_of(one_card) == ["A"]
        assert set(one_card.routes.asked) == {(7, 28)}

    def test_without_a_country_choice_the_cheapest_seller_wins_wherever_it_is(self, one_card: Rig) -> None:
        one_card.scan_result = scan_of(
            [
                offer("Arcane Signet", "A", 100, country="Germany"),
                offer("Arcane Signet", "B", 30, country="Austria"),
            ]
        )

        one_card.service.start()

        assert self.sellers_of(one_card) == ["B"]

    def test_the_solver_gets_only_sellers_arriving_within_the_limit(self, one_card: Rig) -> None:
        one_card.service.set_filters("english", "NM", "any", [], "7")
        slow = timed(offer("Arcane Signet", "B", 30, country="Germany"), 18)
        quick = timed(offer("Arcane Signet", "A", 100, country="Germany"), 6)
        one_card.scan_result = scan_of([slow, quick])

        one_card.service.start()

        shipment = self.shipment(one_card)
        assert (shipment["seller"], shipment["deliveryDays"], shipment["deliveryBasis"]) == ("A", 6, "own")
        package = one_card.service.package(1)
        assert (package["slowestDays"], package["withoutEstimate"], package["leftOutNoEstimate"]) == (6, 0, 0)

    def test_a_seller_without_an_estimate_is_judged_by_the_median_of_its_country(self, one_card: Rig) -> None:
        one_card.service.set_filters("english", "NM", "any", [], "14")
        one_card.scan_result = scan_of(
            [
                timed(offer("Arcane Signet", "A", 100, country="Germany"), 6),
                timed(offer("Arcane Signet", "B", 20, country="Germany"), 18),
                timed(offer("Arcane Signet", "C", 30, country="Germany"), None),
            ]
        )

        one_card.service.start()

        # B is the cheapest but 18 days; C has no estimate and is judged at 12, the mean of 6 and 18.
        shipment = self.shipment(one_card)
        assert (shipment["seller"], shipment["deliveryDays"], shipment["deliveryBasis"]) == (
            "C",
            12,
            "country",
        )
        package = one_card.service.package(1)
        assert (package["slowestDays"], package["leftOutNoEstimate"]) == (12, 0)

    def test_a_seller_without_an_estimate_or_peers_is_judged_by_the_route_average(
        self, one_card: Rig
    ) -> None:
        one_card.service.set_filters("english", "NM", "any", [], "14")
        one_card.scan_result = scan_of(
            [
                timed(offer("Arcane Signet", "A", 100, country="Germany"), 6, 7),
                timed(offer("Arcane Signet", "C", 30, country="Italy"), None, 14),
            ]
        )

        one_card.service.start()

        shipment = self.shipment(one_card)
        assert (shipment["seller"], shipment["deliveryDays"], shipment["deliveryBasis"]) == ("C", 14, "route")

    def test_a_route_average_over_the_limit_leaves_that_seller_out(self, one_card: Rig) -> None:
        one_card.service.set_filters("english", "NM", "any", [], "10")
        one_card.scan_result = scan_of(
            [
                timed(offer("Arcane Signet", "A", 100, country="Germany"), 6, 7),
                timed(offer("Arcane Signet", "C", 30, country="Italy"), None, 14),
            ]
        )

        one_card.service.start()

        assert self.sellers_of(one_card) == ["A"]

    def test_a_seller_with_no_estimate_at_all_is_left_out_and_the_package_counts_the_offers(
        self, one_card: Rig
    ) -> None:
        one_card.service.set_filters("english", "NM", "any", [], "14")
        one_card.scan_result = scan_of(
            [
                timed(offer("Arcane Signet", "A", 100, country="Germany"), 6, 7),
                timed(offer("Arcane Signet", "C", 30, country="Italy"), None, None),
                timed(offer("Arcane Signet", "C", 40, country="Italy"), None, None),
            ]
        )

        one_card.service.start()

        assert self.sellers_of(one_card) == ["A"]
        assert one_card.service.package(1)["leftOutNoEstimate"] == 2

    def test_without_a_limit_a_seller_with_no_estimate_stays_and_the_package_says_so(
        self, one_card: Rig
    ) -> None:
        one_card.scan_result = scan_of(
            [
                timed(offer("Arcane Signet", "A", 100, country="Germany"), 6),
                timed(offer("Arcane Signet", "C", 30, country="Italy"), None, None),
            ]
        )

        one_card.service.start()

        shipment = self.shipment(one_card)
        assert (shipment["seller"], shipment["deliveryDays"], shipment["deliveryBasis"]) == ("C", None, None)
        package = one_card.service.package(1)
        assert (package["slowestDays"], package["withoutEstimate"], package["leftOutNoEstimate"]) == (
            None,
            1,
            0,
        )

    def test_the_package_arrives_with_its_slowest_seller(self, rig: Rig) -> None:
        rig.decks = [make_deck(1, "Ellivere", want("Arcane Signet"), want("Mind Stone"))]
        days = {"A": 9, "B": 4}
        rig.scan_result = scan_of([timed(o, days[o.seller]) for o in two_sellers()])

        rig.service.start()

        package = rig.service.package(2)
        by_seller = {s["seller"]: s["deliveryDays"] for s in cast("list[Json]", package["shipments"])}
        assert by_seller == {"A": 9, "B": 4}
        assert package["slowestDays"] == 9

    def test_a_card_whose_offers_are_all_left_out_is_listed_with_those_without_offers(self, rig: Rig) -> None:
        rig.decks = [make_deck(1, "Ellivere", want("Arcane Signet"), want("Mind Stone"))]
        rig.service.set_filters("english", "NM", "any", [], "7")
        rig.scan_result = scan_of(
            [
                timed(offer("Arcane Signet", "A", 30), 6),
                timed(offer("Mind Stone", "B", 60), 18),
            ],
            no_buyable=("Sol Ring",),
        )

        rig.service.start()
        results = cast("Json", rig.service.status()["results"])

        assert results["noOffers"] == ["Sol Ring", "Mind Stone"]
        assert rig.service.package(1)["missing"] == ["Mind Stone"]

    def test_the_offers_left_out_are_logged_by_reason(
        self, one_card: Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        one_card.service.set_filters("english", "NM", "any", [7], "10")
        one_card.scan_result = scan_of(
            [
                timed(offer("Arcane Signet", "A", 100, country="Germany"), 6),
                timed(offer("Arcane Signet", "B", 30, country="Germany"), 18),
                timed(offer("Arcane Signet", "D", 30, country="Austria"), 6),
                timed(offer("Arcane Signet", "E", 30, country="Germany"), None),
            ]
        )

        with caplog.at_level(logging.INFO):
            one_card.service.start()

        assert (
            "buy: limits left out 1 offers from other countries, 2 that arrive too late, 0 with no "
            "delivery estimate and 0 above the max price"
        ) in caplog.text


def deck_of(view: Json, deck_id: int) -> Json:
    return next(d for d in cast("list[Json]", view["decks"]) if d["id"] == deck_id)


def ticks_of(view: Json, deck_id: int) -> dict[str, bool]:
    return {c["name"]: c["included"] for c in cast("list[Json]", deck_of(view, deck_id)["cards"])}


class TestTheCardsOfADeck:
    """Pantlaza (2) wants 1 Mind Stone and 2 Arcane Signet; Ellivere (1) wants 1 Arcane Signet and 1 Lotus
    Petal."""

    def test_unticking_a_card_leaves_the_deck_ticked_in_part_and_the_card_out_of_the_purchase(
        self, rig: Rig
    ) -> None:
        view = rig.service.include_card(2, "Mind Stone", included=False)

        pantlaza = deck_of(view, 2)
        assert ticks_of(view, 2) == {"Arcane Signet": True, "Mind Stone": False}
        assert (pantlaza["tick"], pantlaza["included"]) == ("some", True)
        assert (pantlaza["copies"], pantlaza["tickedCopies"]) == (3, 2)
        assert deck_of(view, 1)["tick"] == "all"
        assert view["copies"] == 4

    def test_the_cards_left_out_are_not_scanned_and_the_ticked_ones_are_summed_across_decks(
        self, rig: Rig
    ) -> None:
        rig.service.include_card(2, "Arcane Signet", included=False)

        rig.service.start()

        [call] = rig.sync_calls
        assert call.wanted == [
            WantedCard("Arcane Signet", 1),
            WantedCard("Lotus Petal", 1),
            WantedCard("Mind Stone", 1),
        ]

    def test_a_card_in_two_decks_is_ticked_in_each_on_its_own(self, rig: Rig) -> None:
        view = rig.service.include_card(1, "Arcane Signet", included=False)

        assert ticks_of(view, 1)["Arcane Signet"] is False
        assert ticks_of(view, 2)["Arcane Signet"] is True
        rig.service.start()
        assert WantedCard("Arcane Signet", 2) in rig.sync_calls[0].wanted

    def test_unticking_every_card_one_by_one_leaves_the_deck_unticked(self, rig: Rig) -> None:
        rig.service.include_card(1, "Arcane Signet", included=False)

        view = rig.service.include_card(1, "Lotus Petal", included=False)

        assert (deck_of(view, 1)["tick"], deck_of(view, 1)["included"]) == ("none", False)
        saved = json.loads(rig.settings_path.read_text(encoding="utf-8"))
        assert saved["leftOut"] == {"makkis": [1]}
        assert saved["leftOutCards"] == {}

    def test_ticking_the_deck_when_only_some_cards_are_ticked_ticks_them_all(self, rig: Rig) -> None:
        rig.service.include_card(2, "Mind Stone", included=False)

        view = rig.service.include(2, included=True)

        assert deck_of(view, 2)["tick"] == "all"
        assert ticks_of(view, 2) == {"Arcane Signet": True, "Mind Stone": True}
        assert json.loads(rig.settings_path.read_text(encoding="utf-8"))["leftOutCards"] == {}

    def test_unticking_the_deck_when_only_some_cards_are_ticked_unticks_them_all(self, rig: Rig) -> None:
        rig.service.include_card(2, "Mind Stone", included=False)

        view = rig.service.include(2, included=False)

        assert deck_of(view, 2)["tick"] == "none"
        assert ticks_of(view, 2) == {"Arcane Signet": False, "Mind Stone": False}
        assert json.loads(rig.settings_path.read_text(encoding="utf-8"))["leftOutCards"] == {}

    def test_ticking_one_card_of_an_unticked_deck_ticks_that_card_alone(self, rig: Rig) -> None:
        rig.service.include(2, included=False)

        view = rig.service.include_card(2, "Mind Stone", included=True)

        assert ticks_of(view, 2) == {"Arcane Signet": False, "Mind Stone": True}
        assert deck_of(view, 2)["tick"] == "some"
        assert view["copies"] == 3

    def test_ticking_the_only_card_of_an_unticked_deck_ticks_the_deck(self, rig: Rig) -> None:
        rig.decks = [make_deck(1, "Ellivere", want("Lotus Petal"))]
        rig.service.include(1, included=False)

        view = rig.service.include_card(1, "Lotus Petal", included=True)

        assert deck_of(view, 1)["tick"] == "all"

    def test_unticking_a_card_of_an_unticked_deck_changes_nothing(self, rig: Rig) -> None:
        rig.service.include(2, included=False)

        view = rig.service.include_card(2, "Mind Stone", included=False)

        assert deck_of(view, 2)["tick"] == "none"
        assert json.loads(rig.settings_path.read_text(encoding="utf-8"))["leftOutCards"] == {}

    def test_the_cards_left_out_are_remembered_and_belong_to_the_account(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        rig.service.include_card(2, "Mind Stone", included=False)
        again = Rig(tmp_path, rig.card_db)
        other = BuyService(
            store=BuyStore(rig.settings_path),
            decks=lambda: rig.decks,
            user=lambda: "SOMEONE",
            engine=rig.engine,
            card_db=rig.card_db,
            background=lambda work: work(),
        )

        assert ticks_of(again.service.view(), 2) == {"Arcane Signet": True, "Mind Stone": False}
        assert ticks_of(other.view(), 2) == {"Arcane Signet": True, "Mind Stone": True}

    def test_a_card_that_is_no_longer_dont_have_is_forgotten_and_comes_back_ticked(self, rig: Rig) -> None:
        rig.service.include_card(2, "Mind Stone", included=False)
        rig.service.include_card(1, "Lotus Petal", included=False)
        rig.decks[0] = make_deck(2, "Pantlaza, Sun-Favored", want("Arcane Signet", 2))

        view = rig.service.view()

        assert (deck_of(view, 2)["tick"], ticks_of(view, 2)) == ("all", {"Arcane Signet": True})
        saved = json.loads(rig.settings_path.read_text(encoding="utf-8"))
        assert saved["leftOutCards"] == {"makkis": {"1": ["Lotus Petal"]}}
        rig.decks[0] = make_deck(2, "Pantlaza, Sun-Favored", want("Arcane Signet", 2), want("Mind Stone"))
        assert ticks_of(rig.service.view(), 2) == {"Arcane Signet": True, "Mind Stone": True}

    def test_when_the_remaining_cards_are_all_unticked_the_deck_counts_as_unticked(self, rig: Rig) -> None:
        rig.service.include_card(2, "Mind Stone", included=False)
        rig.decks[0] = make_deck(2, "Pantlaza, Sun-Favored", want("Mind Stone"))

        view = rig.service.view()

        assert deck_of(view, 2)["tick"] == "none"
        saved = json.loads(rig.settings_path.read_text(encoding="utf-8"))
        assert saved["leftOut"] == {"makkis": [2]}
        assert saved["leftOutCards"] == {}

    def test_a_deck_with_nothing_left_to_buy_forgets_its_cards(self, rig: Rig) -> None:
        rig.service.include_card(1, "Lotus Petal", included=False)
        rig.decks[1] = make_deck(1, "Ellivere of the Wild Court", card("Lotus Petal", 1, ""))

        rig.service.view()

        saved = json.loads(rig.settings_path.read_text(encoding="utf-8"))
        assert saved["leftOutCards"] == {}

    def test_cards_of_a_deck_whose_labels_are_unknown_are_not_forgotten(self, rig: Rig) -> None:
        rig.service.include_card(2, "Mind Stone", included=False)
        rig.decks[0] = make_deck(2, "Pantlaza, Sun-Favored", card("Mind Stone", 1, None))

        rig.service.view()

        saved = json.loads(rig.settings_path.read_text(encoding="utf-8"))
        assert saved["leftOutCards"] == {"makkis": {"2": ["Mind Stone"]}}

    def test_a_card_the_deck_does_not_want_is_refused(self, rig: Rig) -> None:
        with pytest.raises(BuyError, match="nothing to buy"):
            rig.service.include_card(2, "Lotus Petal", included=False)
        with pytest.raises(BuyError, match="nothing to buy"):
            rig.service.include_card(9, "Mind Stone", included=False)
        with pytest.raises(BuyError, match="nothing to buy"):
            rig.service.include_card(2, 7, included=False)

    def test_with_every_card_unticked_there_is_nothing_to_scan(self, rig: Rig) -> None:
        for deck_id, name in [
            (1, "Arcane Signet"),
            (1, "Lotus Petal"),
            (2, "Arcane Signet"),
            (2, "Mind Stone"),
        ]:
            rig.service.include_card(deck_id, name, included=False)

        view = rig.service.view()

        assert view["copies"] == 0
        with pytest.raises(BuyError, match=re.escape("No cards to buy; tick a deck first.")):
            rig.service.start()

    def test_ticking_a_card_forgets_the_results(self, rig: Rig) -> None:
        scanned(rig)

        rig.service.include_card(1, "Lotus Petal", included=False)

        status = rig.service.status()
        assert (status["phase"], status["results"]) == ("idle", None)

    def test_a_settings_file_with_damaged_cards_left_out_keeps_the_good_ones(
        self, rig: Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        rig.settings_path.parent.mkdir(parents=True)
        document = {
            "version": 3,
            "leftOutCards": {"makkis": {"2": ["Mind Stone", 5, ""], "x": ["Sol Ring"]}},
        }
        rig.settings_path.write_text(json.dumps(document), encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            view = rig.build().view()

        assert ticks_of(view, 2) == {"Arcane Signet": True, "Mind Stone": False}
        assert "no card name" in caplog.text
        assert "no deck id" in caplog.text


class TestTheMaxPrice:
    """One Arcane Signet is wanted; a package costs the card plus a 2 € letter."""

    @pytest.fixture
    def one_card(self, rig: Rig) -> Rig:
        rig.decks = [make_deck(1, "Ellivere", want("Arcane Signet"))]
        return rig

    @staticmethod
    def results(rig: Rig) -> Json:
        return cast("Json", rig.service.status()["results"])

    @staticmethod
    def sellers_of(rig: Rig) -> list[str]:
        return [str(s["seller"]) for s in cast("list[Json]", rig.service.package(1)["shipments"])]

    def test_the_cap_reaches_the_solver_and_an_offer_at_exactly_the_cap_stays(self, one_card: Rig) -> None:
        one_card.service.set_filters("english", "NM", "any", [], "any", max_price="3")
        one_card.scan_result = scan_of(
            [
                offer("Arcane Signet", "A", 300),
                offer("Arcane Signet", "B", 301),
                offer("Arcane Signet", "C", 20),
            ]
        )

        one_card.service.start()

        assert self.sellers_of(one_card) == ["C"]
        [request] = one_card.scan_requests
        assert request.limits == OfferLimits(max_price_cents=300)
        # Cardmarket's own form has no price limit, so none goes in the address.
        assert "rice" not in request.filters.query()

    def test_a_card_whose_cheapest_offer_is_over_the_cap_is_listed_with_that_price_apart_from_the_rest(
        self, rig: Rig
    ) -> None:
        rig.decks = [make_deck(1, "Ellivere", want("Arcane Signet"), want("Mind Stone"), want("Sol Ring"))]
        rig.service.set_filters("english", "NM", "any", [], "any", max_price="5")
        rig.scan_result = scan_of(
            [
                offer("Arcane Signet", "A", 900),
                offer("Arcane Signet", "B", 650),
                offer("Mind Stone", "A", 100),
            ],
            no_buyable=("Sol Ring",),
        )

        rig.service.start()
        results = self.results(rig)

        assert results["overPrice"] == [{"card": "Arcane Signet", "cheapestCents": 650}]
        assert results["noOffers"] == ["Sol Ring"]
        assert rig.service.package(1)["missing"] == ["Arcane Signet", "Sol Ring"]

    def test_a_card_with_an_offer_within_the_cap_is_not_listed_as_over_it(self, one_card: Rig) -> None:
        one_card.service.set_filters("english", "NM", "any", [], "any", max_price="5")
        one_card.scan_result = scan_of([offer("Arcane Signet", "A", 900), offer("Arcane Signet", "B", 500)])

        one_card.service.start()

        assert self.results(one_card)["overPrice"] == []
        assert self.sellers_of(one_card) == ["B"]

    def test_a_card_left_only_by_its_country_is_without_offers_and_one_left_by_the_cap_is_over_price(
        self, rig: Rig
    ) -> None:
        rig.decks = [make_deck(1, "Ellivere", want("Arcane Signet"), want("Mind Stone"))]
        rig.service.set_filters("english", "NM", "any", [7], "any", max_price="5")
        rig.scan_result = scan_of(
            [
                offer("Arcane Signet", "A", 800, country="Germany"),
                offer("Arcane Signet", "B", 100, country="Austria"),
                offer("Mind Stone", "C", 100, country="Austria"),
            ]
        )

        rig.service.start()
        results = self.results(rig)

        assert results["overPrice"] == [{"card": "Arcane Signet", "cheapestCents": 800}]
        assert results["noOffers"] == ["Mind Stone"]

    def test_with_every_offer_over_the_cap_nothing_is_solved_and_shipping_is_not_asked(
        self, one_card: Rig
    ) -> None:
        one_card.service.set_filters("english", "NM", "any", [], "any", max_price="2.99")
        one_card.scan_result = scan_of([offer("Arcane Signet", "A", 300)])

        one_card.service.start()
        results = self.results(one_card)

        assert results["rows"] == []
        assert results["overPrice"] == [{"card": "Arcane Signet", "cheapestCents": 300}]
        assert "shipping" not in one_card.events

    def test_a_new_cap_judges_and_solves_the_scan_already_read_without_a_new_one(self, one_card: Rig) -> None:
        one_card.scan_result = scan_of([offer("Arcane Signet", "A", 300), offer("Arcane Signet", "B", 500)])
        one_card.service.start()
        assert [r["totalCents"] for r in self.results(one_card)["rows"]] == [500]

        capped = one_card.service.set_filters("english", "NM", "any", [], "any", max_price="2")

        status = cast("Json", capped["status"])
        assert status["phase"] == "results"
        assert cast("Json", status["results"])["rows"] == []
        assert cast("Json", status["results"])["overPrice"] == [
            {"card": "Arcane Signet", "cheapestCents": 300}
        ]
        loosened = one_card.service.set_filters("english", "NM", "any", [], "any", max_price="4")
        rows = cast("list[Json]", cast("Json", cast("Json", loosened["status"])["results"])["rows"])
        assert [r["totalCents"] for r in rows] == [500]
        assert self.sellers_of(one_card) == ["A"]
        # Cardmarket was asked once, and the wants list was filled once.
        assert len(one_card.scan_requests) == 1
        assert len(one_card.sync_calls) == 1
        assert one_card.events.count("browser opened") == 1

    def test_removing_the_cap_brings_back_the_cards_it_left_out(self, one_card: Rig) -> None:
        one_card.service.set_filters("english", "NM", "any", [], "any", max_price="1")
        one_card.scan_result = scan_of([offer("Arcane Signet", "A", 300)])
        one_card.service.start()
        assert self.results(one_card)["rows"] == []

        view = one_card.service.set_filters("english", "NM", "any", [], "any", max_price="")

        results = cast("Json", cast("Json", view["status"])["results"])
        assert [r["totalCents"] for r in cast("list[Json]", results["rows"])] == [500]
        assert results["overPrice"] == []
        assert len(one_card.scan_requests) == 1

    def test_the_solved_again_results_keep_the_state_of_the_scan_they_came_from(self, one_card: Rig) -> None:
        one_card.scan_result = scan_of(
            [offer("Arcane Signet", "A", 300)], status=ScanStatus.CANCELLED, pages=9
        )
        one_card.service.start()

        view = one_card.service.set_filters("english", "NM", "any", [], "any", max_price="4")

        status = cast("Json", view["status"])
        assert status["phase"] == "cancelled"
        assert status["message"] == "Scan cancelled after 9 pages; results are partial."
        assert cast("Json", status["results"])["pages"] == 9

    def test_while_it_is_solved_again_the_screen_shows_the_work_and_no_old_results(
        self, one_card: Rig
    ) -> None:
        waiting: list[Callable[[], None]] = []
        service = BuyService(
            store=BuyStore(one_card.settings_path),
            decks=lambda: one_card.decks,
            user=lambda: "MAKKIS",
            engine=one_card.engine,
            card_db=one_card.card_db,
            background=waiting.append,
        )
        one_card.scan_result = scan_of([offer("Arcane Signet", "A", 300)])
        service.start()
        waiting.pop()()

        view = service.set_filters("english", "NM", "any", [], "any", max_price="4")

        status = cast("Json", view["status"])
        assert (status["phase"], status["running"], status["results"]) == ("solving", True, None)
        assert status["progress"] == "Finding the cheapest packages"
        with pytest.raises(BuyError, match=re.escape("Wait for the scan, or cancel it.")):
            service.include(1, included=False)
        [solve] = waiting
        solve()
        done = cast("Json", service.status())
        assert done["phase"] == "results"
        assert len(cast("Json", done["results"])["rows"]) == 1

    def test_another_filter_still_needs_a_new_scan(self, one_card: Rig) -> None:
        one_card.scan_result = scan_of([offer("Arcane Signet", "A", 300)])
        one_card.service.start()

        one_card.service.set_filters("english", "NM", "foil", [], "any", max_price="4")

        status = one_card.service.status()
        assert (status["phase"], status["results"]) == ("idle", None)

    def test_a_change_of_the_cap_before_any_scan_only_saves_it(self, one_card: Rig) -> None:
        view = one_card.service.set_filters("english", "NM", "any", [], "any", max_price="4")

        assert cast("Json", view["status"])["phase"] == "idle"
        assert one_card.events == []

    def test_a_shipping_table_that_fails_while_solving_again_stops_with_its_message(
        self, one_card: Rig
    ) -> None:
        one_card.scan_result = scan_of([offer("Arcane Signet", "A", 300)])
        one_card.service.start()
        one_card.routes = OfflineRoutes()

        view = one_card.service.set_filters("english", "NM", "any", [], "any", max_price="4")

        status = cast("Json", view["status"])
        assert (status["phase"], status["message"]) == (
            "stopped",
            "Shipping prices could not be fetched; scan again.",
        )

    def test_a_cap_is_logged_with_the_filters_and_the_offers_it_left_out(
        self, one_card: Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        one_card.service.set_filters("english", "NM", "any", [], "any", max_price="1")
        one_card.scan_result = scan_of([offer("Arcane Signet", "A", 300), offer("Arcane Signet", "B", 400)])

        with caplog.at_level(logging.INFO):
            one_card.service.start()

        assert "'maxPriceCents': 100" in caplog.text
        assert "2 above the max price" in caplog.text


def mixed_cart(package: Package) -> CartRun:
    """The first offer refused and the second short of its copies, with one seller's shipping dearer."""
    gone, short = package.picks
    return CartRun(
        CartStatus.COMPLETE,
        "",
        (
            ItemResult(gone, CartOutcome.GONE, 0, "The item is not available."),
            ItemResult(short, CartOutcome.SHORT, 0),
        ),
        (ShippingResult("B", "Letter", 200, "Brief S Priority", 255),),
    )


def any_language(service: BuyService) -> object:
    return service.set_filters("any", "NM", "any", [], "any")


def leave_out_the_stone(service: BuyService) -> object:
    return service.include_card(1, "Mind Stone", included=False)


def cap_the_price(service: BuyService) -> object:
    return service.set_filters("english", "NM", "any", [], "any", max_price="4")


# A confirmation no package matches, for a call that is refused before the package is compared.
NO_CHECK: Json = {"totalCents": 0, "picks": []}


def add(service: BuyService, sellers: int) -> Json:
    """What the page does: put in the cart the package it was shown, sending the check it came with."""
    return service.add_to_cart(sellers, service.package(sellers)["check"])


class TestAddingToTheCart:
    """Two cards are wanted, so there are packages of one and of two sellers; the cart is a fake."""

    @pytest.fixture
    def two_packages(self, rig: Rig) -> Rig:
        rig.decks = [make_deck(1, "Ellivere", want("Arcane Signet"), want("Mind Stone"))]
        rig.scan_result = scan_of(two_sellers())
        rig.service.start()
        rig.events.clear()
        rig.progress.clear()
        return rig

    @staticmethod
    def cart_of(rig: Rig) -> Json:
        return cast("Json", rig.service.status()["cart"])

    def test_the_chosen_package_is_added_in_the_scan_window_and_the_report_joins_the_results(
        self, two_packages: Rig
    ) -> None:
        rig = two_packages
        results = rig.service.status()["results"]
        seen: list[Json] = []
        rig.cart_effect = lambda _cancel: seen.append(rig.service.status())

        status = add(rig.service, 2)

        assert rig.events == ["browser opened", "cart", "browser closed"]
        [package] = rig.cart_calls
        assert sorted((p.offer.seller, p.offer.card) for p in package.picks) == [
            ("A", "Mind Stone"),
            ("B", "Arcane Signet"),
        ]
        [during] = seen
        assert (during["phase"], during["running"], during["cart"]) == ("cart", True, None)
        assert during["progress"] == "Adding Arcane Signet · 1 of 2"
        assert (status["phase"], status["running"], status["progress"]) == ("carted", False, "")
        assert status["results"] == results
        assert status["cart"] == {
            "status": "complete",
            "reason": "",
            "added": 2,
            "already": 0,
            "gone": 0,
            "refused": 0,
            "short": 0,
            "missing": 0,
            "noAnswer": 0,
            "notSent": 0,
            "items": [],
            "shipping": [],
        }

    def test_the_package_can_be_chosen_again_after_a_run(self, two_packages: Rig) -> None:
        add(two_packages.service, 1)

        add(two_packages.service, 2)

        assert [p.seller_count for p in two_packages.cart_calls] == [1, 2]
        assert two_packages.service.package(1)["sellers"] == 1

    def test_what_did_not_simply_go_in_is_listed_with_cardmarkets_reason_and_the_shipping_beside_the_estimate(
        self, two_packages: Rig
    ) -> None:
        two_packages.cart_result = mixed_cart

        add(two_packages.service, 2)
        cart = self.cart_of(two_packages)

        gone, short = two_packages.cart_calls[0].picks
        assert (cart["added"], cart["gone"], cart["short"]) == (0, 1, 1)
        assert cart["items"] == [
            {
                "seller": gone.offer.seller,
                "card": gone.offer.card,
                "copies": 1,
                "outcome": "gone",
                "inCart": 0,
                "message": "The item is not available.",
            },
            {
                "seller": short.offer.seller,
                "card": short.offer.card,
                "copies": 1,
                "outcome": "short",
                "inCart": 0,
                "message": "",
            },
        ]
        assert cart["shipping"] == [
            {
                "seller": "B",
                "estimateMethod": "Letter",
                "estimateCents": 200,
                "cartMethod": "Brief S Priority",
                "cartCents": 255,
                "differs": True,
            }
        ]

    def test_an_offer_cardmarket_refused_or_did_not_keep_is_counted_and_listed_with_its_reason(
        self, two_packages: Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        def refused_and_missing(package: Package) -> CartRun:
            refused, missing = package.picks
            return CartRun(
                CartStatus.STOPPED,
                "Cardmarket refused a card; adding stopped.",
                (
                    ItemResult(refused, CartOutcome.REFUSED, 0, "Your cart is full."),
                    ItemResult(missing, CartOutcome.MISSING, 0),
                ),
                (),
            )

        two_packages.cart_result = refused_and_missing

        with caplog.at_level(logging.INFO):
            add(two_packages.service, 2)
        cart = self.cart_of(two_packages)

        refused, missing = two_packages.cart_calls[0].picks
        assert (cart["added"], cart["refused"], cart["missing"]) == (0, 1, 1)
        assert [(i["seller"], i["outcome"], i["message"]) for i in cart["items"]] == [
            (refused.offer.seller, "refused", "Your cart is full."),
            (missing.offer.seller, "missing", ""),
        ]
        assert "0 cards added, 0 already there, 0 gone, 1 refused, 0 short, 1 missing" in caplog.text

    def test_without_a_scan_or_for_a_package_that_is_not_there_it_is_refused_and_no_window_opens(
        self, rig: Rig
    ) -> None:
        with pytest.raises(BuyError, match=r"That package is gone; scan again\."):
            rig.service.add_to_cart(1, NO_CHECK)
        rig.decks = [make_deck(1, "Ellivere", want("Arcane Signet"), want("Mind Stone"))]
        rig.scan_result = scan_of(two_sellers())
        rig.service.start()
        rig.events.clear()

        with pytest.raises(BuyError, match=r"That package is gone; scan again\."):
            rig.service.add_to_cart(3, NO_CHECK)

        assert rig.events == []

    def test_it_is_refused_while_a_scan_or_another_cart_run_is_going(self, two_packages: Rig) -> None:
        rig = two_packages
        refused: list[str] = []

        def try_again(_: threading.Event) -> None:
            with pytest.raises(BuyError) as error:
                rig.service.add_to_cart(1, NO_CHECK)
            refused.append(str(error.value))

        rig.sync_effect = try_again
        rig.cart_effect = try_again
        rig.service.start()
        add(rig.service, 2)

        assert refused == ["Wait for the scan, or cancel it."] * 2
        assert [p.seller_count for p in rig.cart_calls] == [2]

    def test_the_choices_cannot_change_while_the_cart_is_being_filled(self, two_packages: Rig) -> None:
        rig = two_packages
        refused: list[str] = []

        def change(_: threading.Event) -> None:
            for attempt in (
                lambda: rig.service.include(1, included=False),
                lambda: rig.service.set_filters("any", "any", "any", [], "any"),
                rig.service.start,
            ):
                with pytest.raises(BuyError) as error:
                    attempt()
                refused.append(str(error.value))

        rig.cart_effect = change

        add(rig.service, 2)

        assert refused == [
            "Wait for the scan, or cancel it.",
            "Wait for the scan, or cancel it.",
            "A scan is running already.",
        ]

    def test_cancel_reaches_the_run_and_a_cancelled_report_ends_it(self, two_packages: Rig) -> None:
        rig = two_packages
        seen: list[object] = []

        def cancel(event: threading.Event) -> None:
            seen.append(event.is_set())
            seen.append(rig.service.cancel()["cancelling"])
            seen.append(event.is_set())

        def cancelled(package: Package) -> CartRun:
            items = tuple(ItemResult(pick, CartOutcome.NOT_SENT, None) for pick in package.picks)
            return CartRun(CartStatus.CANCELLED, "", items, ())

        rig.cart_effect = cancel
        rig.cart_result = cancelled

        status = add(rig.service, 2)

        assert seen == [False, True, True]
        assert (status["phase"], status["cancelling"]) == ("carted", False)
        assert (self.cart_of(rig)["status"], self.cart_of(rig)["notSent"]) == ("cancelled", 2)

    @pytest.mark.parametrize("need", [Need.SIGN_IN, Need.CHECK])
    def test_the_screen_gives_the_window_to_the_owner_in_the_cart_run_and_takes_it_back(
        self, two_packages: Rig, need: Need
    ) -> None:
        rig = two_packages
        seen: list[tuple[object, object]] = []

        def hand_over(_: threading.Event) -> None:
            assert rig.handover is not None
            rig.handover.show(need)
            seen.append((rig.service.status()["phase"], rig.service.status()["running"]))
            rig.handover.hide()
            seen.append((rig.service.status()["phase"], rig.service.status()["progress"]))

        rig.cart_effect = hand_over

        add(rig.service, 2)

        assert seen == [(need.value, True), ("cart", "Adding Arcane Signet · 1 of 2")]

    @pytest.mark.parametrize(
        ("failure", "status", "reason"),
        [
            (
                LoadTimeout("slow"),
                "stopped",
                "Cardmarket did not answer; check your connection and try again.",
            ),
            (BrowserError("gone"), "stopped", "The Cardmarket window failed; the details are in the log."),
            (
                PageError("not a cart"),
                "stopped",
                "Cardmarket's page was not as expected; the details are in the log.",
            ),
            (
                CredentialError("locked"),
                "stopped",
                "The saved Cardmarket sign-in could not be read; see the log.",
            ),
            (Cancelled("cancelled in the handover"), "cancelled", ""),
            (RuntimeError("boom"), "stopped", "Something went wrong; check your cart on Cardmarket."),
        ],
    )
    def test_a_run_that_fails_before_sending_is_a_report_of_nothing_sent_and_the_window_is_closed(
        self, two_packages: Rig, failure: Exception, status: str, reason: str
    ) -> None:
        rig = two_packages
        results = rig.service.status()["results"]

        def fail(_: threading.Event) -> None:
            raise failure

        rig.cart_effect = fail

        after = add(rig.service, 2)

        assert rig.events == ["browser opened", "cart", "browser closed"]
        assert after["phase"] == "carted"
        assert after["results"] == results
        cart = self.cart_of(rig)
        assert (cart["status"], cart["reason"], cart["added"], cart["notSent"]) == (status, reason, 0, 2)
        assert [item["outcome"] for item in cast("list[Json]", cart["items"])] == ["not-sent"] * 2
        if reason:
            assert_short(reason)

    @pytest.mark.parametrize("error", [BrowserError("the window is gone"), RuntimeError("boom")])
    def test_a_failure_after_a_cancel_ends_the_run_as_cancelled_and_says_to_check_the_cart(
        self, two_packages: Rig, error: Exception, caplog: pytest.LogCaptureFixture
    ) -> None:
        def close_mid_run(cancel: threading.Event) -> None:
            cancel.set()
            raise error

        two_packages.cart_effect = close_mid_run

        with caplog.at_level(logging.INFO):
            status = add(two_packages.service, 2)

        cart = cast("Json", status["cart"])
        assert (cart["status"], cart["notSent"]) == ("cancelled", 2)
        assert cart["reason"] == "Cancelled while a card was being added; check your cart."
        assert_short(cart["reason"])
        assert "browser closed" in two_packages.events
        assert [r.levelno for r in caplog.records if r.levelno >= logging.ERROR] == []

    def test_an_unexpected_failure_is_logged_with_its_trace(
        self, two_packages: Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        def fail(_: threading.Event) -> None:
            raise RuntimeError("boom")

        two_packages.cart_effect = fail

        with caplog.at_level(logging.ERROR):
            add(two_packages.service, 2)

        traces = [r.exc_info[1] for r in caplog.records if r.exc_info]
        assert [(type(error), str(error)) for error in traces] == [(RuntimeError, "boom")]

    def test_the_run_is_logged_with_its_counts(
        self, two_packages: Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        two_packages.cart_result = mixed_cart

        with caplog.at_level(logging.INFO):
            add(two_packages.service, 2)

        assert "buy: putting the package of 2 sellers (2 cards) in the cart" in caplog.text
        counts = "0 cards added, 0 already there, 1 gone, 0 refused, 1 short, 0 missing"
        assert f"buy: the cart run complete: {counts}" in caplog.text

    def test_a_new_scan_drops_the_report_from_its_start(self, two_packages: Rig) -> None:
        rig = two_packages
        during: list[object] = []
        add(rig.service, 2)
        rig.sync_effect = lambda _cancel: during.append(rig.service.status()["cart"])

        rig.service.start()

        assert during == [None]
        assert rig.service.status()["cart"] is None

    @pytest.mark.parametrize("change", [any_language, leave_out_the_stone, cap_the_price])
    def test_changed_filters_or_ticks_drop_the_report_with_the_results(
        self, two_packages: Rig, change: Callable[[BuyService], object]
    ) -> None:
        add(two_packages.service, 2)
        assert two_packages.service.status()["cart"] is not None

        change(two_packages.service)

        assert cast("Json", two_packages.service.view()["status"])["cart"] is None

    def test_the_results_say_whether_the_scan_was_partial_through_the_cart_run_too(self, rig: Rig) -> None:
        rig.decks = [make_deck(1, "Ellivere", want("Arcane Signet"), want("Mind Stone"))]
        rig.scan_result = scan_of(two_sellers(), status=ScanStatus.CANCELLED)
        rig.service.start()
        assert cast("Json", rig.service.status()["results"])["partial"] is True

        status = add(rig.service, 2)

        assert (status["phase"], cast("Json", status["results"])["partial"]) == ("carted", True)
        rig.scan_result = scan_of(two_sellers())
        rig.service.start()
        assert cast("Json", rig.service.status()["results"])["partial"] is False
        assert status["message"] == "Scan cancelled after 5 pages; results are partial."

    @pytest.mark.parametrize(
        "kind", ["total", "fewer picks", "copies", "article", "nothing like it", "no check"]
    )
    def test_a_package_that_differs_from_the_one_confirmed_is_refused_and_nothing_is_sent(
        self, two_packages: Rig, kind: str
    ) -> None:
        rig = two_packages
        shown = cast("Json", rig.service.package(2)["check"])
        picks = cast("list[list[int]]", shown["picks"])
        wrong: dict[str, object] = {
            "total": {**shown, "totalCents": 491},
            "fewer picks": {**shown, "picks": picks[:1]},
            "copies": {**shown, "picks": [[picks[0][0], 2], *picks[1:]]},
            "article": {**shown, "picks": [[0, 1], *picks[1:]]},
            "nothing like it": NO_CHECK,
            "no check": None,
        }

        with pytest.raises(BuyError, match=r"That package changed; look at it again before adding\."):
            rig.service.add_to_cart(2, wrong[kind])

        assert rig.events == []
        assert rig.cart_calls == []
        assert cast("Json", rig.service.status())["phase"] == "results"

    def test_a_rescan_that_changed_the_package_of_that_many_sellers_refuses_the_old_confirmation(
        self, two_packages: Rig
    ) -> None:
        rig = two_packages
        shown = rig.service.package(2)["check"]
        # Seller B's Signet is now 0,10 dearer (a new article), so the two-seller package costs more.
        rig.scan_result = scan_of(
            [
                offer("Arcane Signet", "A", 500, quantity=2),
                offer("Mind Stone", "A", 60),
                offer("Arcane Signet", "B", 40, quantity=3),
                offer("Mind Stone", "B", 900),
            ]
        )
        rig.service.start()
        assert rig.service.package(2)["totalCents"] == 500

        with pytest.raises(BuyError, match=r"That package changed"):
            rig.service.add_to_cart(2, shown)

        assert rig.cart_calls == []

    def test_the_confirmation_is_the_total_and_the_sorted_article_ids_with_their_copies(
        self, two_packages: Rig
    ) -> None:
        offers = two_packages.scan_result.offers
        # The two-seller package takes B's cheap Signet and A's Stone, one copy each.
        bought = sorted([[offers[2].article_id, 1], [offers[1].article_id, 1]])

        check = two_packages.service.package(2)["check"]

        assert check == {"totalCents": 490, "picks": bought}
