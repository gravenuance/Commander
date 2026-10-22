"""Count changes and adds without a cut on a deck's board (ADR 0043): the decklist's + and -, queued until a
save, and what a save sends for them."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from commander.answers import card_key
from commander.archidekt import parse_deck
from commander.archidekt_edit import ArchidektEditor
from commander.board import (
    BOARD_FILE,
    Actor,
    Board,
    BoardError,
    BoardService,
    CountChange,
    Status,
    board_plan,
    counts_met,
    held_keys,
    read_activity,
    read_board,
)
from commander.boardsave import save_board
from commander.carddb import CardDb
from commander.copylimit import copy_limit
from commander.deckstats import stats_after
from commander.deckview import decklist_json
from commander.models import Deck
from commander.net import Fetcher
from commander.plan import AddCard, Plan, RemoveCard
from commander.store import StoredDeck, Workspace
from conftest import ACCESS_TOKEN, DECK_ID, FakeArchidekt, Json, build_card_db, card_entry

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
STAMP = "2026-10-15T12:00:00+00:00"


def multiples_card(index: int, name: str, type_line: str, text: str) -> str:
    """A made-up bulk line for a red card with the given rules text; the real cards' texts are quoted."""
    return json.dumps(
        {
            "object": "card",
            "id": f"aaaaaaaa-0000-4000-8000-{index:012d}",
            "oracle_id": f"bbbbbbbb-0000-4000-8000-{index:012d}",
            "name": name,
            "lang": "en",
            "layout": "normal",
            "set": "tst",
            "collector_number": str(index),
            "digital": False,
            "cmc": 3.0,
            "mana_cost": "{2}{R}",
            "type_line": type_line,
            "color_identity": ["R"],
            "game_changer": False,
            "legalities": {"commander": "legal"},
            "prices": {"eur": "0.10", "eur_foil": None},
            "oracle_text": text,
            "keywords": [],
        }
    )


EXTRA_CARDS = [
    multiples_card(
        1,
        "Dragon's Approach",
        "Instant",
        "Dragon's Approach deals 3 damage to each opponent. You may exile Dragon's Approach and four other"
        " cards named Dragon's Approach from your graveyard. If you do, search your library for a Dragon"
        " creature card and put it onto the battlefield. Then shuffle.\n"
        "A deck can have any number of cards named Dragon's Approach.",
    ),
    multiples_card(
        2,
        "Seven Dwarves",
        "Creature — Dwarf",
        "Seven Dwarves gets +1/+1 for each other creature named Seven Dwarves you control.\n"
        "A deck can have up to seven cards named Seven Dwarves.",
    ),
]


@pytest.fixture(scope="module")
def db_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_card_db(tmp_path_factory.mktemp("multiples"), extra_cards=EXTRA_CARDS)


@pytest.fixture
def db(db_file: Path) -> Iterator[CardDb]:
    with CardDb(db_file) as opened:
        yield opened


@pytest.fixture
def raw(raw_deck: Json) -> Json:
    """The fixture deck (Magda, Battered Golem, Disruptor Flute, six Mountains, two Maybeboard cards) with one
    Dragon's Approach and six Seven Dwarves."""
    raw_deck["cards"].append(card_entry(7_000_000_001, "Dragon's Approach", ["Burn"]))
    raw_deck["cards"].append(card_entry(7_000_000_002, "Seven Dwarves", ["Dwarf"], quantity=6))
    return raw_deck


@pytest.fixture
def deck(raw: Json) -> Deck:
    return parse_deck(raw)


@pytest.fixture
def service(tmp_path: Path, deck: Deck, db: CardDb) -> BoardService:
    return BoardService(tmp_path, deck, db, now=lambda: NOW)


def test_the_copy_limit_comes_from_the_type_line_and_the_rules_text(db: CardDb) -> None:
    def limit(name: str) -> int | None:
        info = db.card_named(name)
        assert info is not None
        text = db.card_text(info.oracle_id)
        return copy_limit(info, text.text if text else "")

    assert limit("Mountain") is None
    assert limit("Dragon's Approach") is None
    assert limit("Seven Dwarves") == 7
    assert limit("Battered Golem") == 1


def test_only_cards_a_deck_may_run_one_of_count_as_held(deck: Deck, db: CardDb) -> None:
    # The others stay ideas, since the mana tab and the decklist adjust their counts (ADR 0046).
    held = held_keys(deck, db)

    assert card_key("Battered Golem") in held
    assert not {card_key(n) for n in ("Mountain", "Dragon's Approach", "Seven Dwarves")} & held


def test_plus_on_a_basic_land_queues_one_more_and_logs_it(tmp_path: Path, service: BoardService) -> None:
    change = service.step_count("mountain", 1, Actor.YOU)

    assert change == CountChange("Mountain", 6, 7, Actor.YOU, STAMP)
    assert read_board(tmp_path).counts == (change,)
    entry = read_activity(tmp_path)[-1]
    assert (entry.action, entry.card, entry.detail) == ("queued copies", "Mountain", "6 → 7")
    service.step_count("Mountain", 1, Actor.YOU)
    assert read_board(tmp_path).counts == (CountChange("Mountain", 6, 8, Actor.YOU, STAMP),)


def test_a_count_change_made_while_another_was_saving_stays_queued(
    tmp_path: Path, service: BoardService
) -> None:
    service.step_count("Mountain", 1, Actor.YOU)
    sent = read_board(tmp_path).counts  # what a save (the CLI's, say) read and sent: Mountain 6 -> 7
    service.step_count("Mountain", 1, Actor.YOU)  # the app asks for one more meanwhile: 6 -> 8

    service.mark_applied([], Actor.YOU, "snapshot", counts=sent)

    assert read_board(tmp_path).counts == (CountChange("Mountain", 6, 8, Actor.YOU, STAMP),)


def test_plus_on_a_card_any_number_of_which_is_allowed_queues_it(service: BoardService) -> None:
    assert service.step_count("Dragon's Approach", 1, Actor.YOU) == CountChange(
        "Dragon's Approach", 1, 2, Actor.YOU, STAMP
    )


def test_plus_is_refused_for_a_singleton_and_past_a_cards_own_limit(
    tmp_path: Path, service: BoardService
) -> None:
    with pytest.raises(BoardError, match="Commander allows one Battered Golem"):
        service.step_count("Battered Golem", 1, Actor.YOU)
    assert service.step_count("Seven Dwarves", 1, Actor.YOU) == CountChange(
        "Seven Dwarves", 6, 7, Actor.YOU, STAMP
    )
    with pytest.raises(BoardError, match="Commander allows 7 Seven Dwarves"):
        service.step_count("Seven Dwarves", 1, Actor.YOU)
    assert [c.count for c in read_board(tmp_path).counts] == [7]


def test_minus_on_a_single_card_queues_its_removal_and_plus_takes_it_back(
    tmp_path: Path, service: BoardService
) -> None:
    assert service.step_count("Battered Golem", -1, Actor.YOU) == CountChange(
        "Battered Golem", 1, 0, Actor.YOU, STAMP
    )
    assert read_activity(tmp_path)[-1].action == "queued to remove"
    with pytest.raises(BoardError, match="Battered Golem is already queued to come out"):
        service.step_count("Battered Golem", -1, Actor.YOU)

    # Back to the copies the deck holds: a singleton may come back, and the change is gone.
    assert service.step_count("Battered Golem", 1, Actor.YOU) is None
    assert read_board(tmp_path).counts == ()
    assert read_activity(tmp_path)[-1].action == "count change undone"


def test_counts_refuse_commanders_and_cards_not_in_the_deck(service: BoardService) -> None:
    with pytest.raises(BoardError, match="Magda, Brazen Outlaw is a commander"):
        service.step_count("Magda, Brazen Outlaw", -1, Actor.YOU)
    with pytest.raises(BoardError, match="'High-Speed Hoverbike' is not in the deck"):
        service.step_count("High-Speed Hoverbike", -1, Actor.YOU)


def test_a_count_change_and_a_queued_swap_never_touch_the_same_card(service: BoardService) -> None:
    bolt = service.propose("Lightning Bolt", Actor.YOU)
    service.decide(bolt.id, Status.ADD, Actor.YOU, choice="Battered Golem")
    with pytest.raises(BoardError, match="Battered Golem is in a queued change for Lightning Bolt"):
        service.step_count("Battered Golem", -1, Actor.YOU)

    service.step_count("Disruptor Flute", -1, Actor.YOU)
    signet = service.propose("Arcane Signet", Actor.YOU)
    with pytest.raises(BoardError, match="Disruptor Flute has a queued count change; undo it first"):
        service.decide(signet.id, Status.ADD, Actor.YOU, choice="Disruptor Flute")


def test_a_card_added_without_a_cut_keeps_its_category_until_undone(
    tmp_path: Path, service: BoardService
) -> None:
    signet = service.propose("Arcane Signet", Actor.YOU)
    queued = service.decide(signet.id, Status.ADD, Actor.YOU, category=" Ramp ")
    assert (queued.choice, queued.category) == (None, "Ramp")
    saved = json.loads((tmp_path / BOARD_FILE).read_text(encoding="utf-8"))
    assert saved["cards"][0]["category"] == "Ramp"

    reopened = service.decide(signet.id, Status.OPEN, Actor.YOU)
    assert reopened.category is None


@pytest.mark.parametrize("category", ["Maybeboard", "sideboard"])
def test_an_add_refuses_a_category_outside_the_deck(service: BoardService, category: str) -> None:
    signet = service.propose("Arcane Signet", Actor.YOU)
    with pytest.raises(BoardError, match="is outside the deck; name a category in the deck"):
        service.decide(signet.id, Status.ADD, Actor.YOU, category=category)


def test_an_add_refuses_a_category_of_the_users_own_outside_the_deck(
    tmp_path: Path, raw: Json, db: CardDb
) -> None:
    # The fixture's "TO ADD" is unticked as "included in deck" on Archidekt; a card filed there shows it.
    raw["cards"].append(card_entry(7_000_000_003, "Chaos Warp", ["TO ADD"]))
    service = BoardService(tmp_path, parse_deck(raw), db, now=lambda: NOW)
    signet = service.propose("Arcane Signet", Actor.YOU)
    with pytest.raises(BoardError, match="to add is outside the deck"):
        service.decide(signet.id, Status.ADD, Actor.YOU, category="to add")


def test_the_plan_carries_adds_without_a_cut_and_count_changes(
    tmp_path: Path, deck: Deck, service: BoardService
) -> None:
    signet = service.propose("Arcane Signet", Actor.YOU)
    service.decide(signet.id, Status.ADD, Actor.YOU, category="Ramp")
    service.set_count("Mountain", 8, Actor.YOU)
    service.step_count("Battered Golem", -1, Actor.YOU)
    service.set_count("Seven Dwarves", 4, Actor.YOU)

    assert board_plan(deck, read_board(tmp_path)) == Plan(
        str(DECK_ID),
        (AddCard("Arcane Signet", 1, category="Ramp"), AddCard("Mountain", 8)),
        (RemoveCard("Battered Golem", keep=0), RemoveCard("Seven Dwarves", keep=4)),
    )


def test_a_count_change_the_deck_already_meets_is_left_out_of_the_plan(
    tmp_path: Path, raw: Json, service: BoardService
) -> None:
    service.set_count("Mountain", 8, Actor.YOU)
    for entry in raw["cards"]:
        if entry["card"]["oracleCard"]["name"] == "Mountain":
            entry["quantity"] = 8
    assert board_plan(parse_deck(raw), read_board(tmp_path)) is None


def mountains_held(raw: Json, copies: int) -> Deck:
    """The fixture deck with `copies` Mountains; none leaves the deck without the card."""
    if copies == 0:
        raw["cards"] = [e for e in raw["cards"] if e["card"]["oracleCard"]["name"] != "Mountain"]
    for entry in raw["cards"]:
        if entry["card"]["oracleCard"]["name"] == "Mountain":
            entry["quantity"] = copies
    return parse_deck(raw)


@pytest.mark.parametrize(("was", "count", "held"), [(6, 4, 3), (6, 4, 0), (6, 4, 4), (6, 8, 9), (6, 8, 8)])
def test_a_decrease_is_at_most_its_count_and_an_increase_at_least_so_either_is_met_past_it(
    raw: Json, was: int, count: int, held: int
) -> None:
    # Cut on Archidekt meanwhile (held below a queued decrease) or raised past a queued increase.
    deck = mountains_held(raw, held)
    board = Board((), (CountChange("Mountain", was, count, Actor.YOU, STAMP),))

    assert board_plan(deck, board) is None
    assert counts_met(deck, board) == board.counts


def test_an_increase_of_a_card_the_deck_no_longer_holds_is_dropped_like_a_decrease(raw: Json) -> None:
    # Mountain 3 -> 5 queued, then the card was cut on Archidekt: re-adding it is a separate, explicit choice.
    deck = mountains_held(raw, 0)
    board = Board((), (CountChange("Mountain", 3, 5, Actor.YOU, STAMP),))

    assert board_plan(deck, board) is None
    assert counts_met(deck, board) == board.counts


@pytest.mark.parametrize(("count", "held", "plan"), [(4, 6, "cut"), (8, 6, "add"), (0, 6, "cut")])
def test_a_count_change_the_deck_has_not_reached_still_cuts_or_adds(
    raw: Json, count: int, held: int, plan: str
) -> None:
    deck = mountains_held(raw, held)
    board = Board((), (CountChange("Mountain", 6, count, Actor.YOU, STAMP),))

    expected = (
        Plan(str(DECK_ID), (), (RemoveCard("Mountain", keep=count),))
        if plan == "cut"
        else Plan(str(DECK_ID), (AddCard("Mountain", count),), ())
    )
    assert board_plan(deck, board) == expected
    assert counts_met(deck, board) == ()


def test_a_version_2_board_reads_without_count_changes_and_is_backed_up_on_its_next_save(
    tmp_path: Path, service: BoardService
) -> None:
    original = json.dumps({"version": 2, "cards": []}) + "\n"
    (tmp_path / BOARD_FILE).write_text(original, encoding="utf-8")
    assert read_board(tmp_path).counts == ()

    service.step_count("Mountain", 1, Actor.YOU)

    saved = json.loads((tmp_path / BOARD_FILE).read_text(encoding="utf-8"))
    assert saved["version"] == 4
    assert saved["counts"] == [
        {"name": "Mountain", "was": 6, "count": 7, "by": "you", "at": STAMP, "category": None}
    ]
    assert (tmp_path / "board.v2.bak").read_text(encoding="utf-8") == original


def split_mountains(raw: Json) -> Deck:
    """The fixture deck with its six Mountains (Don't Have) and four more as a second entry."""
    for entry in raw["cards"]:
        if entry["card"]["oracleCard"]["name"] == "Mountain":
            entry["label"] = "Don't Have,#f47373"
    raw["cards"].append(card_entry(7_000_000_003, "Mountain", ["Land"], quantity=4))
    return parse_deck(raw)


def test_a_card_held_as_two_entries_counts_all_its_copies_in_the_stats_after(raw: Json, db: CardDb) -> None:
    deck = split_mountains(raw)
    board = Board((), (CountChange("Battered Golem", 1, 0, Actor.YOU, STAMP),))

    # 20 cards: Magda, Golem, Flute, Dragon's Approach, six Seven Dwarves and ten Mountains; the Golem leaves.
    assert stats_after(deck, db.resolve(deck.main_cards), board, db).cards == 19


def test_a_cut_of_a_split_card_takes_the_last_entry_first_and_keeps_each_entrys_label(
    raw: Json, db: CardDb
) -> None:
    deck = split_mountains(raw)
    board = Board((), (CountChange("Mountain", 10, 4, Actor.YOU, STAMP),))

    after = stats_after(deck, db.resolve(deck.main_cards), board, db)

    # The four plain Mountains go, then two of the six Don't Have ones (0.12 each): 14 cards, 4 Don't Have.
    assert (after.cards, after.dont_have, after.dont_have_cents) == (14, 4, 48)


def test_plus_on_a_split_card_follows_the_decks_copies_in_all_not_the_entrys(raw: Json, db: CardDb) -> None:
    # Seven Dwarves as six and one: the limit of seven is met by the two entries together.
    raw["cards"].append(card_entry(7_000_000_004, "Seven Dwarves", ["Dwarf"], quantity=1))
    deck = parse_deck(raw)

    view = decklist_json(deck, Board(()), db, colours={}, share=lambda _: None)

    dwarves = [c for g in view["grid"] for c in g["cards"] if c["name"] == "Seven Dwarves"]
    assert [(c["quantity"], c["more"], c["less"]) for c in dwarves] == [(6, False, True), (1, False, True)]


class TestSave:
    """A save of count changes and an add without a cut against a fake Archidekt."""

    @pytest.fixture
    def fake(self, raw: Json) -> FakeArchidekt:
        archidekt = FakeArchidekt()
        archidekt.put(raw)
        return archidekt

    @pytest.fixture
    def stored(self, tmp_path: Path, deck: Deck) -> StoredDeck:
        return StoredDeck(Workspace(tmp_path / "decks").save(deck), deck)

    @pytest.fixture
    def on_board(self, stored: StoredDeck, deck: Deck, db: CardDb) -> BoardService:
        return BoardService(stored.folder, deck, db, now=lambda: NOW)

    def test_a_save_sends_quantities_removals_and_the_new_card_and_clears_the_board(
        self, tmp_path: Path, fake: FakeArchidekt, stored: StoredDeck, db: CardDb, on_board: BoardService
    ) -> None:
        on_board.step_count("Mountain", 1, Actor.YOU)
        on_board.step_count("Battered Golem", -1, Actor.YOU)
        signet = on_board.propose("Arcane Signet", Actor.YOU)
        on_board.decide(signet.id, Status.ADD, Actor.YOU, category="Ramp")

        with fake.client() as client:
            editor = ArchidektEditor(Fetcher(client, sleep=lambda _: None), ACCESS_TOKEN)
            saved = save_board(
                editor,
                Workspace(tmp_path / "decks"),
                stored,
                db,
                on_board,
                user="TESTER",
                actor=Actor.YOU,
                now=lambda: NOW,
            )

        writes = {(e["action"], e["modifications"]["quantity"]) for e in fake.card_writes()}
        assert writes == {("modify", 7), ("remove", 1), ("add", 1)}
        live = {
            e["card"]["oracleCard"]["name"]: (e["quantity"], e["categories"], e.get("label"))
            for e in fake.decks[DECK_ID]["cards"]
        }
        assert live["Mountain"][0] == 7
        assert "Battered Golem" not in live
        assert live["Arcane Signet"][:2] == (1, ["Ramp"])
        assert str(live["Arcane Signet"][2]).startswith("Don't Have,")
        assert saved.result is not None
        assert saved.result.mismatches == ()
        assert sorted(saved.applied) == ["Arcane Signet", "Battered Golem", "Mountain"]
        board = read_board(stored.folder)
        assert board.counts == ()
        assert [c.status for c in board.cards] == [Status.APPLIED]
