"""`cmdr board` and `cmdr suggest --to-board` end to end, against the fake Archidekt."""

import json
import shutil
from pathlib import Path

import pytest

from commander.board import read_activity, read_board
from commander.carddb import CARD_DB_FILE
from conftest import DECK_ID, FakeArchidekt, Json, MemoryKeyring
from test_cli_apply import SNAPSHOT, Run, run, saved_deck, signed_in

FOLDER = "10004639-honestly-though"


@pytest.fixture
def fake(raw_deck: Json) -> FakeArchidekt:
    archidekt = FakeArchidekt()
    archidekt.put(raw_deck)
    return archidekt


@pytest.fixture
def home(tmp_path: Path, card_db_file: Path, fake: FakeArchidekt) -> Path:
    cache = tmp_path / "cache"
    cache.mkdir()
    shutil.copy(card_db_file, cache / CARD_DB_FILE)
    assert run(["sync", "--no-cards", "--workspace", str(tmp_path / "decks")], tmp_path, fake).code == 0
    fake.requests.clear()
    # Start from an empty board: the sync put the fixture's two Maybeboard cards on it.
    for name in ("board.json", "activity.jsonl"):
        (tmp_path / "decks" / "10004639-honestly-though" / name).unlink()
    return tmp_path


def board(home: Path, fake: FakeArchidekt, *args: str) -> Run:
    action, rest = args[0], args[1:]
    return run(["board", action, *rest, "--workspace", str(home / "decks")], home, fake)


def folder(home: Path) -> Path:
    return home / "decks" / FOLDER


def board_id(home: Path, name: str) -> str:
    card = read_board(folder(home)).find(name)
    assert card is not None
    return card.id


def test_an_empty_board_says_how_to_fill_it(home: Path, fake: FakeArchidekt) -> None:
    result = board(home, fake, "show", "magda")
    assert result.code == 0
    assert result.out == (
        "Magda: Honestly, though.. (10004639)\n"
        "the board is empty; `cmdr board add 10004639 <card>` puts a card on it\n"
    )


def test_research_and_decisions_show_on_the_board(home: Path, fake: FakeArchidekt) -> None:
    added = board(home, fake, "add", "magda", "lightning bolt", "--reason", "kills dorks", "--by", "claude")
    assert added.code == 0, added.err
    assert added.out.startswith("Lightning Bolt is on the Magda board (considered, id ")
    assert board(home, fake, "add", "magda", "Arcane Signet", "--suggested").code == 0
    researched = board(
        home,
        fake,
        "research",
        "magda",
        "Lightning Bolt",
        "--replace",
        "Battered Golem=slow",
        "--by",
        "claude",
    )
    assert researched.code == 0, researched.err
    decided = board(home, fake, "decide", "magda", "Lightning Bolt", "add", "--cut", "Battered Golem")
    assert (decided.code, decided.out) == (0, "Lightning Bolt: add, cutting Battered Golem\n")
    assert board(home, fake, "decide", "magda", "Arcane Signet", "reject", "--note", "no room").code == 0

    shown = board(home, fake, "show", "magda").out
    assert shown.splitlines()[2:] == [
        "considered",
        f"  [add, cut Battered Golem] Lightning Bolt  (by claude, id {board_id(home, 'Lightning Bolt')})",
        "      kills dorks",
        "      could replace Battered Golem: slow",
        "",
        "suggested",
        f"  [no] Arcane Signet  (by you, id {board_id(home, 'Arcane Signet')})",
        "      note: no room",
        "",
        "queued: 1 to add, 1 to cut; `cmdr board commit 10004639` previews",
    ]


def test_board_refusals_are_usage_errors(home: Path, fake: FakeArchidekt) -> None:
    result = board(home, fake, "add", "magda", "Rhystic Study")
    assert result.code == 2
    assert result.err == "cmdr: Rhystic Study is outside the deck's colours (R)\n"
    cut = board(home, fake, "decide", "magda", "Lightning Bolt", "reject", "--cut", "Battered Golem")
    assert (cut.code, cut.err) == (2, "cmdr: --cut goes with add\n")
    bad = board(home, fake, "add", "magda", "Lightning Bolt", "--replace", "=why")
    assert bad.code == 2
    assert "names no card" in bad.err


def test_log_shows_every_change_and_filters_by_time(home: Path, fake: FakeArchidekt) -> None:
    board(home, fake, "add", "magda", "Lightning Bolt", "--by", "claude")
    board(home, fake, "remove", "magda", "Lightning Bolt")
    result = board(home, fake, "log")
    assert result.out == (
        "2026-10-15T12:00:00+00:00  Magda  claude proposed (considered) Lightning Bolt\n"
        "2026-10-15T12:00:00+00:00  Magda  you removed Lightning Bolt\n"
    )
    later = run(
        ["board", "log", "magda", "--since", "2026-10-15T12:00:00+00:00", "--workspace", str(home / "decks")],
        home,
        fake,
    )
    assert later.out == "nothing changed since 2026-10-15T12:00:00+00:00\n"
    rows = json.loads(run(["board", "log", "--json", "--workspace", str(home / "decks")], home, fake).out)
    assert [(r["deck"], r["deckName"], r["action"]) for r in rows] == [
        (DECK_ID, "Magda", "proposed (considered)"),
        (DECK_ID, "Magda", "removed"),
    ]


def test_plan_prints_the_queued_cards_for_apply(home: Path, fake: FakeArchidekt) -> None:
    assert board(home, fake, "plan", "magda").code == 2
    board(home, fake, "add", "magda", "Arcane Signet", "--replace", "Disruptor Flute")
    board(home, fake, "decide", "magda", "Arcane Signet", "add", "--cut", "Disruptor Flute")
    result = board(home, fake, "plan", "magda")
    assert result.out == (
        'deck = "10004639"\nadd = ["Arcane Signet"]\n'
        'remove = [{name = "Disruptor Flute", maybeboard = true}]\n'
    )


def test_commit_previews_then_saves_and_marks_the_cards_applied(
    home: Path, fake: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    signed_in(memory_keyring)
    board(home, fake, "add", "magda", "Arcane Signet", "--replace", "Disruptor Flute")
    board(home, fake, "decide", "magda", "Arcane Signet", "add", "--cut", "Disruptor Flute")

    preview = board(home, fake, "commit", "magda")
    assert preview.code == 0, preview.err
    assert "  - Disruptor Flute to the Maybeboard\n" in preview.out
    assert "  + Arcane Signet (FRC 20, €0.30) in Artifact\n" in preview.out
    assert not fake.card_writes()

    saved = board(home, fake, "commit", "magda", "--save")
    assert saved.code == 0, saved.err
    assert f"saved 2 change(s); snapshot '{SNAPSHOT}' is on Archidekt\n" in saved.out
    cards = {c["name"]: c for c in saved_deck(home / "decks")["cards"]}
    assert "Arcane Signet" in cards
    # The cut card waits on the Maybeboard, so the swap can be tried the other way round.
    assert cards["Disruptor Flute"]["categories"] == ["Maybeboard", "Stax"]
    after = read_board(folder(home))
    signet, flute = after.find("Arcane Signet"), after.find("Disruptor Flute")
    assert signet is not None
    assert flute is not None
    assert signet.status.value == "applied"
    # The card swapped out comes back as considered, to weigh the swap the other way round.
    assert (flute.status.value, flute.source.value, flute.section.value) == (
        "open",
        "archidekt",
        "considered",
    )
    assert [e.action for e in read_activity(folder(home))][-2:] == [
        "proposed (considered)",
        "saved to Archidekt",
    ]
    assert board(home, fake, "commit", "magda").code == 2


def test_commit_needs_a_login(home: Path, fake: FakeArchidekt) -> None:
    board(home, fake, "add", "magda", "Arcane Signet")
    board(home, fake, "decide", "magda", "Arcane Signet", "add")
    result = board(home, fake, "commit", "magda")
    assert result.code == 1
    assert "cmdr login" in result.err


def test_suggest_puts_its_ideas_on_the_board_once(home: Path, fake: FakeArchidekt) -> None:
    args = ["suggest", "magda", "--to-board", "--workspace", str(home / "decks")]
    first = run(args, home, fake)
    assert first.code == 0, first.err
    cards = read_board(folder(home)).cards
    assert cards
    assert {c.source.value for c in cards} == {"cmdr"}
    assert {c.section.value for c in cards} == {"suggested"}
    signet = read_board(folder(home)).find("Arcane Signet")
    assert signet is not None
    assert signet.reason == "ramp is short: 2 of 10"
    assert "put " in first.err
    assert "idea(s) on the Magda board" in first.err

    board(home, fake, "decide", "magda", "Arcane Signet", "reject")
    second = run(args, home, fake)
    assert "put 0 idea(s)" in second.err
    # A turned-down card is left out of suggestions from then on.
    shown = json.loads(run([*args[:2], "--json", "--workspace", str(home / "decks")], home, fake).out)
    ramp = next(r for r in shown[0]["roles"] if r["role"] == "ramp")
    assert "Arcane Signet" not in [i["name"] for i in ramp["ideas"]]


def test_to_board_needs_one_deck(home: Path, fake: FakeArchidekt) -> None:
    result = run(["suggest", "--to-board", "--workspace", str(home / "decks")], home, fake)
    assert (result.code, result.err) == (2, "cmdr: --to-board needs one deck\n")


def test_a_failed_save_is_logged_and_keeps_the_cards_queued(
    home: Path, fake: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    signed_in(memory_keyring)
    board(home, fake, "add", "magda", "Arcane Signet")
    board(home, fake, "decide", "magda", "Arcane Signet", "add")
    fake.write_status["modifyCards"] = 500
    result = board(home, fake, "commit", "magda", "--save")
    assert result.code == 1
    assert f"The snapshot '{SNAPSHOT}' is on Archidekt" in result.err
    [card] = read_board(folder(home)).cards
    assert card.status.value == "add"
    last = read_activity(folder(home))[-1]
    assert (last.action, last.card) == ("save to Archidekt failed", None)
    assert SNAPSHOT in last.detail
