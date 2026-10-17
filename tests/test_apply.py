from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from commander import apply as apply_module
from commander.apply import (
    Change,
    ChangeKind,
    Resolution,
    SaveError,
    SaveReadBackError,
    card_edits,
    default_category,
    resolve_plan,
    save,
    verify,
)
from commander.archidekt import parse_deck
from commander.archidekt_edit import ArchidektEditor, AuthError, EditKind, LiveDeck, parse_entry_states
from commander.carddb import CardDb, CardInfo
from commander.changetext import change_text
from commander.models import Deck
from commander.net import MAX_ATTEMPTS, Fetcher
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


def split_mountains(deck: Deck) -> Deck:
    """The six Mountains plus four more in a second printing: ten in two entries."""
    mountain = next(c for c in deck.cards if c.name == "Mountain")
    return replace(deck, cards=(*deck.cards, replace(mountain, entry_id=1, quantity=4)))


def read_back(deck: Deck, quantities: dict[int, int]) -> Deck:
    """The deck as Archidekt would show it with these entry quantities (0 drops the entry)."""
    cards = tuple(
        replace(c, quantity=quantities.get(c.entry_id, c.quantity))
        for c in deck.cards
        if quantities.get(c.entry_id, c.quantity) > 0
    )
    return replace(deck, cards=cards)


def test_cutting_copies_of_a_card_in_several_entries_takes_them_from_the_last_entry(
    deck: Deck, db: CardDb
) -> None:
    split = split_mountains(deck)

    resolution = resolve_plan(split, plan(remove=[RemoveCard("Mountain", keep=9)]), db)

    assert resolution.problems == ()
    assert [(c.kind, c.entry_id, c.before, c.after) for c in resolution.changes] == [
        (ChangeKind.FEWER, 1, 4, 3)
    ]
    assert verify(resolution.changes, split, read_back(split, {1: 3})) == []


def test_cutting_more_copies_than_the_last_entry_holds_empties_it_and_trims_the_one_before(
    deck: Deck, db: CardDb
) -> None:
    split = split_mountains(deck)

    resolution = resolve_plan(split, plan(remove=[RemoveCard("Mountain", keep=5)]), db)

    assert resolution.problems == ()
    assert [(c.kind, c.entry_id, c.before, c.after) for c in resolution.changes] == [
        (ChangeKind.FEWER, MOUNTAIN_ENTRY, 6, 5),
        (ChangeKind.REMOVE, 1, 4, 0),
    ]
    assert resolution.main_after == MAIN_CARDS + 4 - 5
    assert verify(resolution.changes, split, read_back(split, {MOUNTAIN_ENTRY: 5, 1: 0})) == []


def test_removing_a_card_in_several_entries_removes_each(deck: Deck, db: CardDb) -> None:
    resolution = resolve_plan(split_mountains(deck), plan(remove=[RemoveCard("Mountain")]), db)

    assert [(c.kind, c.entry_id, c.after) for c in resolution.changes] == [
        (ChangeKind.REMOVE, MOUNTAIN_ENTRY, 0),
        (ChangeKind.REMOVE, 1, 0),
    ]


def test_keep_counts_every_entry_of_the_card(deck: Deck, db: CardDb) -> None:
    resolution = resolve_plan(split_mountains(deck), plan(remove=[RemoveCard("Mountain", keep=10)]), db)

    assert resolution.changes == ()
    assert resolution.skipped == ("remove Mountain: already 10",)


def test_a_commander_is_never_removed(deck: Deck, db: CardDb) -> None:
    for wanted in (
        RemoveCard("Magda, Brazen Outlaw"),
        RemoveCard("Magda, Brazen Outlaw", maybeboard=True),
    ):
        resolution = resolve_plan(deck, plan(remove=[wanted]), db)

        assert resolution.changes == ()
        assert resolution.problems == (
            "remove Magda, Brazen Outlaw: it is a commander and stays in the deck",
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
    assert edits[0].state.label == "Don't Have,#f47373"
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

    assert verify(changes, deck, deck) == [
        "Arcane Signet: expected 1 added, Archidekt shows 0",
        "Battered Golem: expected 0, Archidekt shows 1",
    ]


def test_verify_does_not_count_a_copy_that_was_already_on_the_maybeboard(deck: Deck) -> None:
    # Seen live: a maybeboard copy of the same printing made a failed add look done.
    bike = next(c for c in deck.cards if c.name == "High-Speed Hoverbike")
    add = Change(
        ChangeKind.ADD,
        bike.name,
        0,
        1,
        "Artifact",
        in_deck=True,
        set_code="neo",
        collector_number="247",
        scryfall_id=bike.scryfall_id,
    )
    landed = replace(bike, entry_id=1, categories=("Artifact",), in_deck=True)

    assert verify([add], deck, deck) == ["High-Speed Hoverbike: expected 1 added, Archidekt shows 0"]
    assert verify([add], deck, replace(deck, cards=(*deck.cards, landed))) == []


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
        "/api/decks/colorTags/",
        f"/api/decks/{DECK_ID}/snapshots/",
        f"/api/decks/{DECK_ID}/modifyCards/v2/",
        f"/api/decks/{DECK_ID}/",
    ]
    assert result.snapshot == "before cmdr apply 2026-10-15 12:00:00 UTC"
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
        SaveError, match=r"HTTP 500\. The snapshot 'before cmdr apply 2026-10-15 12:00:00 UTC' is on"
    ):
        save(editor, live, resolution, now=lambda: NOW)

    assert [p for _, p, _, _ in fake.writes] == [
        f"/api/decks/{DECK_ID}/snapshots/",
        f"/api/decks/{DECK_ID}/modifyCards/v2/",
    ]


@pytest.fixture
def label_waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The waits before a label read is tried again, without actually waiting."""
    waits: list[float] = []
    monkeypatch.setattr(apply_module.time, "sleep", waits.append)
    monkeypatch.setattr(apply_module, "LABEL_RETRY_SECONDS", 2.5)
    return waits


def label_reads(fake: FakeArchidekt) -> int:
    return fake.requests.count("/api/decks/colorTags/")


def test_a_failed_label_read_is_tried_once_more_after_a_wait_and_the_accounts_colour_is_used(
    fake: FakeArchidekt, editor: ArchidektEditor, db: CardDb, label_waits: list[float]
) -> None:
    live = editor.get_deck(DECK_ID)
    resolution = resolve_plan(live.deck, plan([AddCard("Arcane Signet")]), db)
    fake.labels = [{"id": 2, "color": "#fa0d11", "name": "Don't Have"}]
    # The whole first read fails, retries included.
    fake.labels_failures = MAX_ATTEMPTS
    progress: list[str] = []

    result = save(editor, live, resolution, now=lambda: NOW, on_progress=progress.append)

    assert result.mismatches == ()
    assert {w["modifications"]["label"] for w in fake.card_writes()} == {"Don't Have,#fa0d11"}
    assert (label_reads(fake), label_waits) == (MAX_ATTEMPTS + 1, [2.5])
    assert not any("default colours" in line for line in progress)


def test_a_save_whose_label_read_fails_twice_labels_incoming_cards_in_the_default_colour(
    fake: FakeArchidekt, editor: ArchidektEditor, db: CardDb, label_waits: list[float]
) -> None:
    live = editor.get_deck(DECK_ID)
    resolution = resolve_plan(live.deck, plan([AddCard("Arcane Signet")]), db)
    fake.labels = [{"id": 2, "color": "#fa0d11", "name": "Don't Have"}]
    fake.labels_status = 500
    progress: list[str] = []

    result = save(editor, live, resolution, now=lambda: NOW, on_progress=progress.append)

    assert result.mismatches == ()
    assert (label_reads(fake), label_waits) == (2 * MAX_ATTEMPTS, [2.5])
    # Archidekt's default colour for Don't Have (ADR 0012), not the account's #fa0d11.
    assert {w["modifications"]["label"] for w in fake.card_writes()} == {"Don't Have,#f47373"}
    assert any("labels" in line and "default colours" in line for line in progress)


def test_a_save_stops_before_any_write_when_the_label_read_is_refused(
    fake: FakeArchidekt, editor: ArchidektEditor, db: CardDb, label_waits: list[float]
) -> None:
    live = editor.get_deck(DECK_ID)
    resolution = resolve_plan(live.deck, plan([AddCard("Arcane Signet")]), db)
    fake.labels_status = 401

    with pytest.raises(AuthError, match="refused the login"):
        save(editor, live, resolution, now=lambda: NOW)

    assert fake.writes == []
    assert (label_reads(fake), label_waits) == (1, [])


def test_save_refuses_a_resolution_with_problems_or_no_changes(deck: Deck, raw_deck: Json) -> None:
    live = LiveDeck(deck, parse_entry_states(raw_deck))
    editor = ArchidektEditor.__new__(ArchidektEditor)

    with pytest.raises(ValueError, match="problems"):
        save(editor, live, Resolution(deck, (), ("bad",), ()), now=lambda: NOW)
    with pytest.raises(ValueError, match="nothing to save"):
        save(editor, live, Resolution(deck, (), (), ()), now=lambda: NOW)


def test_one_card_named_two_ways_in_add_is_a_problem(deck: Deck, db: CardDb) -> None:
    adds = [AddCard("Sundering Eruption"), AddCard("Sundering Eruption // Volcanic Fissure")]

    resolution = resolve_plan(without_commander(deck), plan(adds), db)

    assert resolution.problems == (
        "add Sundering Eruption // Volcanic Fissure: the plan already adds this card",
    )


def test_one_card_both_added_and_removed_under_two_names_is_a_problem(deck: Deck, db: CardDb) -> None:
    sundering = Deck(
        deck.id,
        deck.name,
        deck.owner,
        deck.bracket,
        deck.updated_at,
        (
            *deck.cards,
            replace(
                deck.cards[0],
                name="Sundering Eruption // Volcanic Fissure",
                categories=("Removal",),
                entry_id=5,
                scryfall_id="50686ac7",
            ),
        ),
    )

    resolution = resolve_plan(
        sundering,
        plan([AddCard("Sundering Eruption // Volcanic Fissure")], [RemoveCard("sundering eruption")]),
        db,
    )

    assert resolution.problems == (
        "add Sundering Eruption // Volcanic Fissure: the plan also removes this card",
    )


def test_two_removes_of_one_card_are_a_problem(deck: Deck, db: CardDb) -> None:
    sundering = Deck(
        deck.id,
        deck.name,
        deck.owner,
        deck.bracket,
        deck.updated_at,
        (
            *deck.cards,
            replace(
                deck.cards[0],
                name="Sundering Eruption // Volcanic Fissure",
                categories=("Removal",),
                entry_id=5,
            ),
        ),
    )

    resolution = resolve_plan(
        sundering,
        plan(remove=[RemoveCard("Sundering Eruption"), RemoveCard("Sundering Eruption // Volcanic Fissure")]),
        db,
    )

    assert resolution.problems == (
        "remove Sundering Eruption // Volcanic Fissure: the plan already removes this card",
    )


def test_a_failed_read_back_says_the_changes_were_sent(
    fake: FakeArchidekt, editor: ArchidektEditor, db: CardDb
) -> None:
    live = editor.get_deck(DECK_ID)
    resolution = resolve_plan(live.deck, plan(remove=[RemoveCard("Disruptor Flute")]), db)
    fake.failing[DECK_ID] = 404

    with pytest.raises(SaveReadBackError) as raised:
        save(editor, live, resolution, now=lambda: NOW)

    assert str(raised.value) == (
        "Archidekt accepted the changes, but reading the deck back failed "
        "(https://archidekt.com/api/decks/10004639/ returned HTTP 404); the local copy may be behind"
    )

    assert len(fake.writes) == 2


@pytest.mark.parametrize(
    ("category", "categories", "counts"),
    [
        # The deck's own setting wins: Archidekt lets each deck choose which categories count.
        ("Maybeboard", {"Maybeboard": False}, False),
        ("Sideboard", {"Sideboard": True}, True),
        ("Burn", {"Burn": False}, False),
        # A category the deck lacks is created by the save; seen live on 2026-10-15, Archidekt creates
        # Maybeboard outside the deck but Sideboard, like any other name, inside it.
        ("Sideboard", {}, True),
        ("maybeboard", {}, False),
        ("Burn", {}, True),
    ],
)
def test_an_add_counts_in_the_deck_as_its_category_says(
    deck: Deck, db: CardDb, category: str, categories: dict[str, bool], *, counts: bool
) -> None:
    wanted = AddCard("Arcane Signet", category=category)

    resolution = resolve_plan(deck, plan([wanted]), db, categories=categories)

    assert resolution.changes[0].in_deck is counts
    assert resolution.main_after == MAIN_CARDS + (1 if counts else 0)


def test_a_new_sideboard_category_warns_that_it_will_count(deck: Deck, db: CardDb) -> None:
    resolution = resolve_plan(deck, plan([AddCard("Arcane Signet", category="Sideboard")]), db, categories={})

    assert resolution.warnings[0] == (
        "Arcane Signet: Archidekt creates the new category Sideboard counting in the deck; "
        "untick it on Archidekt to keep it out"
    )


def test_add_skips_non_regular_printings_unless_the_plan_names_the_set(deck: Deck, db: CardDb) -> None:
    default = resolve_plan(deck, plan([AddCard("Ancient Tomb")]), db)
    named = resolve_plan(deck, plan([AddCard("Ancient Tomb", set_code="wc99")]), db)

    assert (default.changes[0].set_code, default.changes[0].price_cents) == ("uma", 10264)
    assert (named.changes[0].set_code, named.changes[0].price_cents) == ("wc99", 4163)


HOVERBIKE_ENTRY = 3096264032
TEMPESTRA_ENTRY = 3115861444


def test_a_cut_to_the_maybeboard_keeps_the_entry_and_its_categories(deck: Deck, db: CardDb) -> None:
    resolution = resolve_plan(deck, plan(remove=[RemoveCard("Battered Golem", maybeboard=True)]), db)
    [moved] = resolution.changes
    assert (moved.kind, moved.entry_id, moved.before, moved.after) == (
        ChangeKind.SET_ASIDE,
        GOLEM_ENTRY,
        1,
        1,
    )
    # Maybeboard first, so the card leaves the deck; its own categories stay for moving it back.
    assert (moved.category, moved.categories, moved.in_deck) == (
        "Maybeboard",
        ("Maybeboard", "Combo Pieces", "Ramp"),
        False,
    )
    assert (resolution.main_before, resolution.main_after) == (MAIN_CARDS, MAIN_CARDS - 1)


def test_adding_a_maybeboard_card_moves_its_entry_into_the_deck(deck: Deck, db: CardDb) -> None:
    resolution = resolve_plan(deck, plan([AddCard("High-Speed Hoverbike")]), db)
    [moved] = resolution.changes
    assert (moved.kind, moved.entry_id, moved.after) == (ChangeKind.MOVE_IN, HOVERBIKE_ENTRY, 1)
    # Its printing stays; with no category of its own besides Maybeboard it is filed by type.
    assert (moved.category, moved.categories, moved.in_deck) == ("Artifact", ("Artifact",), True)
    assert moved.set_code == next(c for c in deck.cards if c.name == "High-Speed Hoverbike").set_code
    assert resolution.main_after == MAIN_CARDS + 1


def test_moving_in_a_maybeboard_card_brings_the_copies_the_plan_asks_for(deck: Deck, db: CardDb) -> None:
    resolution = resolve_plan(deck, plan([AddCard("High-Speed Hoverbike", 3)]), db)

    [moved] = resolution.changes
    assert (moved.kind, moved.before, moved.after) == (ChangeKind.MOVE_IN, 1, 3)
    assert resolution.main_after == MAIN_CARDS + 3
    bike = next(c for c in deck.cards if c.name == "High-Speed Hoverbike")
    moved_in = replace(bike, quantity=3, categories=("Artifact",), in_deck=True)
    shown = replace(deck, cards=tuple(moved_in if c is bike else c for c in deck.cards))
    assert verify(resolution.changes, deck, shown) == []
    assert verify(resolution.changes, deck, deck) == ["High-Speed Hoverbike: expected 3, Archidekt shows 1"]


def test_a_move_in_of_several_copies_says_how_many(deck: Deck, db: CardDb) -> None:
    [one] = resolve_plan(deck, plan([AddCard("High-Speed Hoverbike")]), db).changes
    [three] = resolve_plan(deck, plan([AddCard("High-Speed Hoverbike", 3)]), db).changes
    assert change_text(one) == "+ High-Speed Hoverbike into the deck from the maybeboard, in Artifact"
    assert change_text(three) == "+ 3 High-Speed Hoverbike into the deck from the maybeboard, in Artifact"


def test_moving_in_keeps_a_maybeboard_stack_larger_than_the_plan_asks(deck: Deck, db: CardDb) -> None:
    bike = next(c for c in deck.cards if c.name == "High-Speed Hoverbike")
    stack = replace(deck, cards=tuple(replace(c, quantity=2) if c is bike else c for c in deck.cards))

    resolution = resolve_plan(stack, plan([AddCard("High-Speed Hoverbike")]), db)

    [moved] = resolution.changes
    assert (moved.before, moved.after) == (2, 2)
    assert resolution.main_after == MAIN_CARDS + 2


def test_moving_in_warns_when_the_plan_names_another_printing(deck: Deck, db: CardDb) -> None:
    resolution = resolve_plan(deck, plan([AddCard("High-Speed Hoverbike", set_code="sld")]), db)

    assert resolution.problems == ()
    assert resolution.warnings[0] == (
        "High-Speed Hoverbike: it comes from the Maybeboard in its own printing (NEO), not SLD"
    )


def test_moving_back_restores_the_categories_kept_behind_the_maybeboard(deck: Deck, db: CardDb) -> None:
    golem = next(c for c in deck.cards if c.name == "Battered Golem")
    aside = replace(golem, categories=("Maybeboard", "Combo Pieces", "Ramp"), in_deck=False)
    cards = tuple(aside if c is golem else c for c in deck.cards)
    resolution = resolve_plan(replace(deck, cards=cards), plan([AddCard("Battered Golem")]), db)
    [moved] = resolution.changes
    assert (moved.kind, moved.categories) == (ChangeKind.MOVE_IN, ("Combo Pieces", "Ramp"))


def test_drop_takes_a_card_off_the_maybeboard_only(deck: Deck, db: CardDb) -> None:
    dropping = Plan("magda", (), (), ("Tempestra, Dame of Games", "Battered Golem"))
    resolution = resolve_plan(deck, dropping, db)
    assert kinds(resolution) == [(ChangeKind.DROP, "Tempestra, Dame of Games", 1, 0)]
    assert resolution.skipped == ("drop Battered Golem: not on the Sideboard or Maybeboard",)
    assert resolution.main_after == MAIN_CARDS


def test_a_swap_both_ways_keeps_the_deck_size(deck: Deck, db: CardDb) -> None:
    swap = plan([AddCard("High-Speed Hoverbike")], [RemoveCard("Battered Golem", maybeboard=True)])
    resolution = resolve_plan(deck, swap, db)
    assert [c.kind for c in resolution.changes] == [ChangeKind.SET_ASIDE, ChangeKind.MOVE_IN]
    assert resolution.main_after == MAIN_CARDS


def test_moves_are_sent_as_modifies_with_the_new_categories(raw_deck: Json, deck: Deck, db: CardDb) -> None:
    swap = plan([AddCard("High-Speed Hoverbike")], [RemoveCard("Battered Golem", maybeboard=True)])
    dropping = Plan("magda", (), (), ("Tempestra, Dame of Games",))
    changes = [*resolve_plan(deck, swap, db).changes, *resolve_plan(deck, dropping, db).changes]
    edits = card_edits(changes, parse_entry_states(raw_deck), {})
    assert [(e.kind, e.entry_id, e.state.categories) for e in edits] == [
        (EditKind.MODIFY, GOLEM_ENTRY, ("Maybeboard", "Combo Pieces", "Ramp")),
        (EditKind.MODIFY, HOVERBIKE_ENTRY, ("Artifact",)),
        (EditKind.REMOVE, TEMPESTRA_ENTRY, ("Maybeboard",)),
    ]


def test_a_card_moved_into_the_deck_is_still_to_buy_unless_it_has_a_label(
    raw_deck: Json, deck: Deck, db: CardDb
) -> None:
    swap = plan([AddCard("High-Speed Hoverbike")], [RemoveCard("Battered Golem", maybeboard=True)])
    changes = resolve_plan(deck, swap, db).changes
    entries = parse_entry_states(raw_deck)
    # The cut keeps its label; the card coming in from the Maybeboard is one to buy.
    assert [e.state.label for e in card_edits(changes, entries, {})] == [",#656565", "Don't Have,#f47373"]
    proxied = {**entries, HOVERBIKE_ENTRY: replace(entries[HOVERBIKE_ENTRY], label="Proxy,#f445f4")}
    assert card_edits(changes, proxied, {})[1].state.label == "Proxy,#f445f4"


def test_verify_reports_a_move_archidekt_did_not_make(deck: Deck, db: CardDb) -> None:
    resolution = resolve_plan(deck, plan(remove=[RemoveCard("Battered Golem", maybeboard=True)]), db)
    assert verify(resolution.changes, deck, deck) == [
        "Battered Golem: expected it in Maybeboard, Archidekt shows Combo Pieces"
    ]


def test_save_moves_cards_both_ways_on_archidekt(
    fake: FakeArchidekt, editor: ArchidektEditor, db: CardDb
) -> None:
    live = editor.get_deck(DECK_ID)
    swap = plan([AddCard("High-Speed Hoverbike")], [RemoveCard("Battered Golem", maybeboard=True)])
    result = save(
        editor, live, resolve_plan(live.deck, swap, db, categories=live.categories), now=lambda: NOW
    )
    assert result.mismatches == ()
    by_name = {c.name: c for c in result.deck.cards}
    assert (by_name["Battered Golem"].aside, by_name["High-Speed Hoverbike"].in_deck) == (True, True)
    # Moves need no card lookup: the entries are already in the deck.
    assert "/api/cards/v2/" not in fake.requests
