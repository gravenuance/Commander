"""The MCP tools over the real `cmdr` commands, against the fake Archidekt (ADR 0069)."""

import json
import logging
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from commander import __version__
from commander.board import Status, read_activity, read_board
from commander.mcp.logfile import LOGGER_NAME, install_log
from commander.mcp.runner import Services
from commander.mcp.session import Session, ToolResult
from conftest import DECK_ID, FakeArchidekt, Json, MemoryKeyring
from mcp_setup import (
    FOLDER,
    Tools,
    document,
    environment,
    make_session,
    prepare_home,
    rows,
    snapshot,
    sync_decks,
    text,
)
from test_cli_apply import NOW, SNAPSHOT, saved_deck, signed_in

PLAN = """\
deck = "magda"
add = ["Arcane Signet"]
remove = ["Disruptor Flute", {name = "Mountain", keep = 5}]
"""


@pytest.fixture
def fake(raw_deck: Json) -> FakeArchidekt:
    golem = next(c for c in raw_deck["cards"] if c["card"]["oracleCard"]["name"] == "Battered Golem")
    golem["label"] = "Don't Have,#f47373"
    archidekt = FakeArchidekt()
    archidekt.put(raw_deck)
    return archidekt


@pytest.fixture
def home(tmp_path: Path, card_db_file: Path, fake: FakeArchidekt) -> Path:
    prepare_home(tmp_path, card_db_file)
    sync_decks(tmp_path, fake)
    return tmp_path


@pytest.fixture
def tools(home: Path, fake: FakeArchidekt) -> Tools:
    return Tools.over(make_session(home, fake))


def deck_changes(fake: FakeArchidekt) -> list[str]:
    """The paths of every write to a deck on Archidekt (signing in is not one)."""
    return [path for _, path, _, _ in fake.writes if path.startswith("/api/decks/")]


def queue_signet(tools: Tools) -> None:
    """Arcane Signet on the board, decided: add it and cut Disruptor Flute."""
    tools.local.board_add("magda", "Arcane Signet", "ramp", ["Disruptor Flute=slow"])
    tools.local.board_decide("magda", "Arcane Signet", "add", cut="Disruptor Flute")


def test_list_decks_answers_the_saved_decks(tools: Tools) -> None:
    answer = tools.read.list_decks()

    [deck] = rows(answer)
    assert (deck["id"], deck["name"], deck["commanders"], deck["bracket"], deck["cards"]) == (
        DECK_ID,
        "Sample, deck..",
        ["Magda, Brazen Outlaw"],
        5,
        9,
    )
    assert answer["warnings"] == []


def test_analyze_and_price_read_the_deck_by_name(tools: Tools) -> None:
    [analysis] = rows(tools.read.analyze("magda"))
    assert (analysis["cards"], analysis["lands"], analysis["curve"]) == (9, 6, [0, 0, 2, 1, 0, 0, 0, 0])
    assert [r["role"] for r in analysis["roles"]][:2] == ["ramp", "draw"]
    [priced] = rows(tools.read.price("magda"))
    assert (priced["totalCents"], priced["cheapestTotalCents"]) == (1087, 973)
    assert [d["id"] for d in rows(tools.read.analyze())] == [DECK_ID]


def test_card_and_tags_look_up_the_card_data(tools: Tools) -> None:
    [bolt] = rows(tools.read.card(["lightning bolt"]))
    assert (bolt["name"], bolt["text"], bolt["roles"], bolt["priceCents"]) == (
        "Lightning Bolt",
        "Lightning Bolt deals 3 damage to any target.",
        ["removal"],
        198,
    )
    assert [t["slug"] for t in rows(tools.read.tags("ramp"))] == ["ramp", "combat-ramp"]


def test_tokens_profile_board_log_and_toget_read_the_deck_folder(tools: Tools) -> None:
    tokens = document(tools.read.tokens("magda"))
    assert [(t["name"], t["makers"]) for t in tokens["tokens"]] == [("Treasure", ["Magda, Brazen Outlaw"])]
    profile = document(tools.read.profile("magda"))
    assert [d["name"] for d in profile["derived"]] == ["Dwarves", "Vehicles"]
    assert document(tools.read.board_show("magda"))["cards"] == []
    assert rows(tools.read.board_log()) == []
    [listed] = rows(tools.read.toget_list())
    assert [(c["name"], c["label"]) for c in listed["cards"]] == [("Battered Golem", "Don't Have")]


def test_bracket_and_suggest_use_commander_spellbook(tools: Tools) -> None:
    [report] = rows(tools.read.bracket("magda"))
    assert (report["estimate"]["tag"], report["estimate"]["minimumBracket"]) == ("Ruthless", 4)
    [ideas] = rows(tools.read.suggest("magda", count=3, max_price_eur=20.0, within_bracket=True))
    assert "Aggravated Assault" in [piece["name"] for piece in ideas["comboPieces"]]


def test_a_stale_card_database_is_a_warning_beside_the_answer(home: Path, fake: FakeArchidekt) -> None:
    later = Session(
        Services(
            env=environment(home),
            client_factory=fake.client,
            now=lambda: NOW + timedelta(days=400),
        )
    )

    answer = Tools.over(later).read.price("magda")

    assert [r["totalCents"] for r in rows(answer)] == [1087]
    [warning] = answer["warnings"]
    assert warning.startswith("cmdr: card data is from 20")
    assert warning.endswith("; run `cmdr sync` for fresh prices")


def test_a_command_that_fails_is_a_tool_error_with_the_cmdr_message(tools: Tools) -> None:
    with pytest.raises(ToolError) as caught:
        tools.read.analyze("nonesuch")

    assert str(caught.value) == "cmdr: no deck matches 'nonesuch'; `cmdr list` shows them all\n(exit code 2)"


def test_a_plan_with_problems_is_an_error_that_lists_them(
    tools: Tools, fake: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    signed_in(memory_keyring)
    with pytest.raises(ToolError) as caught:
        tools.remote.apply_save('deck = "magda"\nadd = ["Rhystic Study"]\n')

    message = str(caught.value)
    assert "its colours (U) are outside the deck's (R)" in message
    assert message.endswith("(exit code 2)")
    assert deck_changes(fake) == []


def test_names_that_cmdr_would_read_as_flags_never_reach_it(tools: Tools) -> None:
    for call in (
        lambda: tools.local.board_add("magda", "--help"),
        lambda: tools.read.analyze("-x"),
        lambda: tools.read.card(["Sol Ring", "--json"]),
        lambda: tools.read.board_log(since="--json"),
        lambda: tools.local.board_add("magda", "Sol Ring", "why\x00"),
        lambda: tools.read.suggest("magda", max_price_eur=float("inf")),
        lambda: tools.local.board_count("magda", "Mountain", -1),
    ):
        with pytest.raises(ToolError):
            call()


def test_a_reason_that_starts_with_a_dash_is_a_reason_not_a_flag(tools: Tools, home: Path) -> None:
    tools.local.board_add("magda", "Arcane Signet", "- protects the combo")

    card = read_board(home / "decks" / FOLDER).find("Arcane Signet")
    assert card is not None
    assert card.reason == "- protects the combo"


READ_CALLS: list[tuple[str, Callable[[Tools], ToolResult]]] = [
    ("list_decks", lambda t: t.read.list_decks()),
    ("analyze", lambda t: t.read.analyze("magda")),
    ("price", lambda t: t.read.price()),
    ("card", lambda t: t.read.card(["Arcane Signet"])),
    ("tags", lambda t: t.read.tags("ramp")),
    ("tokens", lambda t: t.read.tokens()),
    ("profile", lambda t: t.read.profile("magda")),
    ("board_show", lambda t: t.read.board_show("magda")),
    ("board_log", lambda t: t.read.board_log("magda", "2026-10-15T18:00")),
    ("toget_list", lambda t: t.read.toget_list("magda")),
    ("bracket", lambda t: t.read.bracket("magda", refresh=True)),
    ("suggest", lambda t: t.read.suggest("magda")),
    ("board_plan", lambda t: t.read.board_plan("magda")),
    ("board_commit_preview", lambda t: t.read.board_commit_preview("magda")),
    ("apply_preview", lambda t: t.read.apply_preview(PLAN)),
]


@pytest.mark.parametrize("call", [c for _, c in READ_CALLS], ids=[n for n, _ in READ_CALLS])
def test_a_read_tool_changes_no_deck_file_and_nothing_on_archidekt(
    call: Callable[[Tools], ToolResult],
    tools: Tools,
    home: Path,
    fake: FakeArchidekt,
    memory_keyring: MemoryKeyring,
) -> None:
    signed_in(memory_keyring)
    queue_signet(tools)
    before = snapshot(home / "decks")

    call(tools)

    assert snapshot(home / "decks") == before
    assert deck_changes(fake) == []


def test_board_tools_change_the_board_as_claude(tools: Tools, home: Path) -> None:
    folder = home / "decks" / FOLDER
    added = tools.local.board_add("magda", "Lightning Bolt", "kills dorks", ["Battered Golem=slow"])
    assert text(added).startswith("Lightning Bolt is on the Magda board (considering, id ")

    card = read_board(folder).find("Lightning Bolt")
    assert card is not None
    assert (card.source.value, card.reason, [(o.name, o.reason) for o in card.replace]) == (
        "claude",
        "kills dorks",
        [("Battered Golem", "slow")],
    )

    researched = tools.local.board_research("magda", "Lightning Bolt", "cheap", ["Disruptor Flute"])
    assert text(researched) == "updated Lightning Bolt\n"
    again = read_board(folder).find("Lightning Bolt")
    assert again is not None
    assert (again.reason, [o.name for o in again.replace]) == ("cheap", ["Disruptor Flute"])
    with pytest.raises(ToolError, match="replace entries or clear_replace, not both"):
        tools.local.board_research("magda", "Lightning Bolt", replace=["Mountain"], clear_replace=True)
    tools.local.board_research("magda", "Lightning Bolt", clear_replace=True)
    cleared = read_board(folder).find("Lightning Bolt")
    assert cleared is not None
    assert (cleared.reason, cleared.replace) == ("cheap", ())

    decided = tools.local.board_decide("magda", "Lightning Bolt", "add", cut="Battered Golem", note="room")
    assert text(decided) == "Lightning Bolt: add, cutting Battered Golem\n"
    assert read_board(folder).queued[0].status is Status.ADD

    counted = tools.local.board_count("magda", "Mountain", 4)
    assert text(counted) == "Mountain: 6 → 4\n"
    assert [(c.name, c.count) for c in read_board(folder).counts] == [("Mountain", 4)]

    removed = tools.local.board_remove("magda", "Lightning Bolt")
    assert text(removed) == "removed Lightning Bolt\n"
    assert read_board(folder).find("Lightning Bolt") is None
    log = read_activity(folder)
    assert {e.actor.value for e in log} == {"claude"}


def test_suggest_to_board_puts_the_ideas_on_the_board(tools: Tools, home: Path) -> None:
    answer = tools.local.suggest_to_board("magda")

    assert rows(answer)[0]["id"] == DECK_ID
    cards = read_board(home / "decks" / FOLDER).cards
    assert cards
    assert {c.source.value for c in cards} == {"cmdr"}
    assert answer["warnings"] == [f"put {len(cards)} idea(s) on the Magda board"]


def test_profile_init_writes_a_template_and_never_replaces_the_users_profile(
    tools: Tools, home: Path
) -> None:
    profile = home / "decks" / FOLDER / "profile.toml"
    profile.unlink()

    answer = tools.local.profile_init("magda")

    assert text(answer) == f"wrote {profile}; fill it in, then `cmdr profile {DECK_ID}` checks it\n"
    written = profile.read_bytes()
    with pytest.raises(ToolError, match="already exists; edit it instead"):
        tools.local.profile_init("magda")
    assert profile.read_bytes() == written


def test_board_commit_save_saves_the_queued_cards_to_archidekt(
    tools: Tools, home: Path, fake: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    signed_in(memory_keyring)
    queue_signet(tools)

    preview = text(tools.read.board_commit_preview("magda"))
    assert "  + Arcane Signet (FRC 20, €0.30) in Artifact\n" in preview
    assert deck_changes(fake) == []

    saved = text(tools.remote.board_commit_save("magda"))

    assert f"saved 2 change(s); snapshot '{SNAPSHOT}' is on Archidekt\n" in saved
    assert [p for p in deck_changes(fake) if p.endswith("/snapshots/")] == [
        f"/api/decks/{DECK_ID}/snapshots/"
    ]
    names = [c["name"] for c in saved_deck(home / "decks")["cards"]]
    assert "Arcane Signet" in names
    assert "Disruptor Flute" not in names
    assert len(fake.card_writes()) == 2


def test_apply_save_takes_a_snapshot_then_saves_what_the_preview_showed(
    tools: Tools, home: Path, fake: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    signed_in(memory_keyring)

    preview = document(tools.read.apply_preview(PLAN))
    assert (preview["saved"], [c["name"] for c in preview["changes"]]) == (
        False,
        ["Disruptor Flute", "Mountain", "Arcane Signet"],
    )
    assert deck_changes(fake) == []

    saved = document(tools.remote.apply_save(PLAN))

    assert (saved["saved"], saved["snapshot"], saved["mismatches"]) == (True, SNAPSHOT, [])
    assert len(fake.card_writes()) == 3
    names = [c["name"] for c in saved_deck(home / "decks")["cards"]]
    assert "Arcane Signet" in names
    assert "Disruptor Flute" not in names


def test_a_save_without_a_login_is_refused_and_writes_nothing(tools: Tools, fake: FakeArchidekt) -> None:
    queue_signet(tools)

    with pytest.raises(ToolError, match="cmdr login"):
        tools.remote.board_commit_save("magda")
    with pytest.raises(ToolError, match="cmdr login"):
        tools.remote.apply_save(PLAN)

    assert deck_changes(fake) == []


def test_toget_mark_labels_the_card_on_archidekt(
    tools: Tools, fake: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    signed_in(memory_keyring)

    ordered = tools.remote.toget_mark("magda", "battered golem", "ordered")
    assert text(ordered) == "Battered Golem: Don't Have -> Getting\n"
    arrived = tools.remote.toget_mark("magda", "Battered Golem", "arrived")
    assert text(arrived) == "Battered Golem: Getting -> no label\n"

    assert [e["modifications"]["label"] for e in fake.card_writes()] == ["Getting,#2ccce4", ",#656565"]
    assert rows(tools.read.toget_list("magda"))[0]["cards"] == []


def test_names_and_texts_from_the_decks_are_cleaned_of_control_characters(tools: Tools, home: Path) -> None:
    path = home / "decks" / FOLDER / "deck.json"
    deck = json.loads(path.read_text(encoding="utf-8"))
    deck["name"] = f"Sa\x1b[31mmple{chr(0x202E)} deck\x00"
    path.write_text(json.dumps(deck), encoding="utf-8")

    [listed] = rows(tools.read.list_decks())

    assert listed["name"] == "Sa[31mmple deck"


@pytest.fixture
def server_log(home: Path) -> Iterator[Path]:
    """The server's log file installed as `cmdr-mcp` does, and taken down again."""
    logger = logging.getLogger(LOGGER_NAME)
    handlers, propagate, level = list(logger.handlers), logger.propagate, logger.level
    path = install_log(home / "cache", {})
    assert path is not None
    assert path == home / "cache" / "logs" / "commander-mcp.log"
    yield path
    for handler in logger.handlers:
        if handler not in handlers:
            handler.close()
    logger.handlers[:] = handlers
    logger.propagate, logger.level = propagate, level


def test_the_servers_log_and_the_root_logger_survive_the_commands_logging_setup(
    tools: Tools, server_log: Path
) -> None:
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    handlers, level = list(root.handlers), root.level
    try:
        tools.read.list_decks()
        with pytest.raises(ToolError):
            tools.read.analyze("nonesuch")
        # `cmdr` resets the root logger to one handler on its own stderr; the tools put it back.
        assert (root.handlers, root.level) == (handlers, level)
    finally:
        root.removeHandler(sentinel)

    for handler in logging.getLogger(LOGGER_NAME).handlers:
        handler.flush()
    lines = server_log.read_text(encoding="utf-8").splitlines()
    assert f"cmdr-mcp {__version__} (mcp " in lines[0]
    assert any("cmdr list --json finished with exit code 0 in " in line for line in lines)
    assert any(
        "WARNING" in line and "cmdr analyze --json nonesuch finished with exit code 2" in line
        for line in lines
    )
    assert any("cmdr analyze said: cmdr: no deck matches 'nonesuch'" in line for line in lines)
