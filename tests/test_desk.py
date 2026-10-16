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
from commander.metadata import CACHE_FOLDER
from commander.net import Pacer
from conftest import REFRESH_TOKEN, FakeArchidekt, Json, MemoryTokenStore
from test_cli_apply import run
from test_cli_meta import MAGDA, magda_page

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
    return Desk(
        settings,
        client_factory=fake.client,
        token_store=tokens,
        now=lambda: NOW,
        pacer_factory=lambda _: Pacer(0),
    )


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


def test_the_meta_stamp_changes_when_a_meta_answer_is_saved(desk: Desk, settings: DeskSettings) -> None:
    # `cmdr meta` writes the same cache the panel reads; the app rereads the panel when this changes.
    assert desk.stamps()["meta"] == "-"
    (settings.cache / CACHE_FOLDER).mkdir()
    (settings.cache / CACHE_FOLDER / "edhtop16-magda.json").write_text("{}", encoding="utf-8")
    assert str(desk.stamps()["meta"]).startswith("1:")


def test_each_board_card_comes_with_the_deck_cards_it_could_replace(desk: Desk) -> None:
    desk.propose(DECK, "Lightning Bolt")
    [bolt] = items(desk.deck(DECK), "cards")
    targets = cast("list[Json]", bolt["targets"])
    assert targets
    assert {t["source"] for t in targets} == {"role"}
    assert all(str(t["reason"]).startswith(("same role", "fills no role")) for t in targets)
    assert "Magda, Brazen Outlaw" not in {t["name"] for t in targets}


def test_a_card_saved_into_the_deck_is_to_get_until_it_arrives(desk: Desk) -> None:
    assert desk.deck(DECK)["toGet"] == []
    desk.propose(DECK, "Arcane Signet")
    desk.decide(DECK, first_card_id(desk), "add", None, "")
    assert desk.commit(DECK)["saved"] is True
    signet = {"name": "Arcane Signet", "quantity": 1, "label": "Don't Have", "category": "Artifact"}
    assert desk.deck(DECK)["toGet"] == [signet]

    ordered = desk.mark(DECK, "Arcane Signet", "ordered")
    assert (ordered["before"], ordered["after"]) == ("Don't Have", "Getting")
    assert ordered["toGet"] == [{**signet, "label": "Getting"}]
    assert desk.mark(DECK, "Arcane Signet", "arrived")["toGet"] == []
    assert desk.mark(DECK, "Arcane Signet", "lost") == {"error": "unknown step 'lost'"}
    assert desk.mark(DECK, "Arcane Signet", "arrived") == {"error": "Arcane Signet is already marked arrived"}


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


def test_meta_opens_from_cache_and_refreshes_on_demand(desk: Desk, fake: FakeArchidekt) -> None:
    fake.edhtop16.pages[MAGDA] = [magda_page()]
    assert desk.meta(DECK) == {"report": None, "errors": []}
    assert fake.edhtop16.requests == []
    fresh = desk.refresh_meta(DECK)
    report = cast("Json", fresh["report"])
    assert [m["name"] for m in cast("list[Json]", report["missing"])] == ["Mind Stone", "Jeska's Will"]
    asked = len(fake.edhtop16.requests)
    cached = cast("Json", desk.meta(DECK)["report"])
    assert cached["missing"] == report["missing"]
    assert len(fake.edhtop16.requests) == asked


def test_a_failed_refresh_with_nothing_cached_says_why(desk: Desk, fake: FakeArchidekt) -> None:
    fake.edhtop16.status = 403
    fake.peer_page = {"bad": True}
    answer = desk.refresh_meta(DECK)
    assert answer["report"] is None
    assert any("HTTP 403" in str(e) for e in names(answer, "errors"))


def test_refreshing_meta_needs_a_username(settings: DeskSettings, fake: FakeArchidekt) -> None:
    nameless = make_desk(DeskSettings(settings.workspace, settings.card_db, None), fake)
    assert nameless.refresh_meta(DECK) == {"error": "no Archidekt username; run `cmdr init` first"}
