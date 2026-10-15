from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from commander.apply import (
    Change,
    ChangeKind,
    Resolution,
    SaveError,
    card_edits,
    default_category,
    resolve_plan,
    save,
    verify,
)
from commander.archidekt import parse_deck
from commander.archidekt_edit import ArchidektEditor, EditKind, LiveDeck, parse_entry_states
from commander.carddb import CardDb, CardInfo
from commander.models import Deck
from commander.net import Fetcher
from commander.plan import AddCard, Plan, RemoveCard
from commander.suggest import BracketRaise
from conftest import ACCESS_TOKEN, DECK_ID, FakeArchidekt, Json, archidekt_card_id

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
FLUTE_ENTRY = 2531358028
MOUNTAIN_ENTRY = 2081750497
GOLEM_ENTRY = 2081750457
ARCANE_SIGNET = "c6b6117c"
MAIN_CARDS = 9


@pytest.fixture
def db(card_db_file: Path) -> Iterator[CardDb]:
    with CardDb(card_db_file) as opened:
        yield opened


@pytest.fixture
def deck(raw_deck: Json) -> Deck:
    """Magda, Battered Golem, Disruptor Flute and six Mountains, set to bracket 5."""
    return parse_deck(raw_deck)


def plan(add: list[AddCard] | None = None, remove: list[RemoveCard] | None = None) -> Plan:
    return Plan("magda", tuple(add or ()), tuple(remove or ()))


def without_commander(deck: Deck) -> Deck:
    return replace(deck, cards=tuple(c for c in deck.cards if "Commander" not in c.categories))


def kinds(resolution: Resolution) -> list[tuple[ChangeKind, str, int, int]]:
    return [(c.kind, c.name, c.before, c.after) for c in resolution.changes]


def test_add_picks_the_cheapest_priced_printing_and_files_it_by_type(deck: Deck, db: CardDb) -> None:
    resolution = resolve_plan(deck, plan([AddCard("arcane signet")]), db)

    (change,) = resolution.changes
    assert (change.kind, change.name, change.after, change.category) == (
        ChangeKind.ADD,
        "Arcane Signet",
        1,
        "Artifact",
    )
    assert (change.set_code, change.collector_number, change.price_cents) == ("frc", "20", 30)
    assert change.scryfall_id.startswith(ARCANE_SIGNET)
    assert resolution.problems == ()


def test_add_picks_the_cheapest_of_several_priced_printings(deck: Deck, db: CardDb) -> None:
    resolution = resolve_plan(without_commander(deck), plan([AddCard("Magda, Brazen Outlaw")]), db)

    (change,) = resolution.changes
    assert (change.set_code, change.collector_number, change.price_cents) == ("khm", "142", 679)


def test_a_land_creature_is_filed_as_a_land(db: CardDb) -> None:
    golem = db.card_named("Battered Golem")
    assert golem is not None

    assert default_category(replace(golem, type_line="Land Creature — Forest Dryad")) == "Land"
    assert default_category(replace(golem, type_line="Legendary Artifact Creature — Golem")) == "Creature"
    assert default_category(replace(golem, type_line="Kindred Instant — Elf")) == "Instant"
    assert default_category(replace(golem, type_line="Conspiracy")) == "Uncategorized"


def test_add_takes_the_planned_set_and_category(deck: Deck, db: CardDb) -> None:
    wanted = AddCard("Magda, Brazen Outlaw", set_code="sld", category="Commander Backup")

    resolution = resolve_plan(without_commander(deck), plan([wanted]), db)

    (change,) = resolution.changes
    assert (change.set_code, change.price_cents, change.category) == ("sld", 1929, "Commander Backup")


def test_add_finds_a_card_by_its_front_face(deck: Deck, db: CardDb) -> None:
    resolution = resolve_plan(deck, plan([AddCard("Sundering Eruption")]), db)

    assert kinds(resolution) == [(ChangeKind.ADD, "Sundering Eruption // Volcanic Fissure", 0, 1)]
    assert resolution.changes[0].category == "Sorcery"


@pytest.mark.parametrize(
    ("wanted", "problem"),
    [
        (AddCard("Lightning Blot"), "add Lightning Blot: no such card in the card data"),
        (AddCard("Mana Crypt"), "add Mana Crypt: not legal in Commander"),
        (AddCard("Rhystic Study"), "add Rhystic Study: its colours (U) are outside the deck's (R)"),
        (AddCard("Lightning Bolt", set_code="lea"), "add Lightning Bolt: no printing in set 'lea'"),
        (AddCard("Chaos Warp"), "add Chaos Warp: no printing has a EUR price"),
    ],
)
def test_add_problems_block_the_plan(deck: Deck, db: CardDb, wanted: AddCard, problem: str) -> None:
    resolution = resolve_plan(deck, plan([wanted]), db)

    assert resolution.changes == ()
    assert len(resolution.problems) == 1
    assert resolution.problems[0].startswith(problem)


def test_an_unpriced_card_can_be_added_by_naming_its_set(deck: Deck, db: CardDb) -> None:
    resolution = resolve_plan(deck, plan([AddCard("Chaos Warp", set_code="mbc")]), db)

    assert resolution.changes[0].price_cents is None
    assert resolution.problems == ()


def test_add_skips_a_card_the_deck_already_holds(deck: Deck, db: CardDb) -> None:
    resolution = resolve_plan(deck, plan([AddCard("Battered Golem"), AddCard("Mountain", 6)]), db)

    assert resolution.changes == ()
    assert resolution.skipped == (
        "add Battered Golem: already 1 in the deck",
        "add Mountain: already 6 in the deck",
    )


def test_add_raises_copies_up_to_the_planned_count(deck: Deck, db: CardDb) -> None:
    resolution = resolve_plan(deck, plan([AddCard("Mountain", 9)]), db)

    assert kinds(resolution) == [(ChangeKind.MORE, "Mountain", 6, 9)]
    assert resolution.changes[0].entry_id == MOUNTAIN_ENTRY
    assert resolution.warnings == (f"the deck will have {MAIN_CARDS + 3} cards, not 100",)


def test_several_copies_of_a_non_basic_card_are_a_warning(deck: Deck, db: CardDb) -> None:
    resolution = resolve_plan(deck, plan([AddCard("Arcane Signet", 2)]), db)

    assert kinds(resolution) == [(ChangeKind.ADD, "Arcane Signet", 0, 2)]
    assert resolution.warnings[0] == "Arcane Signet: 2 copies; Commander allows one unless the card says so"


def test_an_added_game_changer_is_marked_when_it_lifts_the_bracket(deck: Deck, db: CardDb) -> None:
    low = replace(deck, bracket=2)

    resolution = resolve_plan(low, plan([AddCard("Jeska's Will")]), db)

    assert resolution.changes[0].raises == BracketRaise(3, "game changer")


def test_each_add_counts_the_game_changers_added_before_it(
    deck: Deck, db: CardDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, int]] = []

    def record(info: CardInfo, _bracket: int | None, changers: int) -> None:
        seen.append((info.name, changers))

    monkeypatch.setattr("commander.apply.card_raise", record)
    adds = [
        AddCard("Jeska's Will"),
        AddCard("Arcane Signet"),
        AddCard("Ancient Tomb"),
        AddCard("Mind Stone", set_code="mbc"),
    ]

    resolve_plan(replace(deck, bracket=3), plan(adds), db)

    assert seen == [("Jeska's Will", 0), ("Arcane Signet", 1), ("Ancient Tomb", 1), ("Mind Stone", 2)]


def test_remove_takes_every_copy_and_keep_leaves_some(deck: Deck, db: CardDb) -> None:
    resolution = resolve_plan(
        deck, plan(remove=[RemoveCard("disruptor flute"), RemoveCard("Mountain", keep=4)]), db
    )

    assert kinds(resolution) == [
        (ChangeKind.REMOVE, "Disruptor Flute", 1, 0),
        (ChangeKind.FEWER, "Mountain", 6, 4),
    ]
    assert resolution.main_after == MAIN_CARDS - 3
    assert resolution.changes[0].entry_id == FLUTE_ENTRY


def test_remove_skips_what_the_deck_already_meets(deck: Deck, db: CardDb) -> None:
    removes = [
        RemoveCard("Lightning Bolt"),
        RemoveCard("Mountain", keep=6),
        RemoveCard("High-Speed Hoverbike"),
    ]

    resolution = resolve_plan(deck, plan(remove=removes), db)

    assert resolution.changes == ()
    assert resolution.skipped == (
        "remove Lightning Bolt: not in the deck",
        "remove Mountain: already 6",
        # A maybeboard copy is not in the deck, so it stays.
        "remove High-Speed Hoverbike: not in the deck",
    )


def test_keep_on_a_card_in_several_entries_is_a_problem(deck: Deck, db: CardDb) -> None:
    mountain = next(c for c in deck.cards if c.name == "Mountain")
    split = replace(deck, cards=(*deck.cards, replace(mountain, entry_id=1, quantity=2)))

    whole = resolve_plan(split, plan(remove=[RemoveCard("Mountain")]), db)
    partial = resolve_plan(split, plan(remove=[RemoveCard("Mountain", keep=1)]), db)

    assert [c.after for c in whole.changes] == [0, 0]
    assert partial.problems == (
        "remove Mountain: the deck has it in 2 entries; remove it whole, or change the copies on Archidekt",
    )


def test_a_full_deck_has_no_size_warning(deck: Deck, db: CardDb) -> None:
    mountain = next(c for c in deck.cards if c.name == "Mountain")
    full = replace(deck, cards=tuple(replace(c, quantity=97) if c is mountain else c for c in deck.cards))

    resolution = resolve_plan(full, plan([AddCard("Arcane Signet")], [RemoveCard("Disruptor Flute")]), db)

    assert resolution.main_before == resolution.main_after == 100
    assert resolution.warnings == ()


def test_a_deck_without_a_commander_is_not_colour_checked(deck: Deck, db: CardDb) -> None:
    headless = without_commander(deck)

    resolution = resolve_plan(headless, plan([AddCard("Rhystic Study")]), db)

    assert resolution.problems == ()
    assert resolution.warnings[0] == "the deck has no known commander, so colours are not checked"


@pytest.mark.parametrize(
    ("name", "category"),
    [
        ("Ancient Tomb", "Land"),
        ("Battered Golem", "Creature"),
        ("Jeska's Will", "Sorcery"),
        ("Lightning Bolt", "Instant"),
        ("Arcane Signet", "Artifact"),
        ("Aggravated Assault", "Enchantment"),
    ],
)
def test_default_category_is_the_first_card_type(db: CardDb, name: str, category: str) -> None:
    info = db.card_named(name)

    assert info is not None
    assert default_category(info) == category


def change(kind: ChangeKind, after: int, entry_id: int | None, card_id: int | None = 5) -> Change:
    return Change(
        kind,
        "X",
        1,
        after,
        "Ramp",
        in_deck=True,
        set_code="tst",
        collector_number="1",
        scryfall_id="sid",
        card_id=card_id,
        entry_id=entry_id,
    )


def test_card_edits_send_back_each_entry_and_use_the_looked_up_ids(raw_deck: Json) -> None:
    entries = parse_entry_states(raw_deck)
    changes = [
        change(ChangeKind.ADD, 2, None, card_id=None),
        change(ChangeKind.FEWER, 3, MOUNTAIN_ENTRY),
        change(ChangeKind.REMOVE, 0, FLUTE_ENTRY),
    ]

    edits = card_edits(changes, entries, {"sid": 42})

    assert [(e.kind, e.card_id, e.quantity, e.entry_id) for e in edits] == [
        (EditKind.ADD, 42, 2, None),
        (EditKind.MODIFY, 5, 3, MOUNTAIN_ENTRY),
        (EditKind.REMOVE, 5, 1, FLUTE_ENTRY),
    ]
    assert edits[0].state.categories == ("Ramp",)
    assert edits[2].state == entries[FLUTE_ENTRY]


def test_card_edits_refuse_an_entry_that_is_gone(raw_deck: Json) -> None:
    with pytest.raises(Exception, match="no longer in the deck"):
        card_edits([change(ChangeKind.REMOVE, 0, 77)], parse_entry_states(raw_deck), {})


def test_verify_reports_changes_archidekt_does_not_show(deck: Deck) -> None:
    golem = next(c for c in deck.cards if c.entry_id == GOLEM_ENTRY)
    changes = [
        Change(
            ChangeKind.ADD,
            "Arcane Signet",
            0,
            1,
            "Artifact",
            in_deck=True,
            set_code="frc",
            collector_number="20",
            scryfall_id="missing-id",
        ),
        Change(
            ChangeKind.REMOVE,
            "Battered Golem",
            1,
            0,
            "Ramp",
            in_deck=True,
            set_code="5dn",
            collector_number="106",
            scryfall_id=golem.scryfall_id,
            entry_id=GOLEM_ENTRY,
        ),
        Change(
            ChangeKind.FEWER,
            "Mountain",
            6,
            6,
            "Land",
            in_deck=True,
            set_code="j25",
            collector_number="90",
            scryfall_id="m",
            entry_id=MOUNTAIN_ENTRY,
        ),
    ]

    assert verify(changes, deck) == [
        "Arcane Signet: expected 1 added, Archidekt shows 0",
        "Battered Golem: expected 0, Archidekt shows 1",
    ]


@pytest.fixture
def fake(raw_deck: Json) -> FakeArchidekt:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    return fake


@pytest.fixture
def editor(fake: FakeArchidekt) -> Iterator[ArchidektEditor]:
    with fake.client() as client:
        yield ArchidektEditor(Fetcher(client, sleep=lambda _: None), ACCESS_TOKEN)


def test_save_looks_up_snapshots_sends_once_and_reads_back(
    fake: FakeArchidekt, editor: ArchidektEditor, db: CardDb
) -> None:
    live = editor.get_deck(DECK_ID)
    resolution = resolve_plan(
        live.deck, plan([AddCard("Arcane Signet")], [RemoveCard("Mountain", keep=5)]), db
    )
    fake.requests.clear()
    progress: list[str] = []

    result = save(editor, live, resolution, now=lambda: NOW, on_progress=progress.append)

    assert fake.requests == [
        "/api/cards/v2/",
        f"/api/decks/{DECK_ID}/snapshots/",
        f"/api/decks/{DECK_ID}/modifyCards/v2/",
        f"/api/decks/{DECK_ID}/",
    ]
    assert result.snapshot == "before cmdr apply 2026-10-15 12:00 UTC"
    assert result.mismatches == ()
    signet = next(c for c in result.deck.cards if c.name == "Arcane Signet")
    assert (signet.archidekt_card_id, signet.categories) == (
        archidekt_card_id(signet.scryfall_id),
        ("Artifact",),
    )
    assert next(c for c in result.deck.cards if c.name == "Mountain").quantity == 5
    assert progress[0] == "looking up Arcane Signet (FRC) on Archidekt"


def test_save_stops_before_any_write_when_archidekt_lacks_the_printing(
    fake: FakeArchidekt, editor: ArchidektEditor, db: CardDb
) -> None:
    live = editor.get_deck(DECK_ID)
    resolution = resolve_plan(live.deck, plan([AddCard("Arcane Signet")]), db)
    moved = replace(resolution, changes=(replace(resolution.changes[0], scryfall_id="elsewhere"),))

    with pytest.raises(SaveError, match=r"Archidekt has no Arcane Signet from FRC 20.*Nothing was changed"):
        save(editor, live, moved, now=lambda: NOW)

    assert fake.writes == []


def test_a_failed_save_after_the_snapshot_names_it(
    fake: FakeArchidekt, editor: ArchidektEditor, db: CardDb
) -> None:
    live = editor.get_deck(DECK_ID)
    resolution = resolve_plan(live.deck, plan(remove=[RemoveCard("Disruptor Flute")]), db)
    fake.write_status["modifyCards"] = 500

    with pytest.raises(
        SaveError, match=r"HTTP 500\. The snapshot 'before cmdr apply 2026-10-15 12:00 UTC' is on"
    ):
        save(editor, live, resolution, now=lambda: NOW)

    assert [p for _, p, _, _ in fake.writes] == [
        f"/api/decks/{DECK_ID}/snapshots/",
        f"/api/decks/{DECK_ID}/modifyCards/v2/",
    ]


def test_save_refuses_a_resolution_with_problems_or_no_changes(deck: Deck, raw_deck: Json) -> None:
    live = LiveDeck(deck, parse_entry_states(raw_deck))
    editor = ArchidektEditor.__new__(ArchidektEditor)

    with pytest.raises(ValueError, match="problems"):
        save(editor, live, Resolution(deck, (), ("bad",), ()), now=lambda: NOW)
    with pytest.raises(ValueError, match="nothing to save"):
        save(editor, live, Resolution(deck, (), (), ()), now=lambda: NOW)
