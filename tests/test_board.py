import json
import os
import re
import time
from collections.abc import Iterator
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
    BoardError,
    BoardService,
    ReplaceOption,
    Section,
    Status,
    board_plan,
    board_stamp,
    feed_ideas,
    import_aside,
    read_activity,
    read_board,
)
from commander.carddb import CardDb
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
    assert (card.name, card.section, card.source, card.status) == (
        "Lightning Bolt",
        Section.CONSIDERED,
        Actor.YOU,
        Status.OPEN,
    )
    assert read_board(tmp_path).cards == (card,)
    [entry] = read_activity(tmp_path)
    assert (entry.at, entry.actor, entry.action, entry.card, entry.detail) == (
        "2026-10-15T12:01:00+00:00",
        Actor.YOU,
        "proposed (considered)",
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
        service.propose("Lightning Bolt", Actor.CLAUDE, section=Section.SUGGESTED)


def test_research_records_the_reason_and_the_cards_to_replace(tmp_path: Path, service: BoardService) -> None:
    card = service.propose("Lightning Bolt", Actor.YOU)
    researched = service.research(
        card.id,
        Actor.CLAUDE,
        reason="kills early dorks",
        replace_options=[ReplaceOption("battered golem", "slow"), ReplaceOption("Battered Golem")],
        section=Section.SUGGESTED,
    )
    # Names come out as the deck spells them, and a repeat is dropped.
    assert researched.replace == (ReplaceOption("Battered Golem", "slow"),)
    assert (researched.reason, researched.section) == ("kills early dorks", Section.SUGGESTED)
    assert read_activity(tmp_path)[-1].detail == "reason, cards to replace, list"


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
        (RemoveCard("Battered Golem", maybeboard=True),),
    )
    assert read_activity(tmp_path)[-2].detail == "cut Battered Golem; better early"


def test_cutting_a_card_with_several_copies_takes_out_one_per_cut(
    deck: Deck, tmp_path: Path, service: BoardService
) -> None:
    # The fixture deck runs six Mountains; moving a cut to the Maybeboard would take all six.
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
    assert [e.action for e in read_activity(tmp_path)] == ["proposed (considered)", "turned down", "reopened"]


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
    (tmp_path / BOARD_FILE).write_text(json.dumps({"version": 2, "cards": []}), encoding="utf-8")
    with pytest.raises(BoardError, match="board version 2 is not supported"):
        read_board(tmp_path)


def test_the_stamp_changes_with_every_edit(tmp_path: Path, service: BoardService) -> None:
    before = board_stamp(tmp_path)
    service.propose("Lightning Bolt", Actor.YOU)
    assert board_stamp(tmp_path) != before


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


def test_sideboard_and_maybeboard_cards_come_in_as_considered_once(
    tmp_path: Path, deck: Deck, service: BoardService
) -> None:
    added = import_aside(service, deck, read_board(tmp_path))
    # The fixture keeps High-Speed Hoverbike and Tempestra on its Maybeboard.
    assert [(c.name, c.source, c.section, c.reason) for c in added] == [
        ("High-Speed Hoverbike", Actor.ARCHIDEKT, Section.CONSIDERED, "on the Maybeboard on Archidekt"),
        ("Tempestra, Dame of Games", Actor.ARCHIDEKT, Section.CONSIDERED, "on the Maybeboard on Archidekt"),
    ]
    assert import_aside(service, deck, read_board(tmp_path)) == []


def test_an_applied_card_cut_back_to_the_maybeboard_is_reopened(
    tmp_path: Path, deck: Deck, db: CardDb
) -> None:
    golem = next(c for c in deck.cards if c.name == "Battered Golem")
    before = replace(deck, cards=tuple(c for c in deck.cards if c is not golem))
    first = BoardService(tmp_path, before, db, now=Clock())
    card = first.propose("Battered Golem", Actor.YOU)
    first.decide(card.id, Status.ADD, Actor.YOU)
    first.mark_applied([card.id], Actor.YOU, "snap")
    aside = replace(golem, categories=("Maybeboard", "Ramp"), in_deck=False)
    after = replace(deck, cards=(*before.cards, aside))
    added = import_aside(BoardService(tmp_path, after, db, now=Clock()), after, read_board(tmp_path))
    [again] = [c for c in added if c.name == "Battered Golem"]
    assert (again.id, again.status, again.choice) == (card.id, Status.OPEN, None)


def test_turned_down_maybeboard_cards_are_dropped_by_the_next_save(
    tmp_path: Path, deck: Deck, service: BoardService
) -> None:
    import_aside(service, deck, read_board(tmp_path))
    service.decide("Tempestra, Dame of Games", Status.REJECTED, Actor.YOU)
    board = read_board(tmp_path)
    assert board.to_drop(deck) == ("Tempestra, Dame of Games",)
    assert board.pending(deck) == 1
    assert board_plan(deck, board) == Plan("10004639", (), (), ("Tempestra, Dame of Games",))
    # Once it is off the Maybeboard there is nothing left to drop.
    gone = replace(deck, cards=tuple(c for c in deck.cards if not c.name.startswith("Tempestra")))
    assert board_plan(gone, board) is None


def test_a_turned_down_card_keeps_its_meta_share_until_reopened(
    tmp_path: Path, service: BoardService
) -> None:
    card = service.propose("Lightning Bolt", Actor.CLAUDE)
    service.decide(card.id, Status.REJECTED, Actor.YOU, share=0.1)
    assert read_board(tmp_path).cards[0].rejected_share == pytest.approx(0.1)
    assert service.propose("Lightning Bolt", Actor.YOU).rejected_share is None
    # Only a turned-down card keeps one.
    assert service.decide(card.id, Status.ADD, Actor.YOU, share=0.5).rejected_share is None


def test_the_meta_brings_a_turned_down_card_back_once_its_share_rises_by_20_points(
    tmp_path: Path, service: BoardService
) -> None:
    card = service.propose("Lightning Bolt", Actor.CLAUDE)
    service.decide(card.id, Status.REJECTED, Actor.YOU, share=0.1)
    ideas = {"Lightning Bolt": "in 29 of 40 lists"}
    assert feed_ideas(service, ideas, [], read_board(tmp_path), shares={"lightning bolt": 0.29}) == []
    assert feed_ideas(service, ideas, [], read_board(tmp_path), shares=None) == []
    [back] = feed_ideas(service, ideas, [], read_board(tmp_path), shares={"lightning bolt": 0.31})
    assert (back.id, back.status, back.rejected_share) == (card.id, Status.OPEN, None)
    assert back.reason == "back: in 31% of meta lists, up from 10% when turned down; in 29 of 40 lists"


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
    assert read_activity(tmp_path)[-1].action == "meta share noted"
    assert (
        feed_ideas(service, {"Lightning Bolt": ""}, [], read_board(tmp_path), shares={"lightning bolt": 0.5})
        == []
    )


def test_a_meta_share_on_a_card_that_is_not_turned_down_is_refused(tmp_path: Path) -> None:
    card: Json = {
        "id": "a1",
        "name": "Lightning Bolt",
        "oracleId": "x",
        "section": "considered",
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
    (tmp_path / BOARD_FILE).write_text(json.dumps({"version": 1, "cards": [card]}), encoding="utf-8")
    with pytest.raises(BoardError, match="only a turned-down card keeps its meta share"):
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
