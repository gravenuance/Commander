"""The app's label panel: which labels a deck offers, and giving a card one (ADR 0010)."""

import shutil
from pathlib import Path
from typing import cast

import pytest

from commander.carddb import CARD_DB_FILE
from commander.desk import Desk, DeskSettings
from conftest import DECK_ID, FakeArchidekt, Json, card_entry
from test_cli_apply import run, saved_deck
from test_desk import DECK, make_desk


@pytest.fixture
def fake(raw_deck: Json) -> FakeArchidekt:
    flute = next(c for c in raw_deck["cards"] if c["card"]["oracleCard"]["name"] == "Disruptor Flute")
    flute["label"] = "Proxy,#f445f4"
    archidekt = FakeArchidekt()
    archidekt.put(raw_deck)
    return archidekt


@pytest.fixture
def desk(tmp_path: Path, card_db_file: Path, fake: FakeArchidekt) -> Desk:
    cache = tmp_path / "cache"
    cache.mkdir()
    shutil.copy(card_db_file, cache / CARD_DB_FILE)
    assert (
        run(["sync", "--no-cards", "--no-ideas", "--workspace", str(tmp_path / "decks")], tmp_path, fake).code
        == 0
    )
    return make_desk(DeskSettings(tmp_path / "decks", cache / CARD_DB_FILE, "MAKKIS"), fake)


def label_of(tmp_path: Path, name: str) -> str:
    return next(str(c["label"]) for c in saved_deck(tmp_path / "decks")["cards"] if c["name"] == name)


def test_a_deck_offers_no_label_the_buying_steps_and_its_own_labels(desk: Desk) -> None:
    assert cast("list[Json]", desk.deck(DECK)["labels"]) == [
        {"name": "", "colour": "#656565"},
        {"name": "Don't Have", "colour": "#f47373"},
        {"name": "Getting", "colour": "#2ccce4"},
        {"name": "Proxy", "colour": "#f445f4"},
    ]


def test_a_card_takes_any_offered_label_with_its_colour(
    desk: Desk, fake: FakeArchidekt, tmp_path: Path
) -> None:
    assert desk.set_label(DECK, "Battered Golem", "Proxy") == {
        "card": "Battered Golem",
        "before": "",
        "after": "Proxy",
    }
    assert fake.card_writes()[-1]["modifications"]["label"] == "Proxy,#f445f4"
    assert label_of(tmp_path, "Battered Golem") == "Proxy"
    assert desk.set_label(DECK, "Battered Golem", "")["after"] == ""
    assert fake.card_writes()[-1]["modifications"]["label"] == ",#656565"
    actions = [e["action"] for e in cast("list[Json]", desk.deck(DECK)["activity"])[:2]]
    assert actions == ["unlabelled", "labelled Proxy"]


def test_a_label_the_deck_does_not_offer_or_one_already_set_writes_nothing(
    desk: Desk, fake: FakeArchidekt
) -> None:
    assert desk.set_label(DECK, "Battered Golem", "Wishlist") == {"error": "Magda has no label 'Wishlist'"}
    assert desk.set_label(DECK, "Disruptor Flute", "Proxy") == {
        "error": "Disruptor Flute is already labelled Proxy"
    }
    assert not fake.card_writes()


def test_a_two_faced_card_is_found_by_its_front_face(desk: Desk, fake: FakeArchidekt) -> None:
    fake.decks[DECK_ID]["cards"].append(card_entry(7_000_000_003, "Fire // Ice", ["Burn"]))

    assert desk.set_label(DECK, "fire", "Proxy") == {"card": "Fire // Ice", "before": "", "after": "Proxy"}


def test_a_label_change_archidekt_refuses_is_noted_on_the_board_activity(
    desk: Desk, fake: FakeArchidekt
) -> None:
    fake.write_status["modifyCards"] = 500

    result = desk.set_label(DECK, "Battered Golem", "Proxy")

    assert "HTTP 500" in str(result["error"])
    [latest] = cast("list[Json]", desk.deck(DECK)["activity"])[:1]
    assert (latest["action"], latest["card"]) == ("labelled Proxy failed", "Battered Golem")
    assert "HTTP 500" in latest["detail"]


def test_a_label_whose_read_back_fails_says_to_press_update(desk: Desk, fake: FakeArchidekt) -> None:
    fake.fail_reads_after_edit = True

    result = desk.set_label(DECK, "Battered Golem", "Proxy")

    assert result == {
        "error": "Archidekt took the change, but reading the deck back failed "
        "(https://archidekt.com/api/decks/10004639/ returned HTTP 404). Press Update from Archidekt."
    }


# The account's labels as Archidekt lists them (ADR 0012): shared by every deck, in the account's colours.
ACCOUNT: list[Json] = [
    {"id": 4, "color": "#ab12cd", "name": "Proxy"},
    {"id": 1, "color": "#0DE9FA", "name": "Considering change"},
    {"id": 3, "color": "#0d9ffa", "name": "Getting"},
    {"id": 2, "color": "#fa0d11", "name": "Don't Have"},
]


def grid_card(desk: Desk, name: str) -> Json:
    groups = cast("list[Json]", desk.deck(DECK)["grid"])
    return next(c for g in groups for c in cast("list[Json]", g["cards"]) if c["name"] == name)


def test_every_deck_offers_the_account_labels_in_the_account_colours(desk: Desk, fake: FakeArchidekt) -> None:
    fake.labels = ACCOUNT
    # A label made on the website is on offer at once: each write reads the account's labels first.
    assert desk.set_label(DECK, "Battered Golem", "Considering change")["after"] == "Considering change"
    assert fake.card_writes()[-1]["modifications"]["label"] == "Considering change,#0de9fa"
    assert cast("list[Json]", desk.deck(DECK)["labels"]) == [
        {"name": "", "colour": "#656565"},
        {"name": "Considering change", "colour": "#0de9fa"},
        {"name": "Don't Have", "colour": "#fa0d11"},
        {"name": "Getting", "colour": "#0d9ffa"},
        {"name": "Proxy", "colour": "#ab12cd"},
    ]
    # The deck's Proxy card was labelled in another colour; it shows in the account's.
    assert grid_card(desk, "Disruptor Flute")["labelColour"] == "#ab12cd"


def test_the_buying_steps_are_written_in_the_account_colours(desk: Desk, fake: FakeArchidekt) -> None:
    fake.labels = ACCOUNT
    assert desk.set_label(DECK, "Battered Golem", "Getting")["after"] == "Getting"
    assert fake.card_writes()[-1]["modifications"]["label"] == "Getting,#0d9ffa"
