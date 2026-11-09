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
from commander.buysettings import BuyError, BuyStore
from commander.buywindows import production_engine
from commander.cmpages import Condition, Offer, Want
from commander.cmscan import Foil, ScanProgress, ScanRequest, ScanResult, ScanStage, ScanStatus
from commander.cmsession import ChallengeError, FormBrowser, LoadTimeout, SignedOut
from commander.cmshipping import CardmarketShipping, Country, ShippingMethod
from commander.cmwants import QuantityMismatch, WantsCancelled, WantsProgress, WantsStep, WantsSync
from commander.credentials import CardmarketSessionStore, SessionCookie
from commander.models import Deck, DeckCard
from commander.net import NetworkError
from commander.offerlimits import OfferLimits
from commander.sellersolve import WantedCard
from test_sellersolve import LETTER, offer

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)

DONT_HAVE = "Don't Have"
SECRET = "SECRET-COOKIE-VALUE"
MAX_WORDS = 12
COOKIE = SessionCookie("PHPSESSID", SECRET, "www.cardmarket.com", "/")
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
        assert (call.language, call.min_condition, call.foil) == (1, Condition.EX, Foil.FOIL)
        [request] = rig.scan_requests
        assert (request.filters.language, request.filters.min_condition, request.filters.foil) == (
            1,
            Condition.EX,
            Foil.FOIL,
        )
        assert request.base_sellers == 9

    def test_any_language_and_condition_reach_the_scan_as_no_filter(self, rig: Rig) -> None:
        rig.service.set_filters("any", "any", "any", [], "any")

        rig.service.start()

        [call] = rig.sync_calls
        assert (call.language, call.min_condition, call.foil) == (None, None, Foil.ANY)

    @pytest.mark.parametrize(
        "choices",
        [
            ("french", "NM", "any", [], "any"),
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

        class Failing(Routes):
            def methods(self, origin_id: int, destination_id: int) -> tuple[ShippingMethod, ...]:  # noqa: ARG002
                raise NetworkError("offline")

        one_card.routes = Failing()

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
