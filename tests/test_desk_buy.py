"""The app's Buy screen through the bridge: the cards of the real decks, the saved choices beside the config
file, and a scan with Cardmarket replaced by fakes."""

import json
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from commander.background import in_background
from commander.buypane import PaneArea
from commander.cmscan import ScanStatus
from commander.desk import Desk, DeskSettings
from commander.net import Pacer
from conftest import REFRESH_TOKEN, FakeArchidekt, Json, MemoryTokenStore
from test_buy import NoCountries, Rig, imported, scan_of, two_sellers
from test_desk import DECK, NOW, WAIT_SECONDS, fake, first_card_id, reviewed, settings

if TYPE_CHECKING:
    from concurrent.futures import Future

__all__ = ["fake", "settings"]  # the fixtures shared with test_desk


def make_desk(
    settings: DeskSettings,
    fake: FakeArchidekt,
    rig: Rig,
    background: Callable[[Callable[[], None]], object] = lambda work: work(),
) -> Desk:
    return Desk(
        settings,
        client_factory=fake.client,
        token_store=MemoryTokenStore({"tester": REFRESH_TOKEN}),
        now=lambda: NOW,
        pacer_factory=lambda _: Pacer(0),
        background=background,
        buy_engine=rig.engine,
    )


@pytest.fixture
def rig(tmp_path: Path, settings: DeskSettings) -> Rig:
    return Rig(tmp_path / "rig", settings.card_db)


@pytest.fixture
def desk(settings: DeskSettings, fake: FakeArchidekt, rig: Rig) -> Desk:
    return make_desk(settings, fake, rig)


def want_arcane_signet(desk: Desk) -> None:
    """Add Arcane Signet to the sample deck: a card put in the deck is labelled Don't Have."""
    desk.propose(DECK, "Arcane Signet")
    desk.decide(DECK, first_card_id(desk), "add", None, "")
    assert desk.commit(DECK, reviewed(desk))["saved"] is True


def decks_of(answer: Json) -> list[Json]:
    return cast("list[Json]", answer["decks"])


def test_a_deck_with_no_dont_have_card_adds_nothing_to_buy(desk: Desk) -> None:
    view = desk.buy()

    assert (view["decks"], view["copies"]) == ([], 0)
    assert cast("Json", view["status"])["phase"] == "idle"


def test_the_decks_dont_have_cards_are_listed_by_deck_with_a_tick(desk: Desk) -> None:
    want_arcane_signet(desk)

    [magda] = decks_of(desk.buy())

    assert (magda["id"], magda["short"], magda["included"], magda["copies"]) == (DECK, "Magda", True, 1)
    assert (magda["tick"], magda["tickedCopies"]) == ("all", 1)
    assert magda["cards"] == [{"name": "Arcane Signet", "quantity": 1, "included": True}]


def test_unticking_a_deck_is_saved_beside_the_config_file(
    settings: DeskSettings, fake: FakeArchidekt, rig: Rig, tmp_path: Path
) -> None:
    configured = replace(settings, config=tmp_path / "appdata" / "config.toml")
    desk = make_desk(configured, fake, rig)
    want_arcane_signet(desk)

    view = desk.buy_include(DECK, False)  # noqa: FBT003 - the page passes the tick positionally

    assert [d["included"] for d in decks_of(view)] == [False]
    saved = (tmp_path / "appdata" / "buy.json").read_text(encoding="utf-8")
    assert '"tester": [\n   10000001\n  ]' in saved
    assert desk.buy_scan() == {"error": "No cards to buy; tick a deck first."}


def test_the_filters_and_base_sellers_are_saved_and_a_bad_choice_is_an_error(desk: Desk) -> None:
    chosen = {
        "language": "any",
        "condition": "EX",
        "foil": "non-foil",
        "countries": [7, 23],
        "delivery": "14",
        "maxPriceCents": 250,
    }
    saved = desk.buy_filters("any", "EX", "non-foil", [23, 7], "14", "2,50")
    assert saved["filters"] == chosen
    assert desk.buy_base_sellers(8)["baseSellers"] == 8

    assert desk.buy_filters("any", "EX", "shiny", [], "any") == {
        "error": "Those filters are not available; choose again."
    }
    assert desk.buy_filters("any", "EX", "any", [7], "3") == {
        "error": "Those filters are not available; choose again."
    }
    assert desk.buy_filters("any", "EX", "any", [7], "any", "free") == {
        "error": "Max price: 0.01 to 10000 euros, at most two decimals."
    }
    assert desk.buy_base_sellers(0) == {"error": "Base sellers must be 1 to 40."}
    assert desk.buy()["filters"] == chosen


def test_japanese_and_both_come_through_the_bridge_and_an_unlisted_language_is_an_error(desk: Desk) -> None:
    for language in ("japanese", "both", "english", "any"):
        view = desk.buy_filters(language, "NM", "any", [], "any")
        assert cast("Json", view["filters"])["language"] == language

    assert desk.buy_filters("french", "NM", "any", [], "any") == {
        "error": "Those filters are not available; choose again."
    }
    assert cast("Json", desk.buy()["filters"])["language"] == "any"


def test_the_price_cap_is_optional_and_blank_means_any_price(desk: Desk) -> None:
    assert desk.buy_filters("english", "NM", "any", [], "any")["filters"] == {
        "language": "english",
        "condition": "NM",
        "foil": "any",
        "countries": [],
        "delivery": "any",
        "maxPriceCents": None,
    }
    capped = desk.buy_filters("english", "NM", "any", [], "any", "5")
    assert cast("Json", capped["filters"])["maxPriceCents"] == 500
    assert (
        cast("Json", desk.buy_filters("english", "NM", "any", [], "any", "")["filters"])["maxPriceCents"]
        is None
    )


def test_a_card_of_a_deck_is_ticked_through_the_bridge_and_saved_beside_the_config_file(
    settings: DeskSettings, fake: FakeArchidekt, rig: Rig, tmp_path: Path
) -> None:
    configured = replace(settings, config=tmp_path / "appdata" / "config.toml")
    desk = make_desk(configured, fake, rig)
    want_arcane_signet(desk)

    view = desk.buy_include_card(DECK, "Arcane Signet", False)  # noqa: FBT003 - the page passes the tick positionally

    [magda] = decks_of(view)
    assert (magda["tick"], magda["included"], view["copies"]) == ("none", False, 0)
    saved = json.loads((tmp_path / "appdata" / "buy.json").read_text(encoding="utf-8"))
    assert saved["leftOut"] == {"tester": [DECK]}
    assert desk.buy_include_card(DECK, "Lotus Petal", True) == {  # noqa: FBT003
        "error": "That card has nothing to buy."
    }
    assert decks_of(desk.buy_include_card(DECK, "Arcane Signet", True))[0]["tick"] == "all"  # noqa: FBT003


def test_a_new_price_cap_is_solved_again_over_the_scan_already_read(desk: Desk, rig: Rig) -> None:
    want_arcane_signet(desk)
    rig.scan_result = scan_of([o for o in two_sellers() if o.card == "Arcane Signet"])
    rig.sync_result = imported(names=("Arcane Signet",))
    desk.buy_scan()

    # Seller B's copy is 0,30 €; a cap of 0,20 € leaves nothing, and no new scan is made.
    view = desk.buy_filters("english", "NM", "any", [], "any", "0,20")

    results = cast("Json", cast("Json", view["status"])["results"])
    assert results["rows"] == []
    assert results["overPrice"] == [{"card": "Arcane Signet", "cheapestCents": 30}]
    assert len(rig.scan_requests) == 1


def test_the_countries_come_through_the_bridge_and_a_failure_is_an_error(desk: Desk, rig: Rig) -> None:
    names = [c["name"] for c in cast("list[Json]", desk.buy_countries()["countries"])]
    assert names == ["Austria", "France", "Germany", "Italy", "Netherlands", "Sweden"]

    rig.routes = NoCountries()

    assert desk.buy_countries() == {"error": "Cardmarket's countries could not be loaded; try again."}


def test_without_an_account_the_screen_asks_for_one(
    settings: DeskSettings, fake: FakeArchidekt, rig: Rig
) -> None:
    desk = make_desk(replace(settings, user=None), fake, rig)

    assert desk.buy() == {"error": "Sign in to Archidekt first."}


def test_a_scan_runs_to_a_package_with_its_detail(desk: Desk, rig: Rig) -> None:
    want_arcane_signet(desk)
    rig.scan_result = scan_of(two_sellers())
    rig.sync_result = imported(names=("Arcane Signet",))

    desk.buy_scan()
    status = desk.buy_status()

    assert (status["phase"], status["running"]) == ("results", False)
    rows = cast("list[Json]", cast("Json", status["results"])["rows"])
    # Seller B's Arcane Signet at 0,30 plus 2,00 of shipping.
    assert [(r["sellers"], r["totalCents"]) for r in rows] == [(1, 230)]
    package = desk.buy_package(1)
    [shipment] = cast("list[Json]", package["shipments"])
    assert (shipment["seller"], shipment["shippingCents"]) == ("B", 200)
    assert cast("list[Json]", shipment["picks"])[0]["trendCents"] == 30


def test_the_pane_area_comes_through_the_bridge_and_a_bad_one_is_an_error(desk: Desk, rig: Rig) -> None:
    assert desk.buy_pane(24, 120, 800, 420, 1.25) == {}
    assert desk.buy_pane(0, 0, 0, 0, 1.25) == {}
    assert desk.buy_pane(24, 120, float("nan"), 420, 1.25) == {"error": "The sign-in area was not valid."}

    assert rig.pane.areas == [PaneArea(24.0, 120.0, 800.0, 420.0, 1.25), None]


def test_a_package_that_is_not_there_is_an_error(desk: Desk) -> None:
    assert desk.buy_package(3) == {"error": "That package is gone; scan again."}


def test_a_stopped_scan_comes_back_as_a_status_not_an_error(desk: Desk, rig: Rig) -> None:
    want_arcane_signet(desk)
    rig.scan_result = scan_of([], status=ScanStatus.STOPPED, reason="a redesign", pages=2)

    desk.buy_scan()

    status = desk.buy_status()
    assert (status["phase"], status["detail"]) == ("stopped", "a redesign")


def test_closing_the_app_cancels_a_scan_that_runs_on_its_own_thread(
    settings: DeskSettings, fake: FakeArchidekt, rig: Rig
) -> None:
    runs: list[Future[None]] = []

    def on_a_thread(work: Callable[[], None]) -> object:
        runs.append(in_background(work, name="test-buy"))
        return runs[-1]

    want_arcane_signet(make_desk(settings, fake, rig))
    desk = make_desk(settings, fake, rig, on_a_thread)
    reading = threading.Event()

    def wait_for_cancel(cancel: threading.Event) -> None:
        reading.set()
        assert cancel.wait(WAIT_SECONDS), "the scan was never told to stop"

    rig.sync_effect = wait_for_cancel

    desk.buy_scan()
    assert reading.wait(WAIT_SECONDS)
    assert desk.buy_status()["running"] is True
    desk.close()

    [run] = runs
    run.result(WAIT_SECONDS)
    status = desk.buy_status()
    assert (status["phase"], status["message"]) == ("cancelled", "Scan cancelled.")
    assert rig.events[-1] == "browser closed"
