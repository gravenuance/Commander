"""Previewing and saving a deck's board against a fake Archidekt whose deck moved on since the last sync."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from commander.apply import ApplyOutcome, NotYourDeckError
from commander.archidekt import parse_deck
from commander.archidekt_edit import ArchidektEditor
from commander.board import Actor, BoardError, BoardService, Status, read_activity, read_board
from commander.boardsave import BoardSave, preview_board, save_board
from commander.carddb import CardDb
from commander.net import Fetcher
from commander.store import StoredDeck, Workspace
from conftest import ACCESS_TOKEN, DECK_ID, FakeArchidekt, Json, card_entry, put_on_sideboard

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(card_db_file: Path) -> Iterator[CardDb]:
    with CardDb(card_db_file) as opened:
        yield opened


class LossyArchidekt(FakeArchidekt):
    """Accepts every edit but quietly leaves out those on the entries in `ignored`."""

    def __init__(self) -> None:
        super().__init__()
        self.ignored: set[int] = set()

    def _modify(self, deck: Json, edits: list[Json]) -> Json:
        return super()._modify(deck, [e for e in edits if e.get("deckRelationId") not in self.ignored])


@pytest.fixture
def fake(raw_deck: Json) -> LossyArchidekt:
    archidekt = LossyArchidekt()
    archidekt.put(raw_deck)
    return archidekt


@pytest.fixture
def editor(fake: LossyArchidekt) -> Iterator[ArchidektEditor]:
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
    assert golem is not None
    assert golem["categories"] == ["Combo Pieces", "Ramp"]
    assert live_entry(fake, "Disruptor Flute") is None
    assert (live_quantity(fake, "Lightning Bolt"), live_quantity(fake, "Arcane Signet")) == (1, 1)
    assert saved.applied == ("Lightning Bolt", "Arcane Signet")


ALREADY = "Lightning Bolt: already in the deck on Archidekt; saving marks it applied"


def test_a_preview_of_cards_all_already_in_the_live_deck_changes_nothing_and_says_so(
    fake: FakeArchidekt, service: BoardService, preview: Callable[..., ApplyOutcome]
) -> None:
    queue(service, "Lightning Bolt", cut="Battered Golem")
    fake.decks[DECK_ID]["cards"].append(card_entry(7_000_000_001, "Lightning Bolt", ["Burn"]))

    outcome = preview()

    assert (outcome.resolution.changes, outcome.resolution.problems) == ((), ())
    assert outcome.resolution.skipped == (ALREADY,)
    assert outcome.result is None
    assert fake.writes == []


def test_a_preview_names_the_queued_cards_already_there_beside_the_changes(
    fake: FakeArchidekt, service: BoardService, preview: Callable[..., ApplyOutcome]
) -> None:
    queue(service, "Lightning Bolt", cut="Battered Golem")
    queue(service, "Arcane Signet")
    fake.decks[DECK_ID]["cards"].append(card_entry(7_000_000_001, "Lightning Bolt", ["Burn"]))

    outcome = preview()

    assert [c.name for c in outcome.resolution.changes] == ["Arcane Signet"]
    assert outcome.resolution.skipped == (ALREADY,)


def test_saving_cards_all_already_in_the_live_deck_marks_them_applied_without_writing(
    fake: FakeArchidekt,
    service: BoardService,
    stored: StoredDeck,
    workspace: Workspace,
    commit: Callable[[], BoardSave],
) -> None:
    queue(service, "Lightning Bolt", cut="Battered Golem")
    fake.decks[DECK_ID]["cards"].append(card_entry(7_000_000_001, "Lightning Bolt", ["Burn"]))

    saved = commit()

    assert fake.writes == []
    assert saved.result is None
    assert saved.applied == ("Lightning Bolt",)
    assert saved.resolution.skipped == (ALREADY,)
    board = read_board(stored.folder)
    assert board.queued == ()
    assert board.cards[0].status is Status.APPLIED
    # Its chosen cut is not made.
    golem = live_entry(fake, "Battered Golem")
    assert golem is not None
    assert golem["categories"] == ["Combo Pieces", "Ramp"]
    activity = read_activity(stored.folder)[-1]
    assert (activity.action, activity.detail) == (
        "found in the deck on Archidekt",
        "Lightning Bolt",
    )
    local = workspace.load(DECK_ID)
    assert local is not None
    assert "Lightning Bolt" in {c.name for c in local.cards}


def live_categories(fake: FakeArchidekt) -> dict[str, list[str]]:
    return {c["card"]["oracleCard"]["name"]: c["categories"] for c in fake.decks[DECK_ID]["cards"]}


def test_a_cut_removes_the_card_and_adds_nothing_to_the_maybeboard(
    fake: FakeArchidekt, service: BoardService, commit: Callable[[], BoardSave]
) -> None:
    queue(service, "Lightning Bolt", cut="Battered Golem")
    golem = live_entry(fake, "Battered Golem")
    assert golem is not None

    commit()

    cut = [e for e in fake.card_writes() if e.get("deckRelationId") == golem["id"]]
    assert [e["action"] for e in cut] == ["remove"]
    assert "Battered Golem" not in live_categories(fake)


def test_a_save_leaves_the_maybeboard_and_the_sideboard_untouched(
    fake: FakeArchidekt, service: BoardService, commit: Callable[[], BoardSave]
) -> None:
    put_on_sideboard(fake.decks[DECK_ID], 7_000_000_001, "Chaos Warp")
    queue(service, "Lightning Bolt", cut="Battered Golem")
    aside = {"High-Speed Hoverbike", "Tempestra, Dame of Games", "Chaos Warp"}
    outside = {e["id"] for e in fake.decks[DECK_ID]["cards"] if e["card"]["oracleCard"]["name"] in aside}
    assert len(outside) == 3

    commit()

    # The modifyCards payload holds the cut and the add and nothing for the cards outside the deck.
    assert sorted(e["action"] for e in fake.card_writes()) == ["add", "remove"]
    assert outside.isdisjoint(e.get("deckRelationId") for e in fake.card_writes())
    assert sorted(live_categories(fake)) == [
        "Chaos Warp",
        "Disruptor Flute",
        "High-Speed Hoverbike",
        "Lightning Bolt",
        "Magda, Brazen Outlaw",
        "Mountain",
        "Tempestra, Dame of Games",
    ]


def test_a_save_whose_queued_card_is_in_the_deck_already_writes_nothing(
    fake: FakeArchidekt, service: BoardService, commit: Callable[[], BoardSave]
) -> None:
    queue(service, "Lightning Bolt", cut="Battered Golem")
    fake.decks[DECK_ID]["cards"].append(card_entry(7_000_000_001, "Lightning Bolt", ["Burn"]))

    commit()

    assert fake.writes == []
    assert {"High-Speed Hoverbike", "Tempestra, Dame of Games"} <= set(live_categories(fake))


def test_an_empty_board_is_still_an_error_to_preview_or_save(
    preview: Callable[..., ApplyOutcome], commit: Callable[[], BoardSave]
) -> None:
    with pytest.raises(BoardError, match="nothing is queued to change"):
        preview()
    with pytest.raises(BoardError, match="nothing is queued to change"):
        commit()


def test_another_users_deck_is_refused_before_anything_is_planned(
    stored: StoredDeck, service: BoardService, preview: Callable[..., ApplyOutcome]
) -> None:
    queue(service, "Lightning Bolt", cut="Battered Golem")

    with pytest.raises(NotYourDeckError):
        preview("SOMEONE")

    assert read_board(stored.folder).queued[0].name == "Lightning Bolt"


def test_a_basic_land_swap_trims_the_copies_when_the_land_is_in_two_entries(
    fake: FakeArchidekt, service: BoardService, commit: Callable[[], BoardSave]
) -> None:
    queue(service, "Lightning Bolt", cut="Mountain")
    # Archidekt shows four more Mountains in a second printing: ten in all.
    fake.decks[DECK_ID]["cards"].append(card_entry(7_000_000_002, "Mountain", ["Land"], 4))

    saved = commit()

    mountains = sorted(
        c["quantity"] for c in fake.decks[DECK_ID]["cards"] if c["card"]["oracleCard"]["name"] == "Mountain"
    )
    assert mountains == [3, 6]
    assert live_quantity(fake, "Lightning Bolt") == 1
    assert saved.result is not None
    assert saved.result.mismatches == ()


def test_a_swap_whose_cut_did_not_land_stays_queued(
    fake: LossyArchidekt, service: BoardService, stored: StoredDeck, commit: Callable[[], BoardSave]
) -> None:
    queue(service, "Lightning Bolt", cut="Battered Golem")
    queue(service, "Arcane Signet", cut="Disruptor Flute")
    golem = live_entry(fake, "Battered Golem")
    assert golem is not None
    fake.ignored.add(golem["id"])

    saved = commit()

    assert saved.result is not None
    assert saved.result.mismatches == ("Battered Golem: expected 0, Archidekt shows 1",)
    assert saved.applied == ("Arcane Signet",)
    board = read_board(stored.folder)
    statuses = {c.name: c.status for c in board.cards}
    assert statuses["Lightning Bolt"] is Status.ADD
    assert statuses["Arcane Signet"] is Status.APPLIED
