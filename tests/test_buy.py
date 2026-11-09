"""The Buy screen's engine, from the cards to buy to a package of sellers, with Cardmarket and its windows
replaced by fakes (the real readers have their own tests); the solver and the card data are real."""

import logging
import re
import threading
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from commander.buy import BuyEngine, BuyService, RouteSource
from commander.buysettings import BuyError, BuyStore
from commander.buywindows import production_engine
from commander.cmpages import Condition, Offer, Want
from commander.cmscan import Foil, ScanProgress, ScanRequest, ScanResult, ScanStage, ScanStatus
from commander.cmsession import ChallengeError, FormBrowser, LoadTimeout, SignedOut
from commander.cmshipping import CardmarketShipping, ShippingMethod
from commander.cmwants import QuantityMismatch, WantsCancelled, WantsProgress, WantsStep, WantsSync
from commander.credentials import CardmarketSessionStore, SessionCookie
from commander.models import Deck, DeckCard
from commander.net import NetworkError
from commander.sellersolve import WantedCard
from test_sellersolve import LETTER, offer

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)

DONT_HAVE = "Don't Have"
SECRET = "SECRET-COOKIE-VALUE"
MAX_WORDS = 12
COOKIE = SessionCookie("PHPSESSID", SECRET, "www.cardmarket.com", "/")
Json = dict[str, Any]


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

    def country_id(self, name: str) -> int | None:
        return {"Sweden": 28, "France": 12}.get(name)

    def methods(self, origin_id: int, destination_id: int) -> tuple[ShippingMethod, ...]:
        self.asked.append((origin_id, destination_id))
        return (LETTER,)


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
    language: int | None
    min_condition: Condition | None
    foil: Foil


class Rig:
    """A service whose Cardmarket is a set of fakes, and a record of what it was asked."""

    def __init__(self, tmp_path: Path, card_db: Path, *, session: bool = True) -> None:
        self.decks = fresh_decks()
        self.events: list[str] = []
        self.browser_closed = threading.Event()
        self.progress: list[str] = []
        self.session: SessionCookie | None = COOKIE if session else None
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
        self.sign_in_effect: Callable[[Callable[[], None], Callable[[], None]], None] = lambda ok, _closed: (
            ok()
        )
        self.service = self.build()

    def build(self) -> BuyService:
        self.engine = BuyEngine(
            open_browser=self._open_browser,
            open_sign_in=self._open_sign_in,
            sessions=self,
            shipping=self._shipping,
            sync_wants=self._sync_wants,
            scan=self._scan,
        )
        return BuyService(
            store=BuyStore(self.settings_path),
            decks=lambda: self.decks,
            user=lambda: "MAKKIS",
            engine=self.engine,
            card_db=self.card_db,
            background=lambda work: work(),
        )

    # The session store the engine reads.
    def get(self) -> SessionCookie | None:
        return self.session

    def put(self, cookie: SessionCookie) -> None:
        self.session = cookie

    def delete(self) -> bool:
        self.session, had = None, self.session is not None
        return had

    def _open_browser(self) -> FormBrowser:
        self.events.append("browser opened")
        return cast("FormBrowser", Closer(self.events, "browser", self.browser_closed))

    def _open_sign_in(self, signed_in: Callable[[], None], closed: Callable[[], None]) -> Closer:
        self.events.append("sign-in opened")
        self.sign_in_effect(signed_in, closed)
        return Closer(self.events, "sign-in")

    @contextmanager
    def _shipping(self) -> Generator[RouteSource]:
        self.events.append("shipping")
        yield self.routes

    def _sync_wants(
        self,
        _browser: object,
        wanted: list[WantedCard],
        *,
        language: int | None,
        min_condition: Condition | None,
        foil: Foil,
        cancel: threading.Event,
        on_progress: Callable[[WantsProgress], None],
    ) -> WantsSync:
        self.events.append("sync")
        self.sync_calls.append(SyncCall(wanted, language, min_condition, foil))
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


def test_the_production_engine_reads_shipping_through_the_cache_and_keeps_the_session_in_the_credential_store(
    tmp_path: Path,
) -> None:
    engine = production_engine(httpx.Client, tmp_path, lambda: NOW)

    assert isinstance(engine.sessions, CardmarketSessionStore)
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
                "copies": 2,
                "cards": [{"name": "Arcane Signet", "quantity": 1}, {"name": "Lotus Petal", "quantity": 1}],
            },
            {
                "id": 2,
                "short": "Pantlaza",
                "name": "Pantlaza, Sun-Favored deck",
                "known": True,
                "included": True,
                "copies": 3,
                "cards": [{"name": "Arcane Signet", "quantity": 2}, {"name": "Mind Stone", "quantity": 1}],
            },
        ]
        assert view["copies"] == 5

    def test_a_deck_whose_labels_were_never_kept_is_listed_but_not_ticked(self, rig: Rig) -> None:
        rig.decks.append(make_deck(4, "Old Deck", card("Sol Ring", 1, None), card("Island", 1, None)))

        old = next(d for d in cast("list[Json]", rig.service.view()["decks"]) if d["id"] == 4)

        assert (old["known"], old["included"], old["copies"], old["cards"]) == (False, False, 0, [])

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
        assert "leftOut" not in rig.settings_path.read_text(encoding="utf-8").replace('"leftOut": {}', "")

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

        assert view["filters"] == {"language": "english", "condition": "NM", "foil": "any"}
        assert view["baseSellers"] == 15

    def test_chosen_filters_are_saved_and_found_again_by_a_new_service(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        rig.service.set_filters("any", "any", "non-foil")
        rig.service.set_base_sellers(20)

        view = Rig(tmp_path, rig.card_db).service.view()

        assert view["filters"] == {"language": "any", "condition": "any", "foil": "non-foil"}
        assert view["baseSellers"] == 20

    def test_the_scan_gets_the_filters_chosen(self, rig: Rig) -> None:
        rig.service.set_filters("english", "EX", "foil")
        rig.service.set_base_sellers(9)

        rig.service.start()

        [call] = rig.sync_calls
        assert (call.language, call.min_condition, call.foil) == (1, Condition.EX, Foil.FOIL)
        [request] = rig.scan_requests
        assert (request.filters.language, request.filters.min_condition, request.filters.foil) == (
            1,
            Condition.EX,
            Foil.FOIL,
        )
        assert request.base_sellers == 9

    def test_any_language_and_condition_reach_the_scan_as_no_filter(self, rig: Rig) -> None:
        rig.service.set_filters("any", "any", "any")

        rig.service.start()

        [call] = rig.sync_calls
        assert (call.language, call.min_condition, call.foil) == (None, None, Foil.ANY)

    @pytest.mark.parametrize(
        ("language", "condition", "foil"),
        [("french", "NM", "any"), ("english", "XX", "any"), ("english", "NM", "shiny"), (1, "NM", "any")],
    )
    def test_a_choice_that_is_not_offered_is_refused_and_nothing_is_saved(
        self, rig: Rig, language: object, condition: object, foil: object
    ) -> None:
        with pytest.raises(BuyError, match="not available"):
            rig.service.set_filters(language, condition, foil)

        assert not rig.settings_path.exists()

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

        assert view["filters"] == {"language": "english", "condition": "NM", "foil": "any"}
        assert rig.settings_path.with_name("buy.json.bak").read_text(encoding="utf-8") == "{not json"
        assert "unusable" in caplog.text

    def test_a_file_of_a_newer_format_is_kept_aside_too(self, rig: Rig) -> None:
        rig.settings_path.parent.mkdir(parents=True)
        rig.settings_path.write_text('{"version": 2, "baseSellers": 30}', encoding="utf-8")

        view = rig.build().view()

        assert view["baseSellers"] == 15
        assert rig.settings_path.with_name("buy.json.bak").exists()

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

        assert view["filters"] == {"language": "english", "condition": "EX", "foil": "foil"}
        assert view["baseSellers"] == 15
        assert [(d["id"], d["included"]) for d in cast("list[Json]", view["decks"])] == [
            (1, True),
            (2, False),
        ]
        assert "klingon" in caplog.text

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

    @pytest.mark.parametrize("change", ["deck", "filters", "base"])
    def test_changing_a_choice_forgets_the_results(self, rig: Rig, change: str) -> None:
        scanned(rig)

        if change == "deck":
            rig.service.include(1, included=False)
        elif change == "filters":
            rig.service.set_filters("english", "NM", "foil")
        else:
            rig.service.set_base_sellers(10)

        status = rig.service.status()
        assert (status["phase"], status["results"]) == ("idle", None)
        with pytest.raises(BuyError, match=re.escape("That package is gone; scan again.")):
            rig.service.package(1)

    def test_the_same_choices_again_keep_the_results(self, rig: Rig) -> None:
        scanned(rig)

        rig.service.set_filters("english", "NM", "any")
        rig.service.set_base_sellers(15)

        assert rig.service.status()["results"] is not None


class TestSigningIn:
    def test_without_a_saved_session_the_sign_in_window_opens_first_and_closes_when_signed_in(
        self, tmp_path: Path, card_db_file: Path
    ) -> None:
        rig = Rig(tmp_path, card_db_file, session=False)

        rig.service.start()

        assert rig.events[:4] == ["sign-in opened", "sign-in closed", "browser opened", "sync"]
        assert rig.service.status()["phase"] == "results"

    def test_a_saved_session_that_has_ended_opens_the_sign_in_and_then_scans_in_a_new_window(
        self, rig: Rig
    ) -> None:
        sync_attempts: list[int] = []

        def ended(_: threading.Event) -> None:
            sync_attempts.append(1)
            if len(sync_attempts) == 1:
                raise SignedOut("the wants page asks to sign in")

        rig.sync_effect = ended

        rig.service.start()

        assert rig.events == [
            "browser opened",
            "sync",
            "browser closed",
            "sign-in opened",
            "sign-in closed",
            "browser opened",
            "sync",
            "scan",
            "browser closed",
        ]
        assert rig.service.status()["phase"] == "results"

    def test_a_session_that_ends_again_right_after_signing_in_stops_the_scan(self, rig: Rig) -> None:
        def ended(_: threading.Event) -> None:
            raise SignedOut("asks to sign in")

        rig.sync_effect = ended

        rig.service.start()
        status = rig.service.status()

        assert (status["phase"], status["message"]) == (
            "stopped",
            "Cardmarket still asks you to sign in; scan again.",
        )
        assert rig.events.count("sign-in opened") == 1
        assert rig.events.count("browser closed") == 2
        assert_short(status["message"])

    def test_closing_the_sign_in_window_cancels_and_scanning_again_asks_again(
        self, tmp_path: Path, card_db_file: Path
    ) -> None:
        rig = Rig(tmp_path, card_db_file, session=False)
        rig.sign_in_effect = lambda _ok, closed: closed()

        rig.service.start()
        status = rig.service.status()

        assert (status["phase"], status["message"]) == (
            "cancelled",
            "Sign-in window closed; scan to sign in again.",
        )
        assert_short(status["message"])
        assert rig.events == ["sign-in opened", "sign-in closed"]
        assert status["results"] is None

    def test_cancel_while_waiting_for_the_sign_in_closes_the_window(
        self, tmp_path: Path, card_db_file: Path
    ) -> None:
        rig = Rig(tmp_path, card_db_file, session=False)

        def cancel_it(_ok: Callable[[], None], _closed: Callable[[], None]) -> None:
            rig.service.cancel()

        rig.sign_in_effect = cancel_it

        rig.service.start()
        status = rig.service.status()

        assert (status["phase"], status["message"]) == ("cancelled", "Sign-in cancelled.")
        assert rig.events == ["sign-in opened", "sign-in closed"]


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
            (ChallengeError("check"), "Cardmarket is checking your browser; wait a minute, then scan again."),
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

    def test_a_shipping_table_that_cannot_be_fetched_stops_the_scan(self, rig: Rig) -> None:
        rig.scan_result = scan_of(two_sellers())

        class Failing(Routes):
            def methods(self, origin_id: int, destination_id: int) -> tuple[ShippingMethod, ...]:  # noqa: ARG002
                raise NetworkError("offline")

        rig.routes = Failing()

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
        for effect in (SignedOut("x"), ChallengeError("x"), LoadTimeout("x"), RuntimeError("x")):

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
                lambda: rig.service.set_filters("any", "any", "any"),
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

    def test_a_scan_is_logged_by_stage_and_never_logs_the_session_cookie(
        self, rig: Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        rig.scan_result = scan_of(two_sellers())

        with caplog.at_level(logging.DEBUG):
            rig.service.start()

        assert SECRET not in caplog.text
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
