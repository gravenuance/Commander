"""The desktop app's bridge, called as the page calls it, against the fake Archidekt."""

import copy
import json
import re
import shutil
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import cast

import pytest

from commander import apply as apply_module
from commander import desk as desk_module
from commander import syncrun
from commander.app import desk_settings, page_html
from commander.board import Actor, BoardService, Origin, OriginPool, Status, read_board
from commander.carddb import CARD_DB_FILE, CardDb
from commander.desk import Desk, DeskSettings
from commander.ideas import compile_ideas
from commander.metadata import CACHE_FOLDER
from commander.net import Pacer
from commander.scryfall import CardRefresh
from commander.store import StoredDeck, Workspace
from commander.viewscore import ViewScores
from conftest import REFRESH_TOKEN, FakeArchidekt, Json, MemoryTokenStore
from test_cli_apply import run
from test_cli_meta import MAGDA, magda_page
from test_cli_meta import meta as cli_meta
from test_ideas import ALIKE, tag_pool
from test_meta import meta_data, peer
from test_syncrun import DOWNLOADED, stub_meta

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


def save(
    desk: Desk,
    adjacent: list[list[str]],
    ignore: list[str],
    *,
    tag_search: bool,
    grace: Sequence[tuple[str, object]] = (),
) -> Json:
    return desk.save_settings(
        DECK, adjacent, ignore, tag_search, [{"name": name, "factor": factor} for name, factor in grace]
    )


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


def test_the_review_dialog_has_an_alert_line_that_a_refusal_fills_as_text() -> None:
    review = page_html().split('<dialog id="review-dialog"', 1)[1].split("</dialog>", 1)[0]
    assert '<p id="review-error" class="field-error" role="alert" hidden></p>' in review
    script = (WEB / "review.js").read_text(encoding="utf-8")
    assert 'const problem = $("review-error");' in script
    assert "problem.textContent = error instanceof Error ? error.message : String(error);" in script
    assert "problem.hidden = false;" in script


def test_the_signed_out_page_has_an_empty_state_with_a_sign_in_button() -> None:
    html = page_html()
    empty = html.split('<div id="signed-out" class="state" hidden>', 1)[1].split("</div>", 1)[0]
    assert 'class="state-icon" aria-hidden="true"' in empty
    assert "<span>Sign in to see your decks</span>" in empty
    assert '<button id="signed-out-sign-in" class="button primary" type="button">Sign in</button>' in empty


def test_the_sign_in_dialog_blurs_and_dims_the_app_behind_it_with_tokens_for_both_themes() -> None:
    css = (WEB / "app.css").read_text(encoding="utf-8")
    assert "#signin-dialog::backdrop {" in css
    assert "backdrop-filter: blur(var(--signin-blur));" in css
    assert "@media (prefers-reduced-transparency: reduce)" in css
    light, dark = css.split("@media (prefers-color-scheme: dark)", 1)
    for token in ("--signin-dim:", "--signin-dim-solid:", "--signin-blur:"):
        assert token in light
    for token in ("--signin-dim:", "--signin-dim-solid:"):
        assert token in dark.split("* { box-sizing", 1)[0]


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


def legacy_card(settings: DeskSettings, name: str) -> str:
    """The card on the board as the retired Maybeboard pick-up left it, turned down by the user."""
    deck = Workspace(settings.workspace).load(DECK)
    folder = Workspace(settings.workspace).folder_for(DECK)
    assert deck is not None
    assert folder is not None
    with CardDb(settings.card_db) as db:
        service = BoardService(folder, deck, db, now=lambda: NOW)
        existing = read_board(folder).find(name)
        if existing is not None:
            service.remove(existing.id, Actor.YOU)
        card = service.propose(name, Actor.ARCHIDEKT, reason="on the Maybeboard on Archidekt")
        service.decide(card.id, Status.REJECTED, Actor.YOU, note="not now")
    return card.id


def test_reconsidering_a_turned_down_maybeboard_card_relabels_it_from_the_cached_lists(
    desk: Desk, settings: DeskSettings, fake: FakeArchidekt
) -> None:
    fake.edhtop16.pages[MAGDA] = [magda_page()]
    fetch_meta(settings, fake)
    card_id = legacy_card(settings, "Mind Stone")

    desk.decide(DECK, card_id, "reopen", None, "")

    stone = next(c for c in items(desk.deck(DECK), "cards") if c["id"] == card_id)
    assert (stone["status"], stone["source"], stone["origin"]) == ("open", "cmdr", "meta")
    assert stone["reason"] == "in 5 of 6 Magda lists on EDHTop16 since 2026-08-16"
    # Newest first: the reopen stays on the activity line, by the user.
    latest = items(desk.deck(DECK), "activity")[0]
    assert (latest["action"], latest["actor"], latest["card"]) == ("reopened", "you", "Mind Stone")


def test_reconsidering_a_card_no_cached_list_plays_keeps_it_neutral_and_says_so(
    desk: Desk, settings: DeskSettings
) -> None:
    card_id = legacy_card(settings, "Mind Stone")

    desk.decide(DECK, card_id, "reopen", None, "")

    stone = items(desk.deck(DECK), "cards")[0]
    assert (stone["status"], stone["source"], stone["origin"]) == ("open", "archidekt", None)
    assert stone["reason"] == "reconsidered; no list plays it"


def test_undoing_a_queued_swap_leaves_an_old_pick_up_as_it_was(
    desk: Desk, settings: DeskSettings, fake: FakeArchidekt
) -> None:
    fake.edhtop16.pages[MAGDA] = [magda_page()]
    fetch_meta(settings, fake)
    card_id = legacy_card(settings, "Mind Stone")
    desk.decide(DECK, card_id, "add", "Disruptor Flute", "")

    desk.decide(DECK, card_id, "reopen", None, "")

    stone = next(c for c in items(desk.deck(DECK), "cards") if c["id"] == card_id)
    assert (stone["status"], stone["source"], stone["origin"], stone["reconsidered"]) == (
        "open",
        "archidekt",
        None,
        False,
    )
    assert stone["reason"] == "on the Maybeboard on Archidekt"


def test_settings_offer_commanders_and_tags_and_save_to_the_profile(desk: Desk) -> None:
    assert cast("list[str]", desk.find_commanders("tempes")["names"]) == ["Tempestra, Dame of Games"]
    # Arcane Signet is no commander.
    assert desk.find_commanders("arcane")["names"] == []
    assert desk.find_tags("r")["tags"] == []
    saved = save(desk, [["Tempestra, Dame of Games"]], ["Stax"], tag_search=False)
    assert (saved["adjacent"], saved["ignore"]) == ([["Tempestra, Dame of Games"]], ["Stax"])
    assert desk.settings(DECK)["adjacent"] == [["Tempestra, Dame of Games"]]
    refused = save(desk, [["Arcane Signet"]], [], tag_search=False)
    assert refused == {"error": "'Arcane Signet' is not a commander the card data knows"}
    assert desk.settings(DECK)["adjacent"] == [["Tempestra, Dame of Games"]]


def test_searching_decks_by_tag_is_off_in_the_settings_until_saved_on(
    desk: Desk, settings: DeskSettings
) -> None:
    assert desk.settings(DECK)["tagSearch"] is False
    assert save(desk, [], [], tag_search=True)["tagSearch"] is True
    assert desk.settings(DECK)["tagSearch"] is True
    profile = settings.workspace / "10004639-honestly-though" / "profile.toml"
    assert "tag_search = true" in profile.read_text(encoding="utf-8")
    assert save(desk, [], [], tag_search=False)["tagSearch"] is False
    assert desk.settings(DECK)["tagSearch"] is False


def test_the_settings_dialog_has_a_labelled_tag_search_switch_saved_with_the_rest() -> None:
    dialog = page_html().split('<dialog id="settings-dialog"', 1)[1].split("</dialog>", 1)[0]
    switch = re.search(r'<input[^>]*id="settings-tag-search"[^>]*>', dialog)
    assert switch is not None
    assert 'type="checkbox"' in switch.group(0)
    assert re.search(
        r'<label[^>]*>[^<]*<input[^>]*id="settings-tag-search"[^>]*>\s*Search decks by tag', dialog
    )
    script = (WEB / "settings.js").read_text(encoding="utf-8")
    assert "tagSearch: answer.tagSearch" in script
    assert '$("settings-tag-search").checked = s.tagSearch;' in script
    assert "settingsState.tagSearch = event.target.checked;" in script
    assert 'api("save_settings", s.deck, s.adjacent, s.ignore, s.tagSearch, s.grace)' in script


def profile_file(settings: DeskSettings) -> Path:
    return settings.workspace / "10004639-honestly-though" / "profile.toml"


def test_the_settings_list_the_grace_cards_and_a_save_returns_the_new_ones(
    desk: Desk, settings: DeskSettings
) -> None:
    before = desk.settings(DECK)
    assert (before["grace"], before["maxGrace"]) == ([], 10)
    saved = save(desk, [], [], tag_search=False, grace=[("Arcane Signet", 4), ("Mind Stone", 2.5)])
    expected = [{"name": "Arcane Signet", "factor": 4.0}, {"name": "Mind Stone", "factor": 2.5}]
    assert saved["grace"] == expected
    assert desk.settings(DECK)["grace"] == expected
    text = profile_file(settings).read_text(encoding="utf-8")
    assert '"Arcane Signet" = 4\n"Mind Stone" = 2.5\n' in text
    assert '# "Basalt Monolith" = 3' in text
    changed = save(desk, [], [], tag_search=False, grace=[("Mind Stone", 6)])
    assert changed["grace"] == [{"name": "Mind Stone", "factor": 6.0}]
    assert save(desk, [], [], tag_search=False)["grace"] == []


def test_a_grace_card_the_card_data_does_not_know_is_refused_and_the_file_stays(
    desk: Desk, settings: DeskSettings
) -> None:
    save(desk, [], [], tag_search=False, grace=[("Arcane Signet", 4)])
    before = profile_file(settings).read_bytes()
    refused = save(desk, [], [], tag_search=True, grace=[("Arcane Signet", 4), ("Nonesuch Relic", 3)])
    assert refused == {"error": "No card named 'Nonesuch Relic' (check the spelling)"}
    assert profile_file(settings).read_bytes() == before
    # The check ignores case and the spaces around a name.
    assert save(desk, [], [], tag_search=False, grace=[("  mind STONE ", 3)])["grace"] == [
        {"name": "mind STONE", "factor": 3.0}
    ]


def test_a_grace_card_already_in_the_profile_stays_even_when_the_card_data_lacks_it(
    desk: Desk, settings: DeskSettings
) -> None:
    profile_file(settings).write_text('[grace]\n"Newest Relic" = 3\n', encoding="utf-8")
    saved = save(desk, [], [], tag_search=True, grace=[("Newest Relic", 5)])
    assert saved["grace"] == [{"name": "Newest Relic", "factor": 5.0}]
    assert saved["tagSearch"] is True


@pytest.mark.parametrize(
    ("grace", "message"),
    [
        pytest.param([("Arcane Signet", 1)], "grace.Arcane Signet must be a number over 1", id="one"),
        pytest.param([("Arcane Signet", 11)], "at most 10", id="over-the-cap"),
        pytest.param([("Arcane Signet", None)], "Arcane Signet: the factor must be a number", id="missing"),
        pytest.param([("Arcane Signet", "3")], "Arcane Signet: the factor must be a number", id="text"),
        pytest.param([("Arcane Signet", True)], "Arcane Signet: the factor must be a number", id="flag"),
        pytest.param([("Arcane Signet", 2), ("arcane signet", 3)], "listed twice", id="twice"),
        pytest.param([("Arcane Signet", 2), (" Arcane Signet", 3)], "listed twice", id="twice-trimmed"),
        pytest.param([("  ", 3)], "needs a name", id="blank"),
    ],
)
def test_a_bad_grace_factor_shows_as_the_panels_error_and_leaves_the_file_alone(
    desk: Desk, settings: DeskSettings, grace: list[tuple[str, object]], message: str
) -> None:
    save(desk, [], [], tag_search=False, grace=[("Mind Stone", 2)])
    before = profile_file(settings).read_bytes()
    refused = save(desk, [], [], tag_search=True, grace=grace)
    assert message in str(refused["error"])
    assert set(refused) == {"error"}
    assert profile_file(settings).read_bytes() == before


def test_a_grace_save_refreshes_the_decks_grace_line_and_shares_at_once(
    desk: Desk, settings: DeskSettings, fake: FakeArchidekt
) -> None:
    fake.edhtop16.pages[MAGDA] = [magda_page()]
    fetch_meta(settings, fake)
    plain = desk.deck(DECK)
    assert plain["grace"] == {"cards": [], "holding": 0, "lists": 6}

    def golem_share(view: Json) -> Json:
        grid = {c["name"]: c for g in items(view, "grid") for c in cast("list[Json]", g["cards"])}
        return cast("Json", grid["Battered Golem"]["share"])

    save(desk, [], [], tag_search=False, grace=[("Arcane Signet", 4)])
    graced = desk.deck(DECK)
    assert graced["grace"] == {"cards": [{"name": "Arcane Signet", "factor": 4.0}], "holding": 1, "lists": 6}
    assert golem_share(graced)["share"] == pytest.approx(4.75 / 6.85, abs=1e-4)
    # The same file size: the shares cache follows the file's time, not only its size.
    save(desk, [], [], tag_search=False, grace=[("Arcane Signet", 5)])
    heavier = desk.deck(DECK)
    assert heavier["grace"] == {"cards": [{"name": "Arcane Signet", "factor": 5.0}], "holding": 1, "lists": 6}
    assert golem_share(heavier)["share"] == pytest.approx(4.75 / 7.375, abs=1e-4)
    save(desk, [], [], tag_search=False)
    assert desk.deck(DECK)["grace"] == {"cards": [], "holding": 0, "lists": 6}
    assert golem_share(desk.deck(DECK))["share"] == golem_share(plain)["share"]


def test_the_settings_dialog_has_a_grace_section_with_a_name_a_factor_and_an_add_button() -> None:
    dialog = page_html().split('<dialog id="settings-dialog"', 1)[1].split("</dialog>", 1)[0]
    assert "Lists holding these cards count more in this deck's percentages and ideas." in dialog
    name = re.search(r'<input[^>]*id="settings-grace-name"[^>]*>', dialog)
    factor = re.search(r'<input[^>]*id="settings-grace-factor"[^>]*>', dialog)
    assert name is not None
    assert factor is not None
    assert 'aria-label="Card name"' in name.group(0)
    assert all(part in factor.group(0) for part in ('type="number"', 'value="3"', 'max="10"', "aria-label="))
    assert re.search(r'<button[^>]*id="settings-grace-add"[^>]*>Add card</button>', dialog)


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
    for card in ("High-Speed Hoverbike", "Tempestra, Dame of Games", "Splinter Twin", "Arcane Signet"):
        desk.propose(DECK, card)
    view = desk.deck(DECK)
    # Magda 6.79, Disruptor Flute 2.17, Battered Golem 0.35: average 3.10, within budget to 25% over it
    # (3.88), ceiling 25% over Magda. Splinter Twin (6.68) is over budget.
    assert view["budget"] == {"working": 310, "within": 388, "ceiling": 849}
    within = {str(card["name"]): card["withinBudget"] for card in items(view, "cards")}
    # The Hoverbike costs 0.14 and Tempestra 0.24.
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


def test_grace_cards_weight_the_decks_shares_and_the_view_counts_the_lists_holding_one(
    desk: Desk, settings: DeskSettings, fake: FakeArchidekt
) -> None:
    fake.edhtop16.pages[MAGDA] = [magda_page()]
    fetch_meta(settings, fake)
    assert desk.deck(DECK)["grace"] == {"cards": [], "holding": 0, "lists": 6}
    profile = settings.workspace / "10004639-honestly-though" / "profile.toml"
    # Only the sixth list, which weighs 0.525 of the others' 4.75, runs the Signet: x4 makes it 2.1 of 6.85.
    profile.write_text('[grace]\n"Arcane Signet" = 4\n', encoding="utf-8")
    view = desk.deck(DECK)
    assert view["grace"] == {"cards": [{"name": "Arcane Signet", "factor": 4.0}], "holding": 1, "lists": 6}
    grid = {c["name"]: c for g in items(view, "grid") for c in cast("list[Json]", g["cards"])}
    golem = cast("Json", grid["Battered Golem"]["share"])
    assert (golem["count"], golem["total"], golem["label"]) == (5, 6, "69%")
    assert golem["share"] == pytest.approx(4.75 / 6.85, abs=1e-4)
    stone = next(c for c in items(view, "cards") if c["name"] == "Mind Stone")
    assert cast("Json", stone["share"])["label"] == "69%"
    # Taking the grace card out of the profile brings the plain shares back at once.
    profile.write_text("version = 1\n", encoding="utf-8")
    again = desk.deck(DECK)
    assert again["grace"] == {"cards": [], "holding": 0, "lists": 6}
    assert [c["share"] for c in items(again, "cards") if c["name"] == "Mind Stone"] == [
        cast("Json", stone["share"])
        | {"share": pytest.approx(4.75 / 5.275, abs=1e-4), "label": "90%"}
        | {
            "tip": "meta 90% (5/6 lists)",
            "meta": {"share": pytest.approx(4.75 / 5.275, abs=1e-4), "count": 5, "total": 6},
        }
    ]


def test_the_meta_breakdown_ignores_the_decks_grace_cards(
    desk: Desk, settings: DeskSettings, fake: FakeArchidekt
) -> None:
    fake.edhtop16.pages[MAGDA] = [magda_page()]
    fetch_meta(settings, fake)
    before = desk.meta(DECK)
    assert before["report"] is not None
    profile = settings.workspace / "10004639-honestly-though" / "profile.toml"
    profile.write_text('[grace]\n"Arcane Signet" = 4\n"Mind Stone" = 9\n', encoding="utf-8")
    assert desk.meta(DECK) == before


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
    # What the page's uncut() picks up for the "No cut yet" panel: open, with nothing to replace.
    assert (tomb["name"], tomb["status"], tomb["origin"], tomb["targets"]) == (
        "Ancient Tomb",
        "open",
        "archidekt decks",
        [],
    )


def test_an_alike_or_tagged_idea_carries_its_pools_count_total_and_share(
    settings: DeskSettings, desk: Desk
) -> None:
    store = Workspace(settings.workspace)
    deck = store.load(DECK)
    folder = store.folder_for(DECK)
    assert deck is not None
    assert folder is not None
    (folder / "profile.toml").write_text(ALIKE, encoding="utf-8")
    alike = [peer(1, ["Arcane Signet"]), peer(2, ["Arcane Signet"]), peer(3, []), peer(4, [])]
    landfall = tag_pool("Landfall", ["Ancient Tomb"], ["Ancient Tomb"], ["Ancient Tomb"], [])
    with CardDb(settings.card_db) as db:
        compile_ideas(
            StoredDeck(folder, deck),
            meta_data([], []),
            db,
            now=lambda: NOW,
            adjacent=alike,
            tagged=[landfall],
        )
    desk.propose(DECK, "Lightning Bolt")
    cards = {c["name"]: c for c in items(desk.deck(DECK), "cards")}
    # The pool's own share, not the commander's lists': it is what the idea was chosen by (ADR 0028).
    assert cards["Arcane Signet"]["pool"] == {"count": 2, "total": 4, "share": 0.5}
    assert cards["Ancient Tomb"]["pool"] == {"count": 3, "total": 4, "share": 0.75}
    assert cards["Ancient Tomb"]["originTag"] == "Landfall"
    assert cards["Lightning Bolt"]["pool"] is None


def test_an_idea_card_without_stored_pool_numbers_sends_no_pool_whatever_its_reason_says(
    settings: DeskSettings, desk: Desk
) -> None:
    store = Workspace(settings.workspace)
    deck = store.load(DECK)
    folder = store.folder_for(DECK)
    assert deck is not None
    assert folder is not None
    with CardDb(settings.card_db) as db:
        service = BoardService(folder, deck, db, now=lambda: NOW)
        service.propose(
            "Arcane Signet", Actor.CMDR, reason="in 2 of 4 recent decks tagged Landfall", origin=Origin.TAGGED
        )
        service.propose("Ancient Tomb", Actor.CMDR, origin=Origin.ADJACENT, origin_pool=OriginPool(1, 3))
    cards = {c["name"]: c for c in items(desk.deck(DECK), "cards")}
    assert cards["Arcane Signet"]["pool"] is None
    assert cards["Ancient Tomb"]["pool"] == {"count": 1, "total": 3, "share": 0.3333}


def test_a_commit_whose_label_read_failed_says_so_in_its_answer(
    desk: Desk, fake: FakeArchidekt, monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(apply_module.time, "sleep", waits.append)
    fake.labels_status = 500
    desk.propose(DECK, "Arcane Signet")
    desk.decide(DECK, first_card_id(desk), "add", "Disruptor Flute", "")

    saved = desk.commit(DECK, reviewed(desk))

    assert saved["saved"] is True
    [warning] = names(saved, "warnings")
    assert warning.startswith("could not read your card labels (")
    assert warning.endswith("labelling in Archidekt's default colours")


def test_a_commit_that_read_the_labels_has_no_warnings(desk: Desk) -> None:
    desk.propose(DECK, "Arcane Signet")
    desk.decide(DECK, first_card_id(desk), "add", "Disruptor Flute", "")

    assert desk.commit(DECK, reviewed(desk))["warnings"] == []


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
    assert "clears" not in preview
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
    # The save leaves the Maybeboard cards on Archidekt as they were.
    assert {c["categories"][0] for c in fake.decks[DECK]["cards"]} >= {"Maybeboard"}
    assert [w["action"] for w in fake.card_writes()] == ["remove", "add"]


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


def deferred_desk(settings: DeskSettings, fake: FakeArchidekt) -> tuple[Desk, list[Callable[[], None]]]:
    """A desk whose sync waits in the returned list, as if still running until the test runs it."""
    pending: list[Callable[[], None]] = []
    desk = Desk(
        settings,
        client_factory=fake.client,
        token_store=MemoryTokenStore({"makkis": REFRESH_TOKEN}),
        now=lambda: NOW,
        pacer_factory=lambda _: Pacer(0),
        background=pending.append,
    )
    return desk, pending


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
    desk, pending = deferred_desk(settings, fake)
    desk.sync()
    assert len(pending) == 1
    assert call(desk) == {"error": f"Wait for the sync to finish, then {then}."}
    assert not fake.card_writes()
    assert not (settings.workspace / "10004639-honestly-though" / "board.json").exists()


def _propose(desk: Desk) -> Json:
    return desk.propose(DECK, "Arcane Signet")


def _decide(desk: Desk) -> Json:
    return desk.decide(DECK, "any", "reject", None, "")


def _remove(desk: Desk) -> Json:
    return desk.remove(DECK, "any")


def _save_settings(desk: Desk) -> Json:
    return save(desk, [["Tempestra, Dame of Games"]], ["Stax"], tag_search=True, grace=[("Arcane Signet", 3)])


@pytest.mark.parametrize(
    ("call", "then"),
    [
        (_propose, "propose the card"),
        (_decide, "decide"),
        (_remove, "remove the card"),
        (_save_settings, "save the settings"),
    ],
)
def test_board_edits_and_settings_wait_while_an_update_runs(
    settings: DeskSettings, fake: FakeArchidekt, call: Callable[[Desk], Json], then: str
) -> None:
    desk, pending = deferred_desk(settings, fake)
    folder = settings.workspace / "10004639-honestly-though"
    desk.sync()
    assert len(pending) == 1
    assert call(desk) == {"error": f"Wait for the sync to finish, then {then}."}
    assert not (folder / "board.json").exists()
    assert not (folder / "activity.jsonl").exists()
    # The sync writes the profile template for a deck without one (ADR 0039); the settings are not in it.
    profile = folder / "profile.toml"
    text = profile.read_text(encoding="utf-8") if profile.exists() else ""
    assert "Tempestra" not in text
    assert "Arcane Signet" not in text
    assert "\ntag_search = true" not in text


def probing[**P, R](real: Callable[P, R], probe: Callable[[], None]) -> Callable[P, R]:
    """`real`, run after `probe` has run at the point a write is under way."""

    def call(*args: P.args, **kwargs: P.kwargs) -> R:
        probe()
        return real(*args, **kwargs)

    return call


SYNC_REFUSAL = {"error": "Wait for the save to finish, then sync."}


def queued_save(desk: Desk) -> str:
    desk.propose(DECK, "Arcane Signet")
    desk.decide(DECK, first_card_id(desk), "add", None, "")
    return reviewed(desk)


def test_a_sync_asked_for_during_a_save_is_refused_and_works_once_the_save_ends(
    settings: DeskSettings, fake: FakeArchidekt, monkeypatch: pytest.MonkeyPatch
) -> None:
    desk, pending = deferred_desk(settings, fake)
    stamp = queued_save(desk)
    during: list[Json] = []
    monkeypatch.setattr(
        desk_module, "save_board", probing(desk_module.save_board, lambda: during.append(desk.sync()))
    )
    assert desk.commit(DECK, stamp)["saved"] is True
    assert during == [SYNC_REFUSAL]
    assert pending == []
    assert desk.sync()["running"] is True
    assert len(pending) == 1


def test_a_save_that_fails_still_lets_the_next_sync_start(
    settings: DeskSettings, fake: FakeArchidekt, monkeypatch: pytest.MonkeyPatch
) -> None:
    desk, pending = deferred_desk(settings, fake)
    stamp = queued_save(desk)

    def broken() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(desk_module, "save_board", probing(desk_module.save_board, broken))
    assert desk.commit(DECK, stamp) == {"error": "Something went wrong; the details are in the log."}
    assert desk.sync()["running"] is True
    assert len(pending) == 1


def test_a_sync_asked_for_during_a_label_change_is_refused(
    settings: DeskSettings, fake: FakeArchidekt, monkeypatch: pytest.MonkeyPatch
) -> None:
    desk, pending = deferred_desk(settings, fake)
    during: list[Json] = []
    monkeypatch.setattr(
        desk_module, "set_label", probing(desk_module.set_label, lambda: during.append(desk.sync()))
    )
    _set_label(desk)
    assert during == [{"error": "Wait for the label change to finish, then sync."}]
    assert pending == []


def test_a_sync_asked_for_while_the_swaps_are_added_is_refused(
    settings: DeskSettings, fake: FakeArchidekt, monkeypatch: pytest.MonkeyPatch
) -> None:
    desk, pending = deferred_desk(settings, fake)
    during: list[Json] = []
    monkeypatch.setattr(
        desk_module, "mana_json", probing(desk_module.mana_json, lambda: during.append(desk.sync()))
    )
    _mana_to_board(desk)
    assert during == [{"error": "Wait for the swaps to finish, then sync."}]
    assert pending == []


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
    assert {"keys", "stoppers", "untouched", "comboGroups"} <= set(fresh)
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


def test_a_sync_asked_for_during_a_board_edit_or_settings_save_is_refused(
    settings: DeskSettings, fake: FakeArchidekt, monkeypatch: pytest.MonkeyPatch
) -> None:
    desk, pending = deferred_desk(settings, fake)
    desk.propose(DECK, "Arcane Signet")
    card_id = first_card_id(desk)
    during: list[Json] = []

    def probe() -> None:
        during.append(desk.sync())

    board_service = desk_module.BoardService
    for method in ("propose", "decide", "remove"):
        monkeypatch.setattr(board_service, method, probing(getattr(board_service, method), probe))
    monkeypatch.setattr(desk_module, "save_settings", probing(desk_module.save_settings, probe))
    desk.propose(DECK, "Chaos Warp")
    desk.decide(DECK, card_id, "reject", None, "")
    desk.remove(DECK, card_id)
    _save_settings(desk)
    assert during == [
        {"error": "Wait for the proposal to finish, then sync."},
        {"error": "Wait for the decision to finish, then sync."},
        {"error": "Wait for the removal to finish, then sync."},
        {"error": "Wait for the settings save to finish, then sync."},
    ]
    assert pending == []


def view_scores(settings: DeskSettings) -> dict[int, float]:
    return ViewScores(settings.cache / "views.json", lambda: NOW).scores()


def test_opening_a_deck_adds_one_to_its_view_score_and_reloading_it_adds_nothing(
    desk: Desk, settings: DeskSettings
) -> None:
    assert desk.open_deck(DECK) == {}
    desk.deck(DECK)
    desk.deck(DECK)
    assert view_scores(settings) == {DECK: 1.0}
    desk.open_deck(DECK)
    assert view_scores(settings) == {DECK: 2.0}


def test_the_page_picks_no_deck_for_the_user() -> None:
    web = Path(str(resources.files("commander") / "web"))
    script = (web / "deck.js").read_text(encoding="utf-8")
    assert "openDeck(state.decks[0]" not in script
    assert "pick-deck" not in page_html()
    # Only a click opens a deck, and each opening is reported to the bridge as a view.
    assert script.count('api("open_deck", id)') == 1


def test_opening_a_deck_that_is_not_there_scores_nothing(desk: Desk, settings: DeskSettings) -> None:
    answer = desk.open_deck(999)
    assert "press Sync" in str(answer["error"])
    assert view_scores(settings) == {}


EXTRA_DECKS = (20001, 20002, 20003)


def with_extra_decks(settings: DeskSettings, fake: FakeArchidekt, raw_deck: Json) -> None:
    """Three more decks in the workspace, which sort after the fixture deck."""
    for deck_id in EXTRA_DECKS:
        deck = copy.deepcopy(raw_deck)
        deck["id"], deck["name"] = deck_id, f"Deck {deck_id}"
        fake.put(deck)
    home = settings.workspace.parent
    assert (
        run(["sync", "--no-cards", "--no-ideas", "--workspace", str(settings.workspace)], home, fake).code
        == 0
    )


def sync_with_stubs(
    desk: Desk,
    pending: list[Callable[[], None]],
    monkeypatch: pytest.MonkeyPatch,
    after_gather: Callable[[int], None] = lambda _: None,
) -> list[int]:
    """Run the sync the desk is holding back, with the network reads of the ideas phase replaced."""
    gathered = stub_meta(monkeypatch, after_gather)

    def download(*_: object, **__: object) -> CardRefresh:
        return DOWNLOADED

    monkeypatch.setattr(syncrun, "refresh_card_db", download)
    desk.sync()
    [start] = pending
    start()
    assert desk.sync_status()["failed"] is False
    return gathered


def test_a_sync_fetches_the_deck_open_in_the_app_first_then_the_rest_by_views(
    settings: DeskSettings, fake: FakeArchidekt, raw_deck: Json, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_extra_decks(settings, fake, raw_deck)
    desk, pending = deferred_desk(settings, fake)
    desk.open_deck(20003)
    desk.open_deck(20003)
    desk.open_deck(20002)
    # 20002 is open, so it goes first; 20003 has two views and the others none.
    assert sync_with_stubs(desk, pending, monkeypatch) == [20002, 20003, DECK, 20001]


def test_a_sync_without_an_open_deck_goes_by_views(
    settings: DeskSettings, fake: FakeArchidekt, raw_deck: Json, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_extra_decks(settings, fake, raw_deck)
    desk, pending = deferred_desk(settings, fake)
    ViewScores(settings.cache / "views.json", lambda: NOW).record_open(20001)
    assert sync_with_stubs(desk, pending, monkeypatch) == [20001, DECK, 20002, 20003]


def test_the_apps_sync_pulls_a_private_deck_and_a_later_signed_out_one_warns_and_keeps_it(
    settings: DeskSettings, fake: FakeArchidekt, raw_deck: Json, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_id = 27461808
    fake.put({**raw_deck, "id": private_id, "name": "Private test"})
    fake.private.add(private_id)
    desk, pending = deferred_desk(settings, fake)
    sync_with_stubs(desk, pending, monkeypatch)
    assert Workspace(settings.workspace).load(private_id) is not None
    assert desk.sync_status()["warnings"] == []

    signed_out = Desk(
        settings,
        client_factory=fake.client,
        token_store=MemoryTokenStore(),
        now=lambda: NOW,
        pacer_factory=lambda _: Pacer(0),
        background=pending.append,
    )
    pending.clear()
    signed_out.sync()
    [start] = pending
    start()

    assert signed_out.sync_status()["warnings"] == [
        "Not signed in to Archidekt: private and unlisted decks were not updated"
    ]
    assert Workspace(settings.workspace).load(private_id) is not None


def test_a_deck_opened_during_the_sync_is_fetched_after_the_one_in_progress(
    settings: DeskSettings, fake: FakeArchidekt, raw_deck: Json, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_extra_decks(settings, fake, raw_deck)
    desk, pending = deferred_desk(settings, fake)
    desk.open_deck(20001)

    def open_another_during_the_first_fetch(deck_id: int) -> None:
        if deck_id == 20001:
            desk.open_deck(20003)

    gathered = sync_with_stubs(desk, pending, monkeypatch, open_another_during_the_first_fetch)
    assert gathered == [20001, 20003, DECK, 20002]
    assert view_scores(settings) == {20001: 1.0, 20003: 1.0}


def test_the_sync_status_shows_each_deck_waiting_then_fetching_then_fetched_and_nothing_after(
    settings: DeskSettings, fake: FakeArchidekt, raw_deck: Json, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_extra_decks(settings, fake, raw_deck)
    desk, pending = deferred_desk(settings, fake)
    desk.open_deck(20002)
    seen: list[object] = []

    def snapshot(_: int) -> None:
        seen.append(desk.sync_status()["decks"])

    sync_with_stubs(desk, pending, monkeypatch, snapshot)
    first = str(DECK)
    assert seen == [
        {"20002": "fetching", first: "queued", "20001": "queued", "20003": "queued"},
        {"20002": "done", first: "fetching", "20001": "queued", "20003": "queued"},
        {"20002": "done", first: "done", "20001": "fetching", "20003": "queued"},
        {"20002": "done", first: "done", "20001": "done", "20003": "fetching"},
    ]
    assert desk.sync_status()["decks"] == {}


def test_the_share_pill_takes_the_colour_of_the_origin_the_idea_is_credited_to() -> None:
    css = (WEB / "app.css").read_text(encoding="utf-8")
    for key in ("meta", "decks", "adjacent", "tagged", "none"):
        assert f".origin-{key} {{ --origin: var(--origin-{key}); }}" in css
    pill = css.split(".share-pill {", 1)[1].split("}", 1)[0]
    assert "background: var(--origin);" in pill
    assert "color: var(--on-origin);" in pill


def hex_of(css: str, token: str) -> str:
    found = re.search(rf"{re.escape(token)}:\s*(#[0-9a-fA-F]{{6}})\s*;", css)
    assert found is not None, token
    return found.group(1)


def luminance(colour: str) -> float:
    def channel(index: int) -> float:
        value = int(colour[1 + 2 * index : 3 + 2 * index], 16) / 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(0) + 0.7152 * channel(1) + 0.0722 * channel(2)


def contrast(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


ORIGIN_TOKENS = ("--origin-meta", "--origin-decks", "--origin-adjacent", "--origin-tagged", "--origin-none")


def test_every_origin_colour_has_light_and_dark_values_that_pass_contrast_on_the_tile_and_text() -> None:
    css = (WEB / "app.css").read_text(encoding="utf-8")
    light, dark = css.split("@media (prefers-color-scheme: dark)", 1)
    dark = dark.split("* { box-sizing", 1)[0]
    for theme in (light, dark):
        # The tile is on --surface, or on --selected while open.
        tiles = [hex_of(theme, "--surface"), hex_of(theme, "--selected")]
        ink = hex_of(theme, "--on-origin")
        for token in ORIGIN_TOKENS:
            fill = hex_of(theme, token)
            assert contrast(fill, ink) >= 4.5, token
            assert all(contrast(fill, tile) >= 3.0 for tile in tiles), token
    assert len({hex_of(light, t) for t in ORIGIN_TOKENS}) == len(ORIGIN_TOKENS)
    assert len({hex_of(dark, t) for t in ORIGIN_TOKENS}) == len(ORIGIN_TOKENS)


def test_no_cut_yet_is_a_side_panel_between_considering_and_meta_not_a_fold() -> None:
    html = page_html()
    assert '<details id="uncut"' not in html
    panel = html.split('<section id="uncut" class="panel" aria-labelledby="h-uncut" hidden>', 1)[1]
    panel = panel.split("</section>", 1)[0]
    toggle = 'id="h-uncut" class="panel-toggle" type="button" aria-expanded="true" aria-controls="uncut-body"'
    assert toggle in panel
    assert 'No cut yet <span class="count" id="count-uncut"></span>' in panel
    assert '<div id="uncut-body" class="panel-body">' in panel
    assert '<div id="list-uncut" class="cards" role="list"></div>' in panel
    assert html.index('id="h-considering"') < html.index('id="h-uncut"') < html.index('id="h-meta"')
