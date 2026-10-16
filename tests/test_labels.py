"""The app's label panel: which labels a deck offers, and giving a card one (ADR 0010)."""

import shutil
from pathlib import Path
from typing import cast

import pytest

from commander.carddb import CARD_DB_FILE
from commander.desk import Desk, DeskSettings
from conftest import FakeArchidekt, Json
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
    assert run(["sync", "--no-cards", "--workspace", str(tmp_path / "decks")], tmp_path, fake).code == 0
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
