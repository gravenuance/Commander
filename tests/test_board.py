import json
import os
import re
import time
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from commander import board as board_module
from commander.archidekt import parse_deck
from commander.board import (
    ACTIVITY_FILE,
    BOARD_FILE,
    LOCK_FILE,
    Actor,
    BoardCard,
    BoardError,
    BoardService,
    Origin,
    OriginPool,
    Relabel,
    ReplaceOption,
    Status,
    board_plan,
    board_stamp,
    feed_ideas,
    read_activity,
    read_board,
)
from commander.carddb import CardDb, ResolvedCard
from commander.models import Deck
from commander.plan import AddCard, Plan, RemoveCard
from conftest import Json

START = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


class Clock:
    """Each reading is one minute after the last, so every change gets its own timestamp."""

    def __init__(self) -> None:
        self.current = START

    def __call__(self) -> datetime:
        self.current += timedelta(minutes=1)
        return self.current


@pytest.fixture
def db(card_db_file: Path) -> Iterator[CardDb]:
    with CardDb(card_db_file) as opened:
        yield opened


@pytest.fixture
def deck(raw_deck: Json) -> Deck:
    """Magda (red), Battered Golem, Disruptor Flute and Mountains."""
    return parse_deck(raw_deck)


@pytest.fixture
def service(tmp_path: Path, deck: Deck, db: CardDb) -> BoardService:
    return BoardService(tmp_path, deck, db, now=Clock())


def test_a_proposed_card_is_saved_by_its_real_name_and_logged(tmp_path: Path, service: BoardService) -> None:
    card = service.propose("lightning bolt", Actor.YOU, reason="cheap interaction")
    assert (card.name, card.source, card.status) == (
        "Lightning Bolt",
        Actor.YOU,
        Status.OPEN,
    )
    assert read_board(tmp_path).cards == (card,)
    [entry] = read_activity(tmp_path)
    assert (entry.at, entry.actor, entry.action, entry.card, entry.detail) == (
        "2026-10-15T12:01:00+00:00",
        Actor.YOU,
        "proposed (considering)",
        "Lightning Bolt",
        "cheap interaction",
    )


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("Lightnig Bolt", "no card named 'Lightnig Bolt'"),
        ("  ", "the card name is empty"),
        ("Mana Crypt", "Mana Crypt is not legal in Commander"),
        ("Rhystic Study", "Rhystic Study is outside the deck's colours (R)"),
        ("Battered Golem", "Battered Golem is already in the deck"),
    ],
)
def test_cards_that_cannot_join_the_deck_are_refused(
    tmp_path: Path, service: BoardService, name: str, message: str
) -> None:
    with pytest.raises(BoardError, match=re.escape(message)):
        service.propose(name, Actor.YOU)
    assert not (tmp_path / BOARD_FILE).exists()
    assert not (tmp_path / ACTIVITY_FILE).exists()


def test_a_card_is_listed_once(service: BoardService) -> None:
    service.propose("Lightning Bolt", Actor.YOU)
    with pytest.raises(BoardError, match="Lightning Bolt is already on the board"):
        service.propose("Lightning Bolt", Actor.CLAUDE)


def test_the_decks_colour_identity_is_worked_out_once_for_many_cards(
    service: BoardService, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = board_module.deck_identity

    def counted(main: Sequence[ResolvedCard]) -> str | None:
        calls.append(len(main))
        return real(main)

    monkeypatch.setattr(board_module, "deck_identity", counted)
    for name in ("Lightning Bolt", "Arcane Signet", "Mountain"):
        service.propose(name, Actor.YOU)
    with pytest.raises(BoardError, match="outside the deck's colours"):
        service.propose("Rhystic Study", Actor.YOU)
    assert len(calls) == 1


def test_research_records_the_reason_and_the_cards_to_replace(tmp_path: Path, service: BoardService) -> None:
    card = service.propose("Lightning Bolt", Actor.YOU)
    researched = service.research(
        card.id,
        Actor.CLAUDE,
        reason="kills early dorks",
        replace_options=[ReplaceOption("battered golem", "slow"), ReplaceOption("Battered Golem")],
    )
    # Names come out as the deck spells them, and a repeat is dropped.
    assert researched.replace == (ReplaceOption("Battered Golem", "slow"),)
    assert researched.reason == "kills early dorks"
    assert read_activity(tmp_path)[-1].detail == "reason, cards to replace"


def test_a_card_to_replace_must_be_in_the_deck(service: BoardService) -> None:
    card = service.propose("Lightning Bolt", Actor.YOU)
    with pytest.raises(BoardError, match="'Sol Ring' is not in the deck"):
        service.research(card.id, Actor.CLAUDE, replace_options=[ReplaceOption("Sol Ring")])


def test_overlong_text_is_refused(service: BoardService) -> None:
    with pytest.raises(BoardError, match="the reason is longer than 4,000 characters"):
        service.propose("Lightning Bolt", Actor.YOU, reason="x" * 4001)


def test_queued_cards_make_a_plan_with_their_chosen_cuts(
    deck: Deck, tmp_path: Path, service: BoardService
) -> None:
    bolt = service.propose("Lightning Bolt", Actor.YOU, replace_options=[ReplaceOption("Battered Golem")])
    signet = service.propose("Arcane Signet", Actor.YOU)
    service.decide(bolt.id, Status.ADD, Actor.YOU, choice="battered golem", note="better early")
    service.decide(signet.name, Status.ADD, Actor.YOU)
    assert board_plan(deck, read_board(tmp_path)) == Plan(
        "10004639",
        (AddCard("Lightning Bolt"), AddCard("Arcane Signet")),
        (RemoveCard("Battered Golem"),),
    )
    assert read_activity(tmp_path)[-2].detail == "cut Battered Golem; better early"


def test_cutting_a_card_with_several_copies_takes_out_one_per_cut(
    deck: Deck, tmp_path: Path, service: BoardService
) -> None:
    # The fixture deck runs six Mountains; a cut that took them all would leave none.
    bolt = service.propose("Lightning Bolt", Actor.YOU)
    signet = service.propose("Arcane Signet", Actor.YOU)
    service.decide(bolt.id, Status.ADD, Actor.YOU, choice="Mountain")
    service.decide(signet.id, Status.ADD, Actor.YOU, choice="mountain")
    assert board_plan(deck, read_board(tmp_path)) == Plan(
        "10004639",
        (AddCard("Lightning Bolt"), AddCard("Arcane Signet")),
        (RemoveCard("Mountain", keep=4),),
    )


def test_nothing_queued_makes_no_plan(deck: Deck, tmp_path: Path, service: BoardService) -> None:
    service.propose("Lightning Bolt", Actor.YOU)
    assert board_plan(deck, read_board(tmp_path)) is None


def test_a_chosen_cut_must_be_in_the_deck(service: BoardService) -> None:
    card = service.propose("Lightning Bolt", Actor.YOU)
    with pytest.raises(BoardError, match="'Sol Ring' is not in the deck"):
        service.decide(card.id, Status.ADD, Actor.YOU, choice="Sol Ring")


def test_a_turned_down_card_is_reopened_when_proposed_again(tmp_path: Path, service: BoardService) -> None:
    card = service.propose("Lightning Bolt", Actor.CLAUDE, reason="first look")
    service.decide(card.id, Status.REJECTED, Actor.YOU, note="too narrow")
    assert read_board(tmp_path).rejected_names == ("Lightning Bolt",)
    again = service.propose("Lightning Bolt", Actor.YOU)
    assert (again.id, again.status, again.reason, again.note) == (card.id, Status.OPEN, "first look", "")
    assert [e.action for e in read_activity(tmp_path)] == [
        "proposed (considering)",
        "turned down",
        "reopened",
    ]


def test_dropping_the_chosen_cut_from_the_options_reopens_the_card(service: BoardService) -> None:
    card = service.propose("Lightning Bolt", Actor.YOU, replace_options=[ReplaceOption("Battered Golem")])
    service.decide(card.id, Status.ADD, Actor.YOU, choice="Battered Golem")
    updated = service.research(card.id, Actor.CLAUDE, replace_options=[ReplaceOption("Disruptor Flute")])
    assert (updated.status, updated.choice) == (Status.OPEN, None)


def test_applied_cards_are_final(tmp_path: Path, service: BoardService) -> None:
    card = service.propose("Lightning Bolt", Actor.YOU)
    service.decide(card.id, Status.ADD, Actor.YOU)
    service.mark_applied([card.id], Actor.YOU, "before cmdr apply")
    assert read_board(tmp_path).cards[0].status is Status.APPLIED
    assert read_activity(tmp_path)[-1].detail == "Lightning Bolt; snapshot 'before cmdr apply'"
    with pytest.raises(BoardError, match="already in the deck"):
        service.decide(card.id, Status.OPEN, Actor.YOU)
    with pytest.raises(BoardError, match="only once it is saved"):
        service.decide(card.id, Status.APPLIED, Actor.YOU)


def test_removing_a_card_drops_it_and_logs_it(tmp_path: Path, service: BoardService) -> None:
    card = service.propose("Lightning Bolt", Actor.YOU)
    service.remove("lightning bolt", Actor.YOU)
    assert read_board(tmp_path).cards == ()
    assert read_activity(tmp_path)[-1].action == "removed"
    with pytest.raises(BoardError, match="is not on the board"):
        service.remove(card.id, Actor.YOU)


def test_activity_since_a_time_and_damaged_lines_are_skipped(tmp_path: Path, service: BoardService) -> None:
    service.propose("Lightning Bolt", Actor.YOU)
    with (tmp_path / ACTIVITY_FILE).open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    service.propose("Arcane Signet", Actor.CLAUDE)
    assert [e.card for e in read_activity(tmp_path)] == ["Lightning Bolt", "Arcane Signet"]
    assert [e.card for e in read_activity(tmp_path, since="2026-10-15T12:01:00+00:00")] == ["Arcane Signet"]


def test_unreadable_or_newer_boards_are_refused(tmp_path: Path) -> None:
    (tmp_path / BOARD_FILE).write_text("{", encoding="utf-8")
    with pytest.raises(BoardError, match="cannot read"):
        read_board(tmp_path)
    (tmp_path / BOARD_FILE).write_text(json.dumps({"version": 3, "cards": []}), encoding="utf-8")
    with pytest.raises(BoardError, match="board version 3 is newer than this cmdr reads"):
        read_board(tmp_path)
    (tmp_path / BOARD_FILE).write_text(json.dumps({"version": 0, "cards": []}), encoding="utf-8")
    with pytest.raises(BoardError, match="board version 0 is not supported"):
        read_board(tmp_path)


def test_a_version_1_board_reads_with_every_card_considering_and_is_backed_up_before_its_next_save(
    tmp_path: Path, service: BoardService
) -> None:
    def old_card(card_id: str, name: str, section: str) -> Json:
        return {
            "id": card_id,
            "name": name,
            "oracleId": "x",
            "section": section,
            "source": "claude",
            "reason": "",
            "replace": [],
            "status": "open",
            "choice": None,
            "note": "",
            "addedAt": "t",
            "updatedAt": "t",
        }

    cards = [old_card("a1", "Lightning Bolt", "considered"), old_card("b2", "Arcane Signet", "suggested")]
    original = json.dumps({"version": 1, "cards": cards}, indent=2) + "\n"
    (tmp_path / BOARD_FILE).write_text(original, encoding="utf-8")
    assert [c.name for c in read_board(tmp_path).cards] == ["Lightning Bolt", "Arcane Signet"]
    assert (tmp_path / BOARD_FILE).read_text(encoding="utf-8") == original

    service.decide("Lightning Bolt", Status.REJECTED, Actor.YOU)

    saved = json.loads((tmp_path / BOARD_FILE).read_text(encoding="utf-8"))
    assert saved["version"] == 2
    assert all("section" not in card for card in saved["cards"])
    assert (tmp_path / "board.v1.bak").read_text(encoding="utf-8") == original
    # A board already at the current version is not backed up again.
    (tmp_path / "board.v1.bak").write_text("kept", encoding="utf-8")
    service.decide("Lightning Bolt", Status.OPEN, Actor.YOU)
    assert (tmp_path / "board.v1.bak").read_text(encoding="utf-8") == "kept"


def test_the_stamp_changes_with_every_edit(tmp_path: Path, service: BoardService) -> None:
    before = board_stamp(tmp_path)
    service.propose("Lightning Bolt", Actor.YOU)
    assert board_stamp(tmp_path) != before


def test_a_decision_whose_activity_line_cannot_be_written_is_reported_not_lost(
    tmp_path: Path, service: BoardService, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def disk_full(_folder: Path, _entry: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(board_module, "_append_activity", disk_full)
    with pytest.raises(BoardError, match=r"saved on the board, but its activity line could not be written"):
        service.propose("Lightning Bolt", Actor.YOU)
    assert [c.name for c in read_board(tmp_path).cards] == ["Lightning Bolt"]
    assert "disk full" in caplog.text
    assert not (tmp_path / LOCK_FILE).exists()


def test_a_held_lock_makes_the_board_busy(
    tmp_path: Path, service: BoardService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(board_module, "LOCK_TIMEOUT_SECONDS", 0.0)
    (tmp_path / LOCK_FILE).write_text("123", encoding="utf-8")
    with pytest.raises(BoardError, match="the board is busy"):
        service.propose("Lightning Bolt", Actor.YOU)
    assert not (tmp_path / BOARD_FILE).exists()


def test_a_stale_lock_is_cleared(tmp_path: Path, service: BoardService) -> None:
    lock = tmp_path / LOCK_FILE
    lock.write_text("123", encoding="utf-8")
    old = time.time() - 120
    os.utime(lock, (old, old))
    service.propose("Lightning Bolt", Actor.YOU)
    assert not lock.exists()
    assert len(read_board(tmp_path).cards) == 1


def test_a_takeover_never_removes_a_lock_made_fresh_since_it_was_seen_stale(
    tmp_path: Path, service: BoardService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Another process cleared the stale lock and took the board between our look and our unlink.
    monkeypatch.setattr(board_module, "LOCK_TIMEOUT_SECONDS", 0.0)
    lock = tmp_path / LOCK_FILE
    lock.write_text("123", encoding="utf-8")
    looks: list[Path] = []

    def stale_at_first_sight(path: Path) -> bool:
        looks.append(path)
        return len(looks) == 1 or time.time() - path.stat().st_mtime > board_module.STALE_LOCK_SECONDS

    monkeypatch.setattr(board_module, "_lock_is_stale", stale_at_first_sight)
    with pytest.raises(BoardError, match="the board is busy"):
        service.propose("Lightning Bolt", Actor.YOU)
    assert lock.read_text(encoding="utf-8") == "123"


def test_a_stale_lock_is_not_cleared_while_another_process_is_taking_it_over(
    tmp_path: Path, service: BoardService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(board_module, "LOCK_TIMEOUT_SECONDS", 0.0)
    lock = tmp_path / LOCK_FILE
    lock.write_text("123", encoding="utf-8")
    old = time.time() - 120
    os.utime(lock, (old, old))
    (tmp_path / f"{LOCK_FILE}.takeover").write_text("456", encoding="utf-8")
    with pytest.raises(BoardError, match="the board is busy"):
        service.propose("Lightning Bolt", Actor.YOU)
    assert lock.exists()


def test_a_takeover_left_by_a_crashed_process_is_cleared_too(tmp_path: Path, service: BoardService) -> None:
    old = time.time() - 120
    for name in (LOCK_FILE, f"{LOCK_FILE}.takeover"):
        (tmp_path / name).write_text("123", encoding="utf-8")
        os.utime(tmp_path / name, (old, old))
    service.propose("Lightning Bolt", Actor.YOU)
    assert sorted(p.name for p in tmp_path.glob(".board.lock*")) == []
    assert len(read_board(tmp_path).cards) == 1


def test_a_tagged_idea_carries_the_deck_tag_that_found_it_through_the_board_file(
    tmp_path: Path, service: BoardService
) -> None:
    card = service.propose("Lightning Bolt", Actor.CMDR, origin=Origin.TAGGED, origin_tag="Landfall")
    assert (card.origin, card.origin_tag) == (Origin.TAGGED, "Landfall")
    assert Origin.TAGGED.value == "tagged decks"
    assert read_board(tmp_path).cards == (card,)
    saved = json.loads((tmp_path / BOARD_FILE).read_text(encoding="utf-8"))
    assert (saved["cards"][0]["origin"], saved["cards"][0]["originTag"]) == ("tagged decks", "Landfall")


def test_a_card_without_an_origin_tag_saves_none_and_a_board_from_before_it_reads(
    tmp_path: Path, service: BoardService
) -> None:
    service.propose("Lightning Bolt", Actor.CMDR, origin=Origin.DECKS)
    path = tmp_path / BOARD_FILE
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["version"] == 2
    assert saved["cards"][0]["originTag"] is None
    del saved["cards"][0]["originTag"]
    path.write_text(json.dumps(saved), encoding="utf-8")
    [card] = read_board(tmp_path).cards
    assert (card.origin, card.origin_tag) == (Origin.DECKS, None)


def test_a_reopened_idea_takes_the_new_origin_and_tag(service: BoardService) -> None:
    card = service.propose("Lightning Bolt", Actor.CMDR, origin=Origin.TAGGED, origin_tag="Landfall")
    service.decide(card.id, Status.REJECTED, Actor.YOU)
    assert service.propose("Lightning Bolt", Actor.CMDR).origin_tag == "Landfall"
    service.decide(card.id, Status.REJECTED, Actor.YOU)
    again = service.propose("Lightning Bolt", Actor.CMDR, origin=Origin.TAGGED, origin_tag="Tokens")
    assert again.origin_tag == "Tokens"
    service.decide(card.id, Status.REJECTED, Actor.YOU)
    assert service.propose("Lightning Bolt", Actor.CMDR, origin=Origin.DECKS).origin_tag is None


def test_only_a_tagged_idea_may_name_a_tag() -> None:
    def card(origin: Origin | None, tag: str | None) -> BoardCard:
        return BoardCard(
            "a1",
            "Lightning Bolt",
            "x",
            Actor.CMDR,
            "",
            (),
            Status.OPEN,
            None,
            "",
            "t",
            "t",
            origin=origin,
            origin_tag=tag,
        )

    assert card(Origin.TAGGED, "Landfall").origin_tag == "Landfall"
    with pytest.raises(ValueError, match="only a tagged"):
        card(Origin.DECKS, "Landfall")
    with pytest.raises(ValueError, match="only a tagged"):
        card(None, "Landfall")


def test_a_turned_down_card_keeps_its_share_until_reopened(tmp_path: Path, service: BoardService) -> None:
    card = service.propose("Lightning Bolt", Actor.CLAUDE)
    service.decide(card.id, Status.REJECTED, Actor.YOU, share=0.1)
    assert read_board(tmp_path).cards[0].rejected_share == pytest.approx(0.1)
    assert service.propose("Lightning Bolt", Actor.YOU).rejected_share is None
    # Only a turned-down card keeps one.
    assert service.decide(card.id, Status.ADD, Actor.YOU, share=0.5).rejected_share is None


def test_only_the_user_reopening_a_card_marks_it_reconsidered_and_deciding_it_clears_the_mark(
    tmp_path: Path, service: BoardService
) -> None:
    card = service.propose("Lightning Bolt", Actor.CMDR)
    service.decide(card.id, Status.REJECTED, Actor.YOU)
    assert service.decide(card.id, Status.OPEN, Actor.CLAUDE).reconsidered is False

    service.decide(card.id, Status.REJECTED, Actor.YOU)
    assert service.decide(card.id, Status.OPEN, Actor.YOU).reconsidered is True
    assert read_board(tmp_path).cards[0].reconsidered is True

    assert service.decide(card.id, Status.ADD, Actor.YOU, choice="Battered Golem").reconsidered is False
    assert service.decide(card.id, Status.REJECTED, Actor.YOU).reconsidered is False


def test_undoing_a_queued_swap_is_not_reconsidering_the_card(service: BoardService) -> None:
    card = service.propose("Lightning Bolt", Actor.CMDR)
    service.decide(card.id, Status.ADD, Actor.YOU, choice="Battered Golem")

    undone = service.decide(card.id, Status.OPEN, Actor.YOU)

    assert (undone.status, undone.reconsidered) == (Status.OPEN, False)


def test_reopening_an_open_card_keeps_its_reconsidered_mark(service: BoardService) -> None:
    card = service.propose("Lightning Bolt", Actor.CMDR)
    service.decide(card.id, Status.REJECTED, Actor.YOU)
    service.decide(card.id, Status.OPEN, Actor.YOU)

    assert service.decide(card.id, Status.OPEN, Actor.YOU).reconsidered is True
    assert service.decide(card.id, Status.REJECTED, Actor.YOU).reconsidered is False


def test_undoing_a_queued_swap_does_not_relabel_an_old_pick_up(service: BoardService) -> None:
    card = service.propose("Lightning Bolt", Actor.ARCHIDEKT, reason="on the Maybeboard on Archidekt")
    service.decide(card.id, Status.ADD, Actor.YOU, choice="Battered Golem")

    undone = service.decide(
        card.id, Status.OPEN, Actor.YOU, relabel=Relabel(Origin.DECKS, "in 3 of 4 Archidekt decks")
    )

    assert (undone.source, undone.origin, undone.reason) == (
        Actor.ARCHIDEKT,
        None,
        "on the Maybeboard on Archidekt",
    )


def test_a_board_from_before_the_mark_reads_and_a_set_mark_round_trips(
    tmp_path: Path, service: BoardService
) -> None:
    card = service.propose("Lightning Bolt", Actor.CMDR)
    service.decide(card.id, Status.REJECTED, Actor.YOU)
    service.decide(card.id, Status.OPEN, Actor.YOU)
    path = tmp_path / BOARD_FILE
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["cards"][0]["reconsidered"] is True
    del raw["cards"][0]["reconsidered"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert read_board(tmp_path).cards[0].reconsidered is False


def test_reopening_an_old_pick_up_with_a_label_makes_it_a_compiled_idea_and_logs_the_reopen(
    tmp_path: Path, service: BoardService
) -> None:
    card = service.propose("Lightning Bolt", Actor.ARCHIDEKT, reason="on the Maybeboard on Archidekt")
    service.decide(card.id, Status.REJECTED, Actor.YOU)
    label = Relabel(Origin.DECKS, "in 3 of 4 most-viewed recent Archidekt decks")

    reopened = service.decide(card.id, Status.OPEN, Actor.YOU, relabel=label)

    assert (reopened.source, reopened.origin, reopened.reason) == (Actor.CMDR, Origin.DECKS, label.reason)
    assert (read_activity(tmp_path)[-1].action, read_activity(tmp_path)[-1].actor) == ("reopened", Actor.YOU)


def test_a_label_without_a_source_changes_only_the_reason(service: BoardService) -> None:
    card = service.propose("Lightning Bolt", Actor.ARCHIDEKT, reason="on the Maybeboard on Archidekt")
    service.decide(card.id, Status.REJECTED, Actor.YOU)

    reopened = service.decide(
        card.id, Status.OPEN, Actor.YOU, relabel=Relabel(None, "reconsidered; no list plays it")
    )

    assert (reopened.source, reopened.origin, reopened.reason) == (
        Actor.ARCHIDEKT,
        None,
        "reconsidered; no list plays it",
    )


def test_a_label_leaves_alike_and_tagged_ideas_and_other_peoples_cards_alone(service: BoardService) -> None:
    alike = service.propose("Lightning Bolt", Actor.CMDR, reason="in 2 of 4", origin=Origin.ADJACENT)
    mine = service.propose("Chaos Warp", Actor.YOU, reason="my idea")
    for card in (alike, mine):
        service.decide(card.id, Status.REJECTED, Actor.YOU)

    label = Relabel(Origin.META, "in 5 of 5 lists")
    kept = [service.decide(card.id, Status.OPEN, Actor.YOU, relabel=label) for card in (alike, mine)]

    assert [(c.source, c.origin, c.reason) for c in kept] == [
        (Actor.CMDR, Origin.ADJACENT, "in 2 of 4"),
        (Actor.YOU, None, "my idea"),
    ]


def test_a_turned_down_card_comes_back_once_its_share_rises_by_20_points(
    tmp_path: Path, service: BoardService
) -> None:
    card = service.propose("Lightning Bolt", Actor.CLAUDE)
    service.decide(card.id, Status.REJECTED, Actor.YOU, share=0.1)
    ideas = {"Lightning Bolt": "in 29 of 40 lists"}
    assert feed_ideas(service, ideas, [], read_board(tmp_path), shares={"lightning bolt": 0.29}) == []
    assert feed_ideas(service, ideas, [], read_board(tmp_path), shares=None) == []
    [back] = feed_ideas(service, ideas, [], read_board(tmp_path), shares={"lightning bolt": 0.31})
    assert (back.id, back.status, back.rejected_share) == (card.id, Status.OPEN, None)
    assert back.reason == "back: in 31% of the lists read, up from 10% when turned down; in 29 of 40 lists"


def test_a_card_turned_down_without_a_known_share_gets_todays_as_its_baseline(
    tmp_path: Path, service: BoardService
) -> None:
    card = service.propose("Lightning Bolt", Actor.CLAUDE)
    service.decide(card.id, Status.REJECTED, Actor.YOU)
    assert (
        feed_ideas(service, {"Lightning Bolt": ""}, [], read_board(tmp_path), shares={"lightning bolt": 0.4})
        == []
    )
    assert read_board(tmp_path).cards[0].rejected_share == pytest.approx(0.4)
    noted = read_activity(tmp_path)[-1]
    assert (noted.action, noted.detail) == ("share noted", "in 40% of the lists read")
    assert (
        feed_ideas(service, {"Lightning Bolt": ""}, [], read_board(tmp_path), shares={"lightning bolt": 0.5})
        == []
    )


def test_a_share_on_a_card_that_is_not_turned_down_is_refused(tmp_path: Path) -> None:
    card: Json = {
        "id": "a1",
        "name": "Lightning Bolt",
        "oracleId": "x",
        "source": "you",
        "reason": "",
        "replace": [],
        "status": "open",
        "choice": None,
        "note": "",
        "addedAt": "t",
        "updatedAt": "t",
        "rejectedShare": 0.2,
    }
    (tmp_path / BOARD_FILE).write_text(json.dumps({"version": 2, "cards": [card]}), encoding="utf-8")
    with pytest.raises(BoardError, match="only a turned-down card keeps its share"):
        read_board(tmp_path)


def test_a_card_adding_several_copies_cuts_as_many_of_its_choice(
    deck: Deck, tmp_path: Path, service: BoardService
) -> None:
    # A basic swap from the Mana tab: two more Mountains in place of two of the six.
    card = service.propose("Mountain", Actor.CMDR, replace_options=[ReplaceOption("Mountain")], copies=2)
    assert read_board(tmp_path).cards[0].copies == 2
    service.decide(card.id, Status.ADD, Actor.YOU, choice="Mountain")
    # Six held: the add asks for at least 6 + 2, the cut keeps 6 - 2 of the same card.
    plan = board_plan(deck, read_board(tmp_path))
    assert plan == Plan("10004639", (AddCard("Mountain", 8),), (RemoveCard("Mountain", keep=4),))


def test_a_card_adding_several_copies_cannot_cut_a_single_card(service: BoardService) -> None:
    card = service.propose("Lightning Bolt", Actor.CMDR, copies=2)
    with pytest.raises(BoardError, match="adds 2 copies; the deck has only 1 Battered Golem"):
        service.decide(card.id, Status.ADD, Actor.YOU, choice="Battered Golem")


def test_a_queued_card_the_deck_already_holds_neither_adds_nor_cuts(
    deck: Deck, tmp_path: Path, db: CardDb
) -> None:
    # Queued when the deck lacked Battered Golem; the deck as Archidekt has it now holds it.
    earlier = replace(deck, cards=tuple(c for c in deck.cards if c.name != "Battered Golem"))
    service = BoardService(tmp_path, earlier, db, now=Clock())
    golem = service.propose("Battered Golem", Actor.YOU)
    signet = service.propose("Arcane Signet", Actor.YOU)
    service.decide(golem.id, Status.ADD, Actor.YOU, choice="Disruptor Flute")
    service.decide(signet.id, Status.ADD, Actor.YOU, choice="Mountain")
    assert board_plan(deck, read_board(tmp_path)) == Plan(
        "10004639", (AddCard("Arcane Signet"),), (RemoveCard("Mountain", keep=5),)
    )


def test_a_commander_cannot_make_room(service: BoardService) -> None:
    card = service.propose("Lightning Bolt", Actor.YOU)
    with pytest.raises(BoardError, match="Magda, Brazen Outlaw is a commander, so it cannot make room"):
        service.research(card.id, Actor.CLAUDE, replace_options=[ReplaceOption("magda, brazen outlaw")])
    with pytest.raises(BoardError, match="Magda, Brazen Outlaw is a commander, so it cannot make room"):
        service.decide(card.id, Status.ADD, Actor.YOU, choice="Magda, Brazen Outlaw")


def test_an_ideas_pool_goes_through_the_board_file_as_origin_pool(
    tmp_path: Path, service: BoardService
) -> None:
    pool = OriginPool(7, 15)
    card = service.propose(
        "Lightning Bolt", Actor.CMDR, origin=Origin.TAGGED, origin_tag="Landfall", origin_pool=pool
    )
    assert read_board(tmp_path).cards == (card,)
    assert read_board(tmp_path).cards[0].origin_pool == OriginPool(7, 15)
    saved = json.loads((tmp_path / BOARD_FILE).read_text(encoding="utf-8"))
    assert saved["version"] == 2
    assert saved["cards"][0]["originPool"] == {"count": 7, "total": 15}


def test_a_card_without_a_pool_saves_none_and_a_board_from_before_it_reads(
    tmp_path: Path, service: BoardService
) -> None:
    service.propose("Lightning Bolt", Actor.CMDR, origin=Origin.ADJACENT)
    path = tmp_path / BOARD_FILE
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["cards"][0]["originPool"] is None
    del saved["cards"][0]["originPool"]
    path.write_text(json.dumps(saved), encoding="utf-8")
    [card] = read_board(tmp_path).cards
    assert (card.origin, card.origin_pool) == (Origin.ADJACENT, None)


def test_a_reopened_idea_takes_the_new_pool_and_keeps_the_old_one_when_given_none(
    service: BoardService,
) -> None:
    card = service.propose("Lightning Bolt", Actor.CMDR, origin=Origin.ADJACENT, origin_pool=OriginPool(2, 4))
    service.decide(card.id, Status.REJECTED, Actor.YOU)
    assert service.propose("Lightning Bolt", Actor.CMDR).origin_pool == OriginPool(2, 4)
    service.decide(card.id, Status.REJECTED, Actor.YOU)
    again = service.propose(
        "Lightning Bolt", Actor.CMDR, origin=Origin.ADJACENT, origin_pool=OriginPool(3, 5)
    )
    assert again.origin_pool == OriginPool(3, 5)
    service.decide(card.id, Status.REJECTED, Actor.YOU)
    assert service.propose("Lightning Bolt", Actor.CMDR, origin=Origin.DECKS).origin_pool is None


def test_a_pool_needs_an_alike_or_tagged_idea_and_a_count_within_a_non_empty_total() -> None:
    def card(origin: Origin | None, pool: OriginPool | None) -> BoardCard:
        return BoardCard(
            "a1", "Lightning Bolt", "x", Actor.CMDR, "", (), Status.OPEN, None, "", "t", "t",
            origin=origin, origin_pool=pool,
        )  # fmt: skip

    assert card(Origin.ADJACENT, OriginPool(0, 3)).origin_pool == OriginPool(0, 3)
    assert card(Origin.TAGGED, OriginPool(3, 3)).origin_pool == OriginPool(3, 3)
    with pytest.raises(ValueError, match="only an alike or tagged"):
        card(Origin.META, OriginPool(1, 3))
    with pytest.raises(ValueError, match="pool"):
        OriginPool(4, 3)
    with pytest.raises(ValueError, match="pool"):
        OriginPool(0, 0)
