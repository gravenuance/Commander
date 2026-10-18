"""`cmdr board` and `cmdr suggest --to-board` end to end, against the fake Archidekt."""

import json
import shutil
from pathlib import Path

import pytest

from commander import board as board_module
from commander.board import read_activity, read_board
from commander.carddb import CARD_DB_FILE
from conftest import DECK_ID, FakeArchidekt, Json, MemoryKeyring, card_entry
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
    assert (
        run(["sync", "--no-cards", "--no-ideas", "--workspace", str(tmp_path / "decks")], tmp_path, fake).code
        == 0
    )
    fake.requests.clear()
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
    assert added.out.startswith("Lightning Bolt is on the Magda board (considering, id ")
    assert board(home, fake, "add", "magda", "Arcane Signet").code == 0
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
        "considering",
        f"  [add, cut Battered Golem] Lightning Bolt  (by claude, id {board_id(home, 'Lightning Bolt')})",
        "      kills dorks",
        "      could replace Battered Golem: slow",
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


def test_the_removed_section_flags_are_usage_errors(
    home: Path, fake: FakeArchidekt, capsys: pytest.CaptureFixture[str]
) -> None:
    assert board(home, fake, "add", "magda", "Arcane Signet", "--suggested").code == 2
    assert "unrecognized arguments: --suggested" in capsys.readouterr().err
    assert board(home, fake, "add", "magda", "Arcane Signet").code == 0
    assert board(home, fake, "research", "magda", "Arcane Signet", "--move", "suggested").code == 2
    assert "unrecognized arguments: --move suggested" in capsys.readouterr().err


def test_a_commander_cannot_be_named_as_a_card_to_replace(home: Path, fake: FakeArchidekt) -> None:
    add = board(home, fake, "add", "magda", "Lightning Bolt", "--replace", "Magda, Brazen Outlaw=why not")
    assert (add.code, add.err) == (2, "cmdr: Magda, Brazen Outlaw is a commander, so it cannot make room\n")
    assert read_board(folder(home)).cards == ()


def test_log_since_reads_a_utc_time_to_the_minute_and_compares_it_as_a_time(
    home: Path, fake: FakeArchidekt
) -> None:
    board(home, fake, "add", "magda", "Lightning Bolt", "--by", "claude")

    def log_since(since: str) -> Run:
        return board(home, fake, "log", "--since", since)

    # The entry was made at 12:00:00, so a log after 12:00 has nothing and one after 11:59 has it.
    assert log_since("2026-10-15T12:00").out == "nothing changed since 2026-10-15T12:00:00+00:00\n"
    assert log_since("2026-10-15T11:59").out.count("claude proposed") == 1
    assert log_since("2026-10-15T11:59:59+00:00").out.count("claude proposed") == 1
    assert log_since("2026-10-15T11:59:59Z").out.count("claude proposed") == 1


@pytest.mark.parametrize(
    "since", ["yesterday", "2026-10-15 18:00", "2026-10-15", "2026-13-01T00:00", "18:00"]
)
def test_log_since_refuses_what_is_not_a_utc_time_like_the_help_shows(
    home: Path, fake: FakeArchidekt, since: str
) -> None:
    result = board(home, fake, "log", "--since", since)
    assert (result.code, result.out) == (2, "")
    assert result.err == f"cmdr: --since {since!r} is not a UTC time like 2026-10-15T18:00\n"


def test_log_shows_every_change_and_filters_by_time(home: Path, fake: FakeArchidekt) -> None:
    board(home, fake, "add", "magda", "Lightning Bolt", "--by", "claude")
    board(home, fake, "remove", "magda", "Lightning Bolt")
    result = board(home, fake, "log")
    assert result.out == (
        "2026-10-15T12:00:00+00:00  Magda  claude proposed (considering) Lightning Bolt\n"
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
        (DECK_ID, "Magda", "proposed (considering)"),
        (DECK_ID, "Magda", "removed"),
    ]


def test_plan_prints_the_queued_cards_for_apply(home: Path, fake: FakeArchidekt) -> None:
    empty = board(home, fake, "plan", "magda")
    assert (empty.code, empty.err) == (1, "cmdr: nothing is queued to add\n")
    board(home, fake, "add", "magda", "Arcane Signet", "--replace", "Disruptor Flute")
    board(home, fake, "decide", "magda", "Arcane Signet", "add", "--cut", "Disruptor Flute")
    result = board(home, fake, "plan", "magda")
    assert result.out == ('deck = "10004639"\nadd = ["Arcane Signet"]\nremove = ["Disruptor Flute"]\n')


def test_plan_leaves_out_cards_the_deck_on_archidekt_already_holds(home: Path, fake: FakeArchidekt) -> None:
    for card in ("Arcane Signet", "Lightning Bolt"):
        board(home, fake, "add", "magda", card)
        board(home, fake, "decide", "magda", card, "add")
    # Added on archidekt.com since the last sync, so the local copy of the deck is behind.
    fake.decks[DECK_ID]["cards"].append(card_entry(7_000_000_001, "Arcane Signet", ["Artifact"]))
    result = board(home, fake, "plan", "magda")
    assert result.code == 0, result.err
    assert result.out == 'deck = "10004639"\nadd = ["Lightning Bolt"]\n'


def test_plan_of_cards_all_in_the_deck_on_archidekt_is_a_failed_run(home: Path, fake: FakeArchidekt) -> None:
    board(home, fake, "add", "magda", "Arcane Signet")
    board(home, fake, "decide", "magda", "Arcane Signet", "add")
    fake.decks[DECK_ID]["cards"].append(card_entry(7_000_000_001, "Arcane Signet", ["Artifact"]))
    result = board(home, fake, "plan", "magda")
    assert result.code == 1
    assert result.err == "cmdr: every queued card is already in the deck on Archidekt\n"


def test_commit_previews_then_saves_and_marks_the_cards_applied(
    home: Path, fake: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    signed_in(memory_keyring)
    board(home, fake, "add", "magda", "Arcane Signet", "--replace", "Disruptor Flute")
    board(home, fake, "decide", "magda", "Arcane Signet", "add", "--cut", "Disruptor Flute")

    preview = board(home, fake, "commit", "magda")
    assert preview.code == 0, preview.err
    assert "  - Disruptor Flute\n" in preview.out
    assert "  + Arcane Signet (FRC 20, €0.30) in Artifact\n" in preview.out
    assert (
        "  Clears the Maybeboard: 2 cards\n    High-Speed Hoverbike, Tempestra, Dame of Games\n"
        in preview.out
    )
    assert "  bracket after these changes: 4+ (Ruthless)\n" in preview.out
    assert not fake.card_writes()

    saved = board(home, fake, "commit", "magda", "--save")
    assert saved.code == 0, saved.err
    assert f"saved 2 change(s); snapshot '{SNAPSHOT}' is on Archidekt\n" in saved.out
    assert "  cleared the Maybeboard: 2 cards\n" in saved.out
    cards = {c["name"]: c for c in saved_deck(home / "decks")["cards"]}
    # The cut card is gone, not moved to the Maybeboard, and the Maybeboard is empty.
    assert sorted(cards) == ["Arcane Signet", "Battered Golem", "Magda, Brazen Outlaw", "Mountain"]
    after = read_board(folder(home))
    signet = after.find("Arcane Signet")
    assert signet is not None
    assert signet.status.value == "applied"
    assert after.find("Disruptor Flute") is None
    assert [e.action for e in read_activity(folder(home))][-2:] == ["queued to add", "saved to Archidekt"]
    nothing = board(home, fake, "commit", "magda")
    assert (nothing.code, nothing.err) == (1, "cmdr: nothing is queued to add\n")


def test_a_damaged_board_file_is_a_failure_not_a_usage_error(home: Path, fake: FakeArchidekt) -> None:
    (folder(home) / "board.json").write_text("{not json", encoding="utf-8")
    result = board(home, fake, "show", "magda")
    assert result.code == 1
    assert "cannot read" in result.err


def test_a_busy_board_is_a_failure_not_a_usage_error(
    home: Path, fake: FakeArchidekt, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(board_module, "LOCK_TIMEOUT_SECONDS", 0.0)
    (folder(home) / board_module.LOCK_FILE).write_text("123", encoding="utf-8")
    result = board(home, fake, "add", "magda", "Arcane Signet")
    assert result.code == 1
    assert "the board is busy" in result.err


def test_a_card_that_cannot_go_on_the_board_is_still_a_usage_error(home: Path, fake: FakeArchidekt) -> None:
    result = board(home, fake, "add", "magda", "Not A Real Card")
    assert result.code == 2


def test_commit_marks_cards_already_in_the_deck_on_archidekt_applied_without_writing(
    home: Path, fake: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    signed_in(memory_keyring)
    board(home, fake, "add", "magda", "Arcane Signet", "--replace", "Disruptor Flute")
    board(home, fake, "decide", "magda", "Arcane Signet", "add", "--cut", "Disruptor Flute")
    fake.decks[DECK_ID]["cards"].append(card_entry(7_000_000_001, "Arcane Signet", ["Artifact"]))

    preview = board(home, fake, "commit", "magda")
    assert preview.code == 0, preview.err
    assert (
        "skipped, Arcane Signet: already in the deck on Archidekt; saving marks it applied\n" in preview.out
    )

    saved = board(home, fake, "commit", "magda", "--save")
    assert saved.code == 0, saved.err
    assert "marked applied, already in the deck: Arcane Signet\n" in saved.out
    assert not fake.card_writes()
    signet = read_board(folder(home)).find("Arcane Signet")
    assert signet is not None
    assert signet.status.value == "applied"


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
    signet = read_board(folder(home)).find("Arcane Signet")
    assert signet is not None
    assert signet.reason == "ramp is short: 2 of 10"
    hoverbike = read_board(folder(home)).find("High-Speed Hoverbike")
    assert hoverbike is not None
    assert hoverbike.reason == "fits the deck's themes: Vehicles"
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
