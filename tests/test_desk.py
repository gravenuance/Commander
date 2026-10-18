"""The desktop app's bridge, called as the page calls it, against the fake Archidekt."""

import json
import re
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import cast

import pytest

from commander import desk as desk_module
from commander.app import desk_settings, page_html
from commander.carddb import CARD_DB_FILE, CardDb
from commander.desk import Desk, DeskSettings
from commander.ideas import compile_ideas
from commander.metadata import CACHE_FOLDER
from commander.net import Pacer
from commander.store import StoredDeck, Workspace
from conftest import REFRESH_TOKEN, FakeArchidekt, Json, MemoryTokenStore
from test_cli_apply import run
from test_cli_meta import MAGDA, magda_page
from test_cli_meta import meta as cli_meta
from test_meta import meta_data, peer

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
    assert (
        run(["sync", "--no-cards", "--no-ideas", "--workspace", str(tmp_path / "decks")], tmp_path, fake).code
        == 0
    )
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


def fetch_meta(settings: DeskSettings, fake: FakeArchidekt) -> None:
    """Fill the meta cache and the board's ideas as a sync does, which the desk's panel only reads."""
    result = cli_meta(settings.workspace.parent, fake, "magda", "--to-board")
    assert result.code == 0, result.err


def items(answer: Json, key: str) -> list[Json]:
    return cast("list[Json]", answer[key])


def names(answer: Json, key: str) -> list[str]:
    return cast("list[str]", answer[key])


def reviewed(desk: Desk) -> str:
    """The board stamp a preview hands back, which a save must present."""
    return str(desk.preview(DECK)["stamp"])


def first_card_id(desk: Desk) -> str:
    return str(items(desk.deck(DECK), "cards")[0]["id"])


WEB = Path(str(resources.files("commander") / "web"))
# What would turn board text into markup.
MARKUP_WRITERS = (".innerHTML", ".outerHTML", "insertAdjacentHTML", "document.write")


def test_page_inlines_its_stylesheet_and_scripts() -> None:
    html = page_html()
    assert "/*APP_CSS*/" not in html
    assert "<script src=" not in html
    assert "--accent:" in html
    assert 'addEventListener("pywebviewready"' in html
    assert "Content-Security-Policy" in html
    # The page builds nodes from text only, so board text can never become markup.
    assert ".innerHTML" not in html


def test_every_script_file_is_inlined_into_the_page() -> None:
    html = page_html()
    scripts = sorted(WEB.glob("*.js"))
    assert len(scripts) > 1
    for script in scripts:
        assert script.read_text(encoding="utf-8") in html, script.name


def test_every_sync_button_has_a_note_that_says_why_it_is_off_and_the_meta_has_no_refresh() -> None:
    html = page_html()
    for button, note in (("sync-decks", "sync-status"), ("no-decks-sync", "no-decks-note")):
        assert f'id="{button}"' in html
        assert f'aria-describedby="{note}"' in html
        assert f'id="{note}"' in html
    assert "meta-refresh" not in html
    assert "Update from Archidekt" not in html


@pytest.mark.parametrize("script", sorted(WEB.glob("*.js")), ids=lambda path: path.name)
def test_no_script_writes_markup(script: Path) -> None:
    text = script.read_text(encoding="utf-8")
    assert [writer for writer in MARKUP_WRITERS if writer in text] == []


def test_decks_are_listed_by_commander_short_name(desk: Desk) -> None:
    assert desk.decks() == {
        "decks": [{"id": DECK, "short": "Magda", "name": "Honestly, though..", "queued": 0, "open": 0}]
    }


def test_a_deck_with_a_damaged_board_lists_with_an_error_and_the_others_still_load(
    desk: Desk, settings: DeskSettings
) -> None:
    first = settings.workspace / "10004639-honestly-though"
    second = settings.workspace / "10004640-second"
    shutil.copytree(first, second)
    snapshot = json.loads((second / "deck.json").read_text(encoding="utf-8"))
    snapshot["id"] = 10004640
    snapshot["name"] = "Zeta"
    (second / "deck.json").write_text(json.dumps(snapshot), encoding="utf-8")
    (first / "board.json").write_text("{not json", encoding="utf-8")
    listed = items(desk.decks(), "decks")
    assert [row["id"] for row in listed] == [10004639, 10004640]
    assert "board.json" in str(listed[0]["error"])
    assert listed[0]["name"] == "Honestly, though.."
    assert "error" not in listed[1]
    assert (listed[1]["name"], listed[1]["queued"]) == ("Zeta", 0)


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
    assert [e["action"] for e in items(view, "activity")] == ["proposed (considering)"]


def test_decisions_and_removal_round_trip(desk: Desk) -> None:
    desk.propose(DECK, "Arcane Signet")
    card_id = first_card_id(desk)
    decided = desk.decide(DECK, card_id, "add", "Disruptor Flute", "")
    assert cast("Json", decided["card"])["choice"] == "Disruptor Flute"
    assert items(desk.decks(), "decks")[0]["queued"] == 1
    assert desk.decide(DECK, card_id, "maybe") == {"error": "unknown decision 'maybe'"}
    assert desk.remove(DECK, card_id) == {"ok": True}
    assert desk.deck(DECK)["cards"] == []


def test_a_card_turned_down_keeps_its_reason_for_the_rejected_list(desk: Desk) -> None:
    desk.propose(DECK, "Arcane Signet")
    card_id = first_card_id(desk)
    desk.decide(DECK, card_id, "reject", None, "rejected in design: no rocks")
    card = items(desk.deck(DECK), "cards")[0]
    assert (card["status"], card["note"]) == ("rejected", "rejected in design: no rocks")
    desk.decide(DECK, card_id, "reopen", None, "")
    assert items(desk.deck(DECK), "cards")[0]["status"] == "open"


def test_settings_offer_commanders_and_tags_and_save_to_the_profile(desk: Desk) -> None:
    assert cast("list[str]", desk.find_commanders("tempes")["names"]) == ["Tempestra, Dame of Games"]
    # Arcane Signet is no commander.
    assert desk.find_commanders("arcane")["names"] == []
    assert desk.find_tags("r")["tags"] == []
    saved = desk.save_settings(DECK, [["Tempestra, Dame of Games"]], ["Stax"])
    assert (saved["adjacent"], saved["ignore"]) == ([["Tempestra, Dame of Games"]], ["Stax"])
    assert desk.settings(DECK)["adjacent"] == [["Tempestra, Dame of Games"]]
    refused = desk.save_settings(DECK, [["Arcane Signet"]], [])
    assert refused == {"error": "'Arcane Signet' is not a commander the card data knows"}
    assert desk.settings(DECK)["adjacent"] == [["Tempestra, Dame of Games"]]


def test_the_mana_tab_reports_lands_and_colour_sources(desk: Desk) -> None:
    mana = desk.mana(DECK)
    lands = cast("Json", mana["lands"])
    # The fixture deck: six Mountains and four spells besides Magda.
    assert lands["lands"] == 6
    colours = cast("list[Json]", mana["colours"])
    assert [colour["colour"] for colour in colours] == ["R"]
    # Without cached meta answers there is nothing to compare with.
    assert mana["comparison"] is None


def test_bracket_5_decks_get_no_mana_suggestion(desk: Desk) -> None:
    # The fixture deck is set to 5 on Archidekt: tournament lists, not Karsten, guide its mana base.
    assert desk.mana(DECK)["plan"] is None
    assert desk.mana_to_board(DECK) == {
        "error": "bracket 5 decks follow the tournament lists, not Karsten's numbers"
    }


def test_below_bracket_5_the_mana_tab_suggests_the_land_count_and_basics(
    desk: Desk, settings: DeskSettings, fake: FakeArchidekt, tmp_path: Path
) -> None:
    fake.decks[DECK]["edhBracket"] = 3
    fake.decks[DECK]["updatedAt"] = "2026-10-16T10:00:00.000000Z"
    assert (
        run(["sync", "--no-cards", "--no-ideas", "--workspace", str(settings.workspace)], tmp_path, fake).code
        == 0
    )
    plan = cast("Json", desk.mana(DECK)["plan"])
    # Six Mountains are far below Karsten's count; with nothing to compare, the count moves.
    change = cast("int", plan["landChange"])
    assert change == round(cast("float", plan["target"]) - cast("float", plan["lands"]))
    assert change > 0
    assert items(plan, "basics") == [
        {"colour": "R", "name": "Mountain", "now": 6, "after": 6 + change, "replaced": 0}
    ]
    # Mono-red with no tap-lands: nothing to swap, so nothing goes on the board.
    assert plan["swaps"] == []
    assert desk.mana_to_board(DECK) == {"added": [], "skipped": 0}


def test_below_bracket_5_considering_marks_cards_over_the_decks_budget(
    desk: Desk, settings: DeskSettings, fake: FakeArchidekt, tmp_path: Path
) -> None:
    fake.decks[DECK]["edhBracket"] = 3
    fake.decks[DECK]["updatedAt"] = "2026-10-16T10:00:00.000000Z"
    assert (
        run(["sync", "--no-cards", "--no-ideas", "--workspace", str(settings.workspace)], tmp_path, fake).code
        == 0
    )
    desk.propose(DECK, "Splinter Twin")
    desk.propose(DECK, "Arcane Signet")
    view = desk.deck(DECK)
    # Magda 6.79, Disruptor Flute 2.17, Battered Golem 0.35: average 3.10, within budget to 25% over it
    # (3.88), ceiling 25% over Magda. Splinter Twin (6.68) is over budget.
    assert view["budget"] == {"working": 310, "within": 388, "ceiling": 849}
    within = {str(card["name"]): card["withinBudget"] for card in items(view, "cards")}
    # The sync put the Maybeboard's Hoverbike (0.14) and Tempestra (0.24) on the board too.
    assert within == {
        "High-Speed Hoverbike": True,
        "Tempestra, Dame of Games": True,
        "Splinter Twin": False,
        "Arcane Signet": True,
    }


def test_bracket_5_decks_have_no_budget_in_considering(desk: Desk) -> None:
    desk.propose(DECK, "Splinter Twin")
    view = desk.deck(DECK)
    assert view["budget"] == {"working": None, "within": None, "ceiling": None}
    assert [card["withinBudget"] for card in items(view, "cards")] == [None]


def test_the_bracket_check_names_what_lifts_the_estimate_above_the_ceiling(
    desk: Desk, settings: DeskSettings, fake: FakeArchidekt
) -> None:
    folder = settings.workspace / "10004639-honestly-though"
    (folder / "profile.toml").write_text("version = 1\nbracket_ceiling = 2\n", encoding="utf-8")
    answer = desk.bracket(DECK)
    assert answer["estimate"] == {"tag": "Ruthless", "brackets": "4+"}
    # The fixture deck is set to 5 on Archidekt.
    assert answer["notes"] == [
        "the profile's bracket ceiling (2) is below the Archidekt bracket (5)",
        "the estimate (4+) is above the profile's bracket ceiling (2)",
    ]
    causes = items(answer, "causes")
    assert [(c["tag"], c["brackets"], c["reason"]) for c in causes] == [
        ("Ruthless", "4+", "a fast two-card combo that wins"),
        ("Spicy", "3-4", "a fast combo of about two cards"),
        ("Powerful", "3+", "a game changer"),
    ]
    assert causes[0]["comboUrl"] == "https://commanderspellbook.com/combo/1486-1651-7298/"
    assert causes[2]["cards"] == ["Ancient Tomb"]
    desk.bracket(DECK)
    assert len(fake.spellbook.bodies) == 1


def test_the_bracket_check_without_spellbook_still_compares_the_ceiling(
    desk: Desk, settings: DeskSettings, fake: FakeArchidekt
) -> None:
    folder = settings.workspace / "10004639-honestly-though"
    (folder / "profile.toml").write_text("version = 1\nbracket_ceiling = 4\n", encoding="utf-8")
    fake.spellbook.status = 403
    answer = desk.bracket(DECK)
    assert answer["estimate"] is None
    assert "HTTP 403" in str(answer["unavailable"])
    assert answer["notes"] == ["the profile's bracket ceiling (4) is below the Archidekt bracket (5)"]
    assert answer["causes"] == []


def test_export_copies_the_saved_deck_for_topdeck(desk: Desk) -> None:
    exported = desk.export(DECK)
    text = cast("str", exported["text"])
    lines = text.splitlines()
    assert lines[:4] == ["~~Commanders~~", "1 Magda, Brazen Outlaw", "", "~~Mainboard~~"]
    assert exported["cards"] == sum(int(line.split(" ", 1)[0]) for line in lines if line[:1].isdigit())


def test_cardmarket_wants_copy_the_dont_have_cards_of_every_deck(desk: Desk) -> None:
    assert desk.cardmarket_wants() == {"text": "", "cards": 0, "skipped": []}
    desk.propose(DECK, "Arcane Signet")
    desk.decide(DECK, first_card_id(desk), "add", None, "")
    assert desk.commit(DECK, reviewed(desk))["saved"] is True

    assert desk.cardmarket_wants() == {"text": "1 Arcane Signet\n", "cards": 1, "skipped": []}
    desk.set_label(DECK, "Arcane Signet", "Getting")
    assert desk.cardmarket_wants() == {"text": "", "cards": 0, "skipped": []}


def test_cardmarket_wants_name_the_decks_whose_labels_are_unknown(desk: Desk, settings: DeskSettings) -> None:
    snapshot = settings.workspace / "10004639-honestly-though" / "deck.json"
    document = json.loads(snapshot.read_text(encoding="utf-8"))
    document["version"] = 2
    for entry in document["cards"]:
        del entry["label"]
    snapshot.write_text(json.dumps(document), encoding="utf-8")

    assert desk.cardmarket_wants() == {"text": "", "cards": 0, "skipped": ["Magda"]}


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


def test_the_deck_view_carries_the_card_grid(desk: Desk) -> None:
    view = desk.deck(DECK)
    assert view["tags"] == ["Dwarves", "Vehicles", "cEDH"]
    # No recent decks with Magda are cached, so there is nothing to lean against.
    assert view["leanings"] == []
    grid = items(view, "grid")
    assert [(g["name"], g["count"]) for g in grid] == [
        ("Commander", 1),
        ("Creature", 1),
        ("Artifact", 1),
        ("Land", 6),
    ]
    golem = cast("list[Json]", grid[1]["cards"])[0]
    assert (golem["name"], golem["label"], golem["category"]) == ("Battered Golem", "", "Combo Pieces")
    assert str(golem["image"]).startswith("https://cards.scryfall.io/normal/front/")


def test_without_cached_lists_no_card_carries_a_share(desk: Desk) -> None:
    desk.propose(DECK, "Lightning Bolt")
    view = desk.deck(DECK)
    [bolt] = items(view, "cards")
    assert bolt["share"] is None
    cards = [c for g in items(view, "grid") for c in cast("list[Json]", g["cards"])]
    assert cards
    assert all(c["share"] is None for c in cards)


def test_the_deck_view_carries_each_cards_combined_share_on_deck_and_board_cards(
    desk: Desk, settings: DeskSettings, fake: FakeArchidekt
) -> None:
    fake.edhtop16.pages[MAGDA] = [magda_page()]
    fetch_meta(settings, fake)
    view = desk.deck(DECK)
    grid = {c["name"]: c for g in items(view, "grid") for c in cast("list[Json]", g["cards"])}
    golem = cast("Json", grid["Battered Golem"]["share"])
    # In the first five of the six lists with a decklist; the sixth weighs 0.525 of the others' 4.75.
    assert (golem["count"], golem["total"], golem["label"]) == (5, 6, "90%")
    assert golem["share"] == pytest.approx(4.75 / 5.275, abs=1e-4)
    assert golem["meta"] == {"share": golem["share"], "count": 5, "total": 6}
    assert golem["archidekt"] is None
    assert golem["tip"] == "meta 90% (5/6 lists)"
    # A card no list plays is 0%, not missing; the commander is in every deck by definition.
    mountain = cast("Json", grid["Mountain"]["share"])
    assert (mountain["count"], mountain["total"], mountain["label"]) == (0, 6, "0%")
    assert grid["Magda, Brazen Outlaw"]["share"] is None
    stone = next(c for c in items(view, "cards") if c["name"] == "Mind Stone")
    assert cast("Json", stone["share"])["label"] == "90%"


def test_each_board_card_comes_with_the_deck_cards_it_could_replace(desk: Desk) -> None:
    desk.propose(DECK, "Lightning Bolt")
    [bolt] = items(desk.deck(DECK), "cards")
    assert bolt["origin"] is None
    targets = cast("list[Json]", bolt["targets"])
    assert targets
    assert {t["source"] for t in targets} == {"role"}
    assert all(str(t["reason"]).startswith(("same role", "fills no role")) for t in targets)
    assert "Magda, Brazen Outlaw" not in {t["name"] for t in targets}


def test_a_compiled_idea_with_no_deck_card_to_replace_comes_in_the_view_with_no_targets(
    settings: DeskSettings, desk: Desk
) -> None:
    deck = Workspace(settings.workspace).load(DECK)
    folder = Workspace(settings.workspace).folder_for(DECK)
    assert deck is not None
    assert folder is not None
    peers = [peer(i, ["Ancient Tomb"]) for i in range(1, 4)]
    with CardDb(settings.card_db) as db:
        compile_ideas(StoredDeck(folder, deck), meta_data([], peers), db, now=lambda: NOW)
    [tomb] = items(desk.deck(DECK), "cards")
    # What the page's uncut() picks up for the "No cut yet" fold: open, with nothing to replace.
    assert (tomb["name"], tomb["status"], tomb["origin"], tomb["targets"]) == (
        "Ancient Tomb",
        "open",
        "archidekt decks",
        [],
    )


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
    # The fake Spellbook answers Ruthless for any list; the deck's bracket 5 allows it.
    bracket = cast("Json", preview["bracket"])
    assert (bracket["after"], bracket["afterLabel"], bracket["check"]) == (
        "4+",
        "Ruthless",
        {"notes": [], "causes": []},
    )
    assert not fake.card_writes()

    saved = desk.commit(DECK, str(preview["stamp"]))
    assert saved["saved"] is True
    assert saved["applied"] == ["Arcane Signet"]
    assert saved["snapshot"] == "before cmdr apply 2026-10-15 12:00:00 UTC"
    assert items(desk.deck(DECK), "cards")[0]["status"] == "applied"


def test_commit_without_a_login_says_how_to_sign_in_in_the_apps_words(
    settings: DeskSettings, fake: FakeArchidekt
) -> None:
    desk = make_desk(settings, fake, signed_in=False)
    desk.propose(DECK, "Arcane Signet")
    card_id = first_card_id(desk)
    desk.decide(DECK, card_id, "add", None, "")
    answer = desk.commit(DECK, reviewed(desk))
    assert answer == {"error": "not signed in to Archidekt as MAKKIS. Sign in to Archidekt."}
    assert not fake.card_writes()


def _commit(desk: Desk) -> Json:
    return desk.commit(DECK, "any")


def _set_label(desk: Desk) -> Json:
    return desk.set_label(DECK, "Battered Golem", "Getting")


def _mana_to_board(desk: Desk) -> Json:
    return desk.mana_to_board(DECK)


@pytest.mark.parametrize(
    ("call", "then"),
    [
        (_commit, "save"),
        (_set_label, "change the label"),
        (_mana_to_board, "add the swaps"),
    ],
)
def test_nothing_changes_the_deck_or_board_while_an_update_runs(
    settings: DeskSettings, fake: FakeArchidekt, call: Callable[[Desk], Json], then: str
) -> None:
    pending: list[Callable[[], None]] = []
    desk = Desk(
        settings,
        client_factory=fake.client,
        token_store=MemoryTokenStore({"makkis": REFRESH_TOKEN}),
        now=lambda: NOW,
        pacer_factory=lambda _: Pacer(0),
        background=pending.append,
    )
    desk.sync()
    assert len(pending) == 1
    assert call(desk) == {"error": f"Wait for the sync to finish, then {then}."}
    assert not fake.card_writes()
    assert not (settings.workspace / "10004639-honestly-though" / "board.json").exists()


def test_commit_refuses_a_board_that_changed_since_the_preview(desk: Desk, fake: FakeArchidekt) -> None:
    desk.propose(DECK, "Arcane Signet")
    desk.decide(DECK, first_card_id(desk), "add", None, "")
    stamp = reviewed(desk)
    desk.propose(DECK, "Lightning Bolt")
    assert desk.commit(DECK, stamp) == {
        "error": "The board changed after the preview; open Review again before saving."
    }
    assert not fake.card_writes()
    assert desk.commit(DECK, reviewed(desk))["saved"] is True


def test_without_a_username_previews_ask_to_sign_in(settings: DeskSettings, fake: FakeArchidekt) -> None:
    desk = make_desk(DeskSettings(settings.workspace, settings.card_db, None), fake)
    assert desk.preview(DECK) == {"error": "Sign in to Archidekt first."}


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


def test_meta_reads_only_the_cache_and_a_sync_fills_it(
    desk: Desk, settings: DeskSettings, fake: FakeArchidekt
) -> None:
    fake.edhtop16.pages[MAGDA] = [magda_page()]
    assert desk.meta(DECK) == {"report": None}
    assert fake.edhtop16.requests == []
    fetch_meta(settings, fake)
    asked = len(fake.edhtop16.requests)
    fresh = desk.meta(DECK)
    report = cast("Json", fresh["report"])
    assert [m["name"] for m in cast("list[Json]", report["missing"])] == ["Mind Stone", "Jeska's Will"]
    assert len(fake.edhtop16.requests) == asked
    assert {"keys", "stoppers", "untouched", "combos"} <= set(fresh)
    assert [k["name"] for k in cast("list[Json]", fresh["keys"])][:1] == ["Magda, Brazen Outlaw"]
    board = items(desk.deck(DECK), "cards")
    # Jeska's Will, in 2 of the 6 lists, is under the Wilson bar.
    assert sorted((c["name"], c["source"], c["origin"]) for c in board) == [("Mind Stone", "cmdr", "meta")]
    # Its share, for ordering Considering: of the six lists with a decklist, Mind Stone is in the first five;
    # the sixth, 20th of 40, weighs 0.525 against 1 to 0.9 for the others.
    [stone] = board
    share = cast("Json", stone["share"])
    assert (share["count"], share["total"]) == (5, 6)
    assert share["share"] == pytest.approx(4.75 / 5.275, abs=1e-4)
    desk.meta(DECK)
    assert len(items(desk.deck(DECK), "cards")) == len(board)
