"""Taking a card off a category outside the deck, such as the Maybeboard (ADR 0052): queued on the board like
a count change, listed by Review, and saved as a removal of exactly that entry."""

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from commander.apply import resolve_plan
from commander.archidekt import parse_deck
from commander.archidekt_edit import ArchidektEditor
from commander.board import (
    BOARD_FILE,
    Actor,
    Board,
    BoardError,
    BoardService,
    CountChange,
    board_plan,
    counts_met,
    read_activity,
    read_board,
)
from commander.boardsave import BoardSave, preview_board, save_board
from commander.carddb import CardDb
from commander.changetext import change_text
from commander.deckstats import deck_stats, stat_cards, stats_after
from commander.models import Deck
from commander.net import Fetcher
from commander.plan import Plan, PlanError, RemoveCard, parse_plan
from commander.store import StoredDeck, Workspace
from conftest import ACCESS_TOKEN, DECK_ID, FakeArchidekt, Json, card_entry, put_on_sideboard

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
STAMP = "2026-10-15T12:00:00+00:00"
HOVERBIKE = "High-Speed Hoverbike"
TEMPESTRA = "Tempestra, Dame of Games"
# The fixture's Maybeboard entries.
HOVERBIKE_ENTRY = 3096264032
TEMPESTRA_ENTRY = 3115861444


@pytest.fixture
def db(card_db_file: Path) -> Iterator[CardDb]:
    with CardDb(card_db_file) as opened:
        yield opened


@pytest.fixture
def raw(raw_deck: Json) -> Json:
    """The fixture deck, with a second Battered Golem on the Maybeboard."""
    raw_deck["cards"].append(card_entry(7_000_000_001, "Battered Golem", ["Maybeboard"]))
    return raw_deck


@pytest.fixture
def deck(raw: Json) -> Deck:
    return parse_deck(raw)


@pytest.fixture
def service(tmp_path: Path, deck: Deck, db: CardDb) -> BoardService:
    return BoardService(tmp_path, deck, db, now=lambda: NOW)


def test_minus_queues_the_card_to_come_off_its_category_and_plus_undoes_it(
    tmp_path: Path, deck: Deck, service: BoardService
) -> None:
    change = service.step_count(HOVERBIKE, -1, Actor.YOU, category="maybeboard")

    assert change == CountChange(HOVERBIKE, 1, 0, Actor.YOU, STAMP, category="Maybeboard")
    board = read_board(tmp_path)
    assert board.counts == (change,)
    assert board.pending == 1
    assert board.count_of(HOVERBIKE) is None
    assert board.count_of(HOVERBIKE, "Maybeboard") == change
    last = read_activity(tmp_path)[-1]
    assert (last.action, last.card, last.detail) == ("queued to remove from Maybeboard", HOVERBIKE, "1 → 0")
    assert board_plan(deck, board) == Plan(str(DECK_ID), (), (RemoveCard(HOVERBIKE, category="Maybeboard"),))

    assert service.step_count(HOVERBIKE, 1, Actor.YOU, category="Maybeboard") is None
    assert read_board(tmp_path).counts == ()
    assert read_activity(tmp_path)[-1].action == "count change undone"


def test_a_card_outside_the_deck_can_only_come_off_and_only_from_a_category_that_holds_it(
    service: BoardService,
) -> None:
    with pytest.raises(BoardError, match=f"{HOVERBIKE} on Maybeboard can only be taken off"):
        service.step_count(HOVERBIKE, 1, Actor.YOU, category="Maybeboard")
    with pytest.raises(BoardError, match="'Disruptor Flute' is not on Maybeboard"):
        service.step_count("Disruptor Flute", -1, Actor.YOU, category="Maybeboard")
    # Stax is a category in the deck: its cards change through the deck's own + and -.
    with pytest.raises(BoardError, match="'Disruptor Flute' is not on Stax"):
        service.step_count("Disruptor Flute", -1, Actor.YOU, category="Stax")
    service.step_count(HOVERBIKE, -1, Actor.YOU, category="Maybeboard")
    with pytest.raises(BoardError, match=f"{HOVERBIKE} is already queued to come out"):
        service.step_count(HOVERBIKE, -1, Actor.YOU, category="Maybeboard")


def test_a_card_in_the_deck_and_on_the_maybeboard_keeps_its_two_changes_apart(
    tmp_path: Path, deck: Deck, db: CardDb, service: BoardService
) -> None:
    service.step_count("Battered Golem", -1, Actor.YOU, category="Maybeboard")
    # The deck's own copy is still free to change, and to cut in a swap.
    assert service.step_count("Battered Golem", -1, Actor.YOU) is not None
    board = read_board(tmp_path)
    assert [(c.name, c.category) for c in board.counts] == [
        ("Battered Golem", "Maybeboard"),
        ("Battered Golem", None),
    ]

    plan = board_plan(deck, board)
    assert plan == Plan(
        str(DECK_ID),
        (),
        (RemoveCard("Battered Golem", category="Maybeboard"), RemoveCard("Battered Golem")),
    )
    assert plan is not None
    resolution = resolve_plan(deck, plan, db, categories={"Maybeboard": False})
    assert resolution.problems == ()
    assert sorted((c.entry_id, c.in_deck, c.after) for c in resolution.changes) == [
        (2081750457, True, 0),
        (7_000_000_001, False, 0),
    ]
    # Only the deck's copy counts: 9 cards before, 8 after.
    assert (resolution.main_before, resolution.main_after) == (9, 8)


def test_review_says_which_category_the_card_comes_off(deck: Deck, db: CardDb) -> None:
    plan = Plan(str(DECK_ID), (), (RemoveCard(HOVERBIKE, category="Maybeboard"),))
    resolution = resolve_plan(deck, plan, db, categories={"Maybeboard": False})

    assert [change_text(c) for c in resolution.changes] == [f"- {HOVERBIKE} from the Maybeboard"]
    assert [(c.entry_id, c.in_deck) for c in resolution.changes] == [(HOVERBIKE_ENTRY, False)]
    assert resolution.main_after == resolution.main_before


def test_a_removal_from_a_category_with_no_copy_left_is_skipped(deck: Deck, db: CardDb) -> None:
    plan = Plan(str(DECK_ID), (), (RemoveCard("Disruptor Flute", category="Maybeboard"),))
    resolution = resolve_plan(deck, plan, db)

    assert (resolution.changes, resolution.skipped) == ((), ("remove Disruptor Flute: not on Maybeboard",))


def test_the_stats_after_ignore_a_card_coming_off_the_maybeboard(deck: Deck, db: CardDb) -> None:
    board = Board((), (CountChange("Battered Golem", 1, 0, Actor.YOU, STAMP, category="Maybeboard"),))
    main = db.resolve(deck.main_cards)

    assert stats_after(deck, main, board, db) == deck_stats(stat_cards(main))


def test_a_removal_is_met_once_the_category_holds_no_copy(raw: Json) -> None:
    board = Board((), (CountChange(HOVERBIKE, 1, 0, Actor.YOU, STAMP, category="Maybeboard"),))
    assert counts_met(parse_deck(raw), board) == ()

    raw["cards"] = [e for e in raw["cards"] if e["id"] != HOVERBIKE_ENTRY]
    gone = parse_deck(raw)
    assert counts_met(gone, board) == board.counts
    assert board_plan(gone, board) is None


def test_a_plan_file_removes_from_a_category_and_may_add_the_same_card_to_the_deck() -> None:
    plan = parse_plan(
        {
            "deck": "magda",
            "add": [HOVERBIKE],
            "remove": [
                {"name": HOVERBIKE, "category": "Maybeboard"},
                {"name": HOVERBIKE, "category": "Sideboard"},
            ],
        }
    )
    assert plan.remove == (
        RemoveCard(HOVERBIKE, category="Maybeboard"),
        RemoveCard(HOVERBIKE, category="Sideboard"),
    )
    with pytest.raises(PlanError, match="is listed twice in remove"):
        parse_plan({"deck": "magda", "remove": [{"name": HOVERBIKE, "category": "Maybeboard"}] * 2})


def test_the_board_is_written_as_version_4_with_the_category_and_a_version_3_board_is_backed_up(
    tmp_path: Path, service: BoardService
) -> None:
    original = json.dumps({"version": 3, "cards": [], "counts": []}) + "\n"
    (tmp_path / BOARD_FILE).write_text(original, encoding="utf-8")

    service.step_count(HOVERBIKE, -1, Actor.YOU, category="Maybeboard")

    saved = json.loads((tmp_path / BOARD_FILE).read_text(encoding="utf-8"))
    assert saved["version"] == 4
    assert saved["counts"] == [
        {"name": HOVERBIKE, "was": 1, "count": 0, "by": "you", "at": STAMP, "category": "Maybeboard"}
    ]
    assert (tmp_path / "board.v3.bak").read_text(encoding="utf-8") == original


def test_only_a_removal_names_a_category() -> None:
    with pytest.raises(ValueError, match="only comes off"):
        CountChange(HOVERBIKE, 2, 1, Actor.YOU, STAMP, category="Maybeboard")


def test_sync_clears_a_removal_the_deck_on_archidekt_already_made(
    tmp_path: Path, raw: Json, db: CardDb, service: BoardService
) -> None:
    service.step_count(HOVERBIKE, -1, Actor.YOU, category="Maybeboard")
    raw["cards"] = [e for e in raw["cards"] if e["id"] != HOVERBIKE_ENTRY]
    synced = BoardService(tmp_path, parse_deck(raw), db, now=lambda: NOW)

    cleared = synced.clear_met_counts()

    assert [(c.name, c.category) for c in cleared] == [(HOVERBIKE, "Maybeboard")]
    assert read_board(tmp_path).counts == ()


class LossyArchidekt(FakeArchidekt):
    """Accepts every edit but quietly leaves out those on the entries in `ignored`."""

    def __init__(self) -> None:
        super().__init__()
        self.ignored: set[int] = set()

    def _modify(self, deck: Json, edits: list[Json]) -> Json:
        return super()._modify(deck, [e for e in edits if e.get("deckRelationId") not in self.ignored])


@pytest.fixture
def fake(raw: Json) -> LossyArchidekt:
    archidekt = LossyArchidekt()
    archidekt.put(raw)
    return archidekt


@pytest.fixture
def stored(tmp_path: Path, raw: Json) -> StoredDeck:
    deck = parse_deck(raw)
    return StoredDeck(Workspace(tmp_path / "decks").save(deck), deck)


@pytest.fixture
def saving(stored: StoredDeck, db: CardDb) -> BoardService:
    return BoardService(stored.folder, stored.deck, db, now=lambda: NOW)


@pytest.fixture
def commit(
    fake: LossyArchidekt, tmp_path: Path, stored: StoredDeck, db: CardDb, saving: BoardService
) -> Iterator[Callable[[], BoardSave]]:
    with fake.client() as client:
        editor = ArchidektEditor(Fetcher(client, sleep=lambda _: None), ACCESS_TOKEN)

        def run() -> BoardSave:
            workspace = Workspace(tmp_path / "decks")
            return save_board(
                editor, workspace, stored, db, saving, user="TESTER", actor=Actor.YOU, now=lambda: NOW
            )

        yield run


def live_ids(fake: FakeArchidekt) -> set[int]:
    return {e["id"] for e in fake.decks[DECK_ID]["cards"]}


def test_a_save_removes_exactly_that_entry_and_leaves_the_rest_outside_the_deck_alone(
    fake: LossyArchidekt, saving: BoardService, stored: StoredDeck, commit: Callable[[], BoardSave]
) -> None:
    put_on_sideboard(fake.decks[DECK_ID], 7_000_000_002, "Chaos Warp")
    saving.step_count(HOVERBIKE, -1, Actor.YOU, category="Maybeboard")
    before = live_ids(fake)

    saved = commit()

    assert [(e["action"], e.get("deckRelationId")) for e in fake.card_writes()] == [
        ("remove", HOVERBIKE_ENTRY)
    ]
    assert live_ids(fake) == before - {HOVERBIKE_ENTRY}
    assert {TEMPESTRA_ENTRY, 7_000_000_001, 7_000_000_002} <= live_ids(fake)
    assert saved.applied == (HOVERBIKE,)
    assert read_board(stored.folder).counts == ()
    assert read_activity(stored.folder)[-1].action == "saved to Archidekt"


def test_a_removal_archidekt_did_not_make_stays_queued_while_the_decks_own_change_is_done(
    fake: LossyArchidekt, saving: BoardService, stored: StoredDeck, commit: Callable[[], BoardSave]
) -> None:
    saving.step_count("Battered Golem", -1, Actor.YOU, category="Maybeboard")
    saving.step_count("Mountain", -1, Actor.YOU)
    fake.ignored.add(7_000_000_001)

    commit()

    left = read_board(stored.folder).counts
    assert [(c.name, c.category) for c in left] == [("Battered Golem", "Maybeboard")]


def test_preview_lists_the_removal_against_the_live_deck(
    fake: LossyArchidekt, saving: BoardService, stored: StoredDeck, db: CardDb
) -> None:
    saving.step_count(HOVERBIKE, -1, Actor.YOU, category="Maybeboard")
    with fake.client() as client:
        editor = ArchidektEditor(Fetcher(client, sleep=lambda _: None), ACCESS_TOKEN)
        preview = preview_board(editor, stored, db, user="TESTER", now=lambda: NOW)

    assert [change_text(c) for c in preview.resolution.changes] == [f"- {HOVERBIKE} from the Maybeboard"]
    assert fake.writes == []
