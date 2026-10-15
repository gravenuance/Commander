"""The desktop app's bridge, called as the page calls it, against the fake Archidekt."""

import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from commander import desk as desk_module
from commander.app import desk_settings, page_html
from commander.carddb import CARD_DB_FILE
from commander.desk import Desk, DeskSettings
from conftest import REFRESH_TOKEN, FakeArchidekt, Json, MemoryTokenStore
from test_cli_apply import run

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
DECK = 10004639


@pytest.fixture
def fake(raw_deck: Json) -> FakeArchidekt:
    archidekt = FakeArchidekt()
    archidekt.put(raw_deck)
    return archidekt


@pytest.fixture
def settings(tmp_path: Path, card_db_file: Path, fake: FakeArchidekt) -> DeskSettings:
    cache = tmp_path / "cache"
    cache.mkdir()
    shutil.copy(card_db_file, cache / CARD_DB_FILE)
    assert run(["sync", "--no-cards", "--workspace", str(tmp_path / "decks")], tmp_path, fake).code == 0
    fake.requests.clear()
    # Start from an empty board: the sync put the fixture's two Maybeboard cards on it.
    for name in ("board.json", "activity.jsonl"):
        (tmp_path / "decks" / "10004639-honestly-though" / name).unlink()
    return DeskSettings(tmp_path / "decks", cache / CARD_DB_FILE, "MAKKIS")


def make_desk(settings: DeskSettings, fake: FakeArchidekt, *, signed_in: bool = True) -> Desk:
    tokens = MemoryTokenStore({"makkis": REFRESH_TOKEN} if signed_in else {})
    return Desk(settings, client_factory=fake.client, token_store=tokens, now=lambda: NOW)


@pytest.fixture
def desk(settings: DeskSettings, fake: FakeArchidekt) -> Desk:
    return make_desk(settings, fake)


def items(answer: Json, key: str) -> list[Json]:
    return cast("list[Json]", answer[key])


def names(answer: Json, key: str) -> list[str]:
    return cast("list[str]", answer[key])


def first_card_id(desk: Desk) -> str:
    return str(items(desk.deck(DECK), "cards")[0]["id"])


def test_page_inlines_its_stylesheet_and_script() -> None:
    html = page_html()
    assert "/*APP_CSS*/" not in html
    assert "/*APP_JS*/" not in html
    assert "--accent:" in html
    assert 'addEventListener("pywebviewready"' in html
    assert "Content-Security-Policy" in html
    # The page builds nodes from text only, so board text can never become markup.
    assert ".innerHTML" not in html


def test_decks_are_listed_by_commander_short_name(desk: Desk) -> None:
    assert desk.decks() == {
        "decks": [{"id": DECK, "short": "Magda", "name": "Honestly, though..", "queued": 0, "open": 0}]
    }


def test_a_proposed_card_shows_with_its_card_facts(desk: Desk) -> None:
    assert "card" in desk.propose(DECK, "lightning bolt")
    view = desk.deck(DECK)
    [card] = items(view, "cards")
    assert (card["name"], card["manaCost"], card["status"], card["source"]) == (
        "Lightning Bolt",
        "{R}",
        "open",
        "you",
    )
    assert card["text"] == "Lightning Bolt deals 3 damage to any target."
    assert re.fullmatch(
        r"https://cards\.scryfall\.io/normal/front/[0-9a-f]/[0-9a-f]/[0-9a-f-]{36}\.jpg", str(card["image"])
    )
    assert "Magda, Brazen Outlaw" not in names(view, "deckCards")
    assert "Battered Golem" in names(view, "deckCards")
    assert [e["action"] for e in items(view, "activity")] == ["proposed (considered)"]


def test_decisions_and_removal_round_trip(desk: Desk) -> None:
    desk.propose(DECK, "Arcane Signet")
    card_id = first_card_id(desk)
    decided = desk.decide(DECK, card_id, "add", "Disruptor Flute", "")
    assert cast("Json", decided["card"])["choice"] == "Disruptor Flute"
    assert items(desk.decks(), "decks")[0]["queued"] == 1
    assert desk.decide(DECK, card_id, "maybe") == {"error": "unknown decision 'maybe'"}
    assert desk.remove(DECK, card_id) == {"ok": True}
    assert desk.deck(DECK)["cards"] == []


def test_refusals_come_back_as_errors(desk: Desk) -> None:
    assert desk.propose(DECK, "Rhystic Study") == {"error": "Rhystic Study is outside the deck's colours (R)"}
    assert str(desk.deck(999)["error"]).startswith("deck 999 is not in ")


def test_search_finds_names_in_colour_and_not_in_the_deck(desk: Desk) -> None:
    assert [c["name"] for c in items(desk.search(DECK, "bolt"), "cards")] == ["Lightning Bolt"]
    assert desk.search(DECK, "rhystic")["cards"] == []
    assert desk.search(DECK, "battered")["cards"] == []


def test_stamps_change_when_the_board_does(desk: Desk) -> None:
    before = desk.stamps()["stamps"]
    desk.propose(DECK, "Lightning Bolt")
    assert desk.stamps()["stamps"] != before


def test_preview_and_commit_save_the_queued_cards(desk: Desk, fake: FakeArchidekt) -> None:
    desk.propose(DECK, "Arcane Signet")
    card_id = first_card_id(desk)
    desk.decide(DECK, card_id, "add", "Disruptor Flute", "")

    preview = desk.preview(DECK)
    assert [(c["mark"], c["name"]) for c in items(preview, "changes")] == [
        ("-", "Disruptor Flute"),
        ("+", "Arcane Signet"),
    ]
    assert (preview["before"], preview["after"], preview["problems"]) == (9, 9, [])
    assert not fake.card_writes()

    saved = desk.commit(DECK)
    assert saved["saved"] is True
    assert saved["applied"] == ["Arcane Signet"]
    assert saved["snapshot"] == "before cmdr apply 2026-10-15 12:00:00 UTC"
    assert items(desk.deck(DECK), "cards")[0]["status"] == "applied"


def test_commit_without_a_login_says_how_to_sign_in(settings: DeskSettings, fake: FakeArchidekt) -> None:
    desk = make_desk(settings, fake, signed_in=False)
    desk.propose(DECK, "Arcane Signet")
    card_id = first_card_id(desk)
    desk.decide(DECK, card_id, "add", None, "")
    answer = desk.commit(DECK)
    assert "cmdr login" in str(answer["error"])
    assert not fake.card_writes()


def test_without_a_username_previews_ask_for_cmdr_init(settings: DeskSettings, fake: FakeArchidekt) -> None:
    desk = make_desk(DeskSettings(settings.workspace, settings.card_db, None), fake)
    assert desk.preview(DECK) == {"error": "no Archidekt username; run `cmdr init` first"}


def test_unexpected_failures_are_logged_not_raised(desk: Desk, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_folder: Path) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(desk_module, "read_board", broken)
    assert desk.decks() == {"error": "Something went wrong; the details are in the log."}


def test_settings_without_a_username_still_open(tmp_path: Path) -> None:
    env = {"APPDATA": str(tmp_path), "LOCALAPPDATA": str(tmp_path), "CMDR_WORKSPACE": str(tmp_path / "decks")}
    settings = desk_settings(env, config=None, workspace=None, user=None)
    assert (settings.workspace, settings.user) == (tmp_path / "decks", None)
    assert settings.card_db == tmp_path / "Commander" / CARD_DB_FILE
