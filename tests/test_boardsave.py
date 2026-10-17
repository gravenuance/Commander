"""Previewing and saving a deck's board against a fake Archidekt whose deck moved on since the last sync."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from commander.apply import ApplyOutcome, NotYourDeckError
from commander.archidekt import parse_deck
from commander.archidekt_edit import ArchidektEditor
from commander.board import Actor, BoardError, BoardService, Status, read_board
from commander.boardsave import BoardSave, preview_board, save_board
from commander.carddb import CardDb
from commander.net import Fetcher
from commander.store import StoredDeck, Workspace
from conftest import ACCESS_TOKEN, DECK_ID, FakeArchidekt, Json, card_entry

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(card_db_file: Path) -> Iterator[CardDb]:
    with CardDb(card_db_file) as opened:
        yield opened


@pytest.fixture
def fake(raw_deck: Json) -> FakeArchidekt:
    archidekt = FakeArchidekt()
    archidekt.put(raw_deck)
    return archidekt


@pytest.fixture
def editor(fake: FakeArchidekt) -> Iterator[ArchidektEditor]:
    with fake.client() as client:
        yield ArchidektEditor(Fetcher(client, sleep=lambda _: None), ACCESS_TOKEN)


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path / "decks")


@pytest.fixture
def stored(workspace: Workspace, raw_deck: Json) -> StoredDeck:
    """The deck as the last sync saved it: Magda, Battered Golem, Disruptor Flute, six Mountains."""
    deck = parse_deck(raw_deck)
    return StoredDeck(workspace.save(deck), deck)


@pytest.fixture
def service(stored: StoredDeck, db: CardDb) -> BoardService:
    return BoardService(stored.folder, stored.deck, db, now=lambda: NOW)


@pytest.fixture
def preview(editor: ArchidektEditor, stored: StoredDeck, db: CardDb) -> Callable[..., ApplyOutcome]:
    def run(user: str = "MAKKIS") -> ApplyOutcome:
        return preview_board(editor, stored, db, user=user, now=lambda: NOW)

    return run


@pytest.fixture
def commit(
    editor: ArchidektEditor, workspace: Workspace, stored: StoredDeck, db: CardDb, service: BoardService
) -> Callable[[], BoardSave]:
    def run() -> BoardSave:
        return save_board(
            editor, workspace, stored, db, service, user="MAKKIS", actor=Actor.YOU, now=lambda: NOW
        )

    return run


def queue(service: BoardService, name: str, cut: str | None = None, copies: int = 1) -> str:
    card = service.propose(name, Actor.YOU, copies=copies)
    service.decide(card.id, Status.ADD, Actor.YOU, choice=cut)
    return card.id


def live_entry(fake: FakeArchidekt, name: str) -> Json | None:
    cards = fake.decks[DECK_ID]["cards"]
    return next((c for c in cards if c["card"]["oracleCard"]["name"] == name), None)


def live_quantity(fake: FakeArchidekt, name: str) -> int:
    entry = live_entry(fake, name)
    return 0 if entry is None else entry["quantity"]


def test_a_cut_is_planned_from_the_live_copies_not_the_synced_ones(
    fake: FakeArchidekt,
    service: BoardService,
    preview: Callable[..., ApplyOutcome],
    commit: Callable[[], BoardSave],
) -> None:
    queue(service, "Lightning Bolt", cut="Mountain")
    # Two Mountains were cut on Archidekt since the sync: four are left.
    mountains = live_entry(fake, "Mountain")
    assert mountains is not None
    mountains["quantity"] = 4

    assert [(c.name, c.before, c.after) for c in preview().resolution.changes] == [
        ("Mountain", 4, 3),
        ("Lightning Bolt", 0, 1),
    ]
    assert fake.writes == []

    saved = commit()

    assert (live_quantity(fake, "Mountain"), live_quantity(fake, "Lightning Bolt")) == (3, 1)
    assert saved.applied == ("Lightning Bolt",)


def test_a_card_already_added_on_archidekt_does_not_cut_its_swap_again(
    fake: FakeArchidekt, service: BoardService, commit: Callable[[], BoardSave]
) -> None:
    queue(service, "Lightning Bolt", cut="Battered Golem")
    queue(service, "Arcane Signet", cut="Disruptor Flute")
    # The user put Lightning Bolt in on Archidekt after the sync, so its swap is theirs to have made.
    fake.decks[DECK_ID]["cards"].append(card_entry(7_000_000_001, "Lightning Bolt", ["Burn"]))

    saved = commit()

    golem = live_entry(fake, "Battered Golem")
    flute = live_entry(fake, "Disruptor Flute")
    assert golem is not None
    assert flute is not None
    assert golem["categories"] == ["Combo Pieces", "Ramp"]
    assert flute["categories"][0] == "Maybeboard"
    assert (live_quantity(fake, "Lightning Bolt"), live_quantity(fake, "Arcane Signet")) == (1, 1)
    assert saved.applied == ("Lightning Bolt", "Arcane Signet")


def test_queued_cards_already_in_the_live_deck_leave_nothing_to_save(
    fake: FakeArchidekt, service: BoardService, preview: Callable[..., ApplyOutcome]
) -> None:
    queue(service, "Lightning Bolt", cut="Battered Golem")
    fake.decks[DECK_ID]["cards"].append(card_entry(7_000_000_001, "Lightning Bolt", ["Burn"]))

    with pytest.raises(BoardError, match="already in the deck on Archidekt"):
        preview()

    assert fake.writes == []


def test_another_users_deck_is_refused_before_anything_is_planned(
    stored: StoredDeck, service: BoardService, preview: Callable[..., ApplyOutcome]
) -> None:
    queue(service, "Lightning Bolt", cut="Battered Golem")

    with pytest.raises(NotYourDeckError):
        preview("SOMEONE")

    assert read_board(stored.folder).queued[0].name == "Lightning Bolt"
