"""Review for an account other than MAKKIS: when the deck on Archidekt already holds the queued card, and
for a second swap after a saved one."""

import logging
from pathlib import Path
from typing import cast

import pytest

from commander.desk import Desk
from conftest import PASSWORD, FakeArchidekt, Json, card_entry
from test_desk_account import Env

DECK = 10004639
USER = "arxitektonas"
BOARDSAVE_LOG = "commander.boardsave"


@pytest.fixture
def env(tmp_path: Path, raw_deck: Json, card_db_file: Path) -> Env:
    raw_deck["owner"] = {"id": 7, "username": USER}
    env = Env(tmp_path, raw_deck, card_db_file)
    env.fake = FakeArchidekt(USER)
    env.fake.put(raw_deck)
    return env


def signed_in(env: Env) -> Desk:
    """The tester's start: no account, then a sign-in whose first sync fills the account's folder."""
    desk = env.desk()
    assert str(desk.sign_in(USER, PASSWORD)["sync"]).count("Synced 1 deck") == 1
    return desk


def card_id(desk: Desk, name: str) -> str:
    cards = cast("list[Json]", desk.deck(DECK)["cards"])
    return str(next(c for c in cards if c["name"] == name)["id"])


def add_on_archidekt(fake: FakeArchidekt, name: str) -> None:
    fake.decks[DECK]["cards"].append(card_entry(7_000_000_001, name, ["Ramp"]))


def test_a_swap_whose_card_is_already_in_the_deck_on_archidekt_says_so_and_offers_to_mark_it(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    desk = signed_in(env)
    desk.propose(DECK, "Arcane Signet")
    add_on_archidekt(env.fake, "Arcane Signet")
    desk.decide(DECK, card_id(desk, "Arcane Signet"), "add", "Disruptor Flute", "")
    caplog.set_level(logging.INFO, logger=BOARDSAVE_LOG)

    preview = desk.preview(DECK)

    assert (preview["changes"], preview["problems"]) == ([], [])
    already = (
        "Arcane Signet: already in the deck on Archidekt, so its cut Disruptor Flute stays;"
        " saving marks it applied"
    )
    assert preview["skipped"] == [already]
    assert preview["done"] == ["Arcane Signet"]
    assert preview["nothingToSend"] == (
        "Nothing to send to Archidekt: the deck there already has Arcane Signet."
    )
    [record] = [r for r in caplog.records if r.name == BOARDSAVE_LOG]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == f"deck {DECK}: the preview sends nothing to Archidekt: {already}"

    saved = desk.commit(DECK, str(preview["stamp"]))
    assert (saved["saved"], saved["applied"]) == (False, ["Arcane Signet"])
    assert env.fake.card_writes() == []


def test_a_preview_with_changes_has_nothing_done_and_no_empty_reason(env: Env) -> None:
    desk = signed_in(env)
    desk.propose(DECK, "Arcane Signet")
    desk.decide(DECK, card_id(desk, "Arcane Signet"), "add", "Disruptor Flute", "")

    preview = desk.preview(DECK)

    assert [c["name"] for c in cast("list[Json]", preview["changes"])] == ["Disruptor Flute", "Arcane Signet"]
    assert (preview["done"], preview["nothingToSend"]) == ([], None)


def on_archidekt(fake: FakeArchidekt) -> set[str]:
    return {entry["card"]["oracleCard"]["name"] for entry in fake.decks[DECK]["cards"]}


# The tester's sequence: a swap saved, then a second swap; the second may cut the card the first added.
@pytest.mark.parametrize("cut", ["Battered Golem", "Arcane Signet"])
def test_a_second_swap_after_a_saved_one_is_reviewed_and_saved(env: Env, cut: str) -> None:
    desk = signed_in(env)
    desk.propose(DECK, "Arcane Signet")
    desk.decide(DECK, card_id(desk, "Arcane Signet"), "add", "Disruptor Flute", "")
    first = desk.commit(DECK, str(desk.preview(DECK)["stamp"]))
    assert (first["saved"], first["applied"]) == (True, ["Arcane Signet"])
    view = desk.deck(DECK)
    assert view["pending"] == 0
    assert "Arcane Signet" in cast("list[str]", view["deckCards"])
    assert "Disruptor Flute" not in cast("list[str]", view["deckCards"])
    desk.propose(DECK, "Lightning Bolt")
    desk.decide(DECK, card_id(desk, "Lightning Bolt"), "add", cut, "")
    assert desk.deck(DECK)["pending"] == 1

    preview = desk.preview(DECK)

    assert [(c["mark"], c["name"]) for c in cast("list[Json]", preview["changes"])] == [
        ("-", cut),
        ("+", "Lightning Bolt"),
    ]
    assert (preview["problems"], preview["skipped"], preview["done"]) == ([], [], [])
    assert preview["nothingToSend"] is None
    second = desk.commit(DECK, str(preview["stamp"]))
    assert (second["saved"], second["applied"]) == (True, ["Lightning Bolt"])
    assert "Lightning Bolt" in on_archidekt(env.fake)
    assert not {"Disruptor Flute", cut} & on_archidekt(env.fake)
    assert desk.deck(DECK)["pending"] == 0


def test_queueing_a_card_the_synced_deck_already_holds_is_refused(env: Env) -> None:
    desk = signed_in(env)
    desk.propose(DECK, "Arcane Signet")
    add_on_archidekt(env.fake, "Arcane Signet")
    env.fake.decks[DECK]["updatedAt"] = "2026-10-16T12:00:00.000000Z"
    desk.sync()

    answer = desk.decide(DECK, card_id(desk, "Arcane Signet"), "add", "Disruptor Flute", "")

    assert answer == {"error": "Arcane Signet is already in the deck; remove it from the board"}
