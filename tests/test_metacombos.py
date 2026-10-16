from collections.abc import Iterator
from dataclasses import replace
from datetime import date

import pytest

from commander.answers import Piece, piece_facts
from commander.archidekt import parse_deck
from commander.carddb import CardDb
from commander.edhtop16 import CommanderStats
from commander.metacombos import (
    AnswerRole,
    ComboAnswer,
    LockScope,
    Stop,
    as_cast,
    card_stops,
    meta_combos,
    stop_applies,
)
from commander.metadata import CommanderCombos, MetaCommander
from commander.models import Deck, DeckCard
from commander.spellbook import ComboDetail, ComboPiece, parse_details
from conftest import Json, build_card_db, load_fixture
from test_meta import REMOVAL_TAGS, entry

KINNAN = "Kinnan, Bonder Prodigy"
SINCE = date(2026, 8, 16)


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CardDb]:
    with CardDb(build_card_db(tmp_path_factory.mktemp("combodb"), REMOVAL_TAGS)) as opened:
        yield opened


@pytest.fixture
def deck(raw_deck: Json) -> Deck:
    # Magda, Battered Golem, Mountain and Disruptor Flute, whose lock spares mana abilities.
    return parse_deck(raw_deck)


def details() -> dict[str, ComboDetail]:
    return {d.id: d for d in parse_details(load_fixture("spellbook_meta_kinnan.json"))}


def combo(description: str, *, results: tuple[str, ...] = (), zones: tuple[str, ...] = ("B",)) -> ComboDetail:
    piece = ComboPiece("Basalt Monolith", "Artifact", zones)
    return ComboDetail("1-2", (piece,), (), results, description)


def test_activation_locks_reach_the_pieces_whose_abilities_the_steps_activate() -> None:
    kinnan = details()["129-4131"]
    assert stop_applies(Stop.ACTIVATIONS, LockScope("Artifact"), kinnan)
    # Kinnan is the creature, but the steps only activate the Monolith.
    assert not stop_applies(Stop.ACTIVATIONS, LockScope("Creature"), kinnan)


def test_a_lock_that_spares_mana_abilities_misses_steps_that_add_mana() -> None:
    mana_only = combo("Activate Basalt Monolith by tapping it, adding {3}.\nRepeat.")
    assert not stop_applies(Stop.ACTIVATIONS, LockScope(None, spares_mana=True), mana_only)
    assert stop_applies(Stop.ACTIVATIONS, LockScope("Artifact"), mana_only)
    # Untapping the Monolith is no mana ability, so the same lock stops Kinnan's loop.
    assert stop_applies(Stop.ACTIVATIONS, LockScope(None, spares_mana=True), details()["129-4131"])


def test_casting_locks_meet_storm_and_repeated_casts() -> None:
    hullbreaker = details()["513-5034--46"]
    assert stop_applies(Stop.CASTING, LockScope(), hullbreaker)
    assert not stop_applies(Stop.CASTING, LockScope(), details()["129-4131"])
    assert not stop_applies(Stop.ENTER_TRIGGERS, LockScope(), hullbreaker)
    assert not stop_applies(Stop.GRAVEYARD, LockScope(), hullbreaker)


@pytest.mark.parametrize(
    ("stop", "detail"),
    [
        (Stop.GRAVEYARD, combo("Mill it.", zones=("G",))),
        (Stop.OFF_HAND_CASTING, combo("Cast Basalt Monolith from your graveyard.")),
        (Stop.CHEATED_IN, combo("Return Basalt Monolith to the battlefield.")),
        (Stop.ENTER_TRIGGERS, combo("When Basalt Monolith enters, it triggers, untapping a land.")),
        (Stop.ENTER_TRIGGERS, combo("Basalt Monolith enters the battlefield, causing you to win the game.")),
        (Stop.TOKENS, combo("Loop.", results=("Infinite artifact tokens",))),
    ],
)
def test_each_stop_reads_the_steps_it_needs(stop: Stop, detail: ComboDetail) -> None:
    assert stop_applies(stop, LockScope(), detail)
    assert not stop_applies(stop, LockScope(), combo("Activate Basalt Monolith."))


WITNESS = ComboPiece("Eternal Witness", "Creature — Human Shaman", ("B",))
TALON_GATES = ComboPiece("Talon Gates of Madara", "Land", ("B",))
CAGE = (
    "Creature cards in graveyards and libraries can't enter the battlefield.\n"
    "Players can't cast spells from graveyards or libraries."
)


def steps(description: str, *pieces: ComboPiece, results: tuple[str, ...] = ()) -> ComboDetail:
    return ComboDetail("1-2", pieces, (), results, description)


def test_enter_trigger_locks_need_a_trigger_on_a_card_of_their_type() -> None:
    creatures = LockScope("Creature")
    # Real Spellbook steps: Eternal Witness's trigger, Talon Gates' (a land) and Hullbreaker's plain entering.
    witness = steps(
        "When Eternal Witness enters, it triggers, returning Snap from your graveyard to your hand.", WITNESS
    )
    gates = steps(
        "When Talon Gates of Madara enters the battlefield, it triggers, "
        "phasing out up to one target creature.",
        TALON_GATES,
    )
    plain = steps("That permanent enters the battlefield.", results=("Infinite creature ETB",))
    assert stop_applies(Stop.ENTER_TRIGGERS, creatures, witness)
    assert not stop_applies(Stop.ENTER_TRIGGERS, creatures, gates)
    assert stop_applies(Stop.ENTER_TRIGGERS, LockScope(), gates)
    assert not stop_applies(Stop.ENTER_TRIGGERS, LockScope(), plain)
    assert not stop_applies(
        Stop.ENTER_TRIGGERS,
        creatures,
        steps("When the Treasure enters the battlefield, Battered Golem triggers."),
    )


def test_a_lock_on_some_zones_misses_cards_put_in_from_others() -> None:
    [(_, scope)] = [found for found in card_stops(CAGE, set()) if found[0] is Stop.CHEATED_IN]
    assert scope == LockScope("Creature", zones=frozenset({"graveyard", "library"}))
    from_hand = steps(
        "Activate Talon Gates of Madara's third ability by paying {4}, "
        "putting it from your hand onto the battlefield.",
        TALON_GATES,
    )
    from_graveyard = steps("Return Eternal Witness from your graveyard to the battlefield.", WITNESS)
    assert not stop_applies(Stop.CHEATED_IN, scope, from_hand)
    assert stop_applies(Stop.CHEATED_IN, scope, from_graveyard)


def test_graveyard_hate_needs_rules_text_that_exiles_from_graveyards() -> None:
    hearse = "{T}: Exile up to two target cards from a single graveyard."
    assert Stop.GRAVEYARD not in {stop for stop, _ in card_stops(CAGE, {Stop.GRAVEYARD})}
    assert Stop.GRAVEYARD in {stop for stop, _ in card_stops(hearse, {Stop.GRAVEYARD})}


def test_pieces_cast_for_nothing_are_marked_free() -> None:
    amber = ComboPiece("Mox Amber", "Legendary Artifact", ("B",))
    vault = ComboPiece("Mana Vault", "Artifact", ("B",))
    facts = {"mox amber": Piece("Mox Amber", "Legendary Artifact", "", mana_value=0)}
    loop = steps("Cast Mana Vault by paying {1}.\nCast Mox Amber by paying {0}", amber, vault)
    assert as_cast(loop, amber, facts).cast_free
    assert not as_cast(loop, vault, facts).cast_free
    # A piece the card data doesn't know is free only when the steps say so.
    assert as_cast(steps("Cast Mana Vault by paying {0}.", vault), vault, {}).cast_free


def test_piece_facts_come_from_the_card_data(db: CardDb) -> None:
    facts = piece_facts(db, [("magda, brazen outlaw", "Creature"), ("Nobody Knows", "Creature — Horror")])
    assert facts["magda, brazen outlaw"] == Piece(
        "Magda, Brazen Outlaw", "Legendary Creature — Dwarf Berserker", "R", 2, 1, mana_value=2.0
    )
    assert facts["nobody knows"] == Piece("Nobody Knows", "Creature — Horror")


def kinnan_field(lists: str) -> list[MetaCommander]:
    """Kinnan's lists, one per letter: C holds the Basalt Monolith combo, - doesn't."""
    entries = tuple(
        entry(i + 1, 40, 8, [(KINNAN, 0), ("Basalt Monolith", 0)] if mark == "C" else [("Sol Ring", 0)])
        for i, mark in enumerate(lists)
    )
    return [MetaCommander(CommanderStats(KINNAN, 1000, 300, 0.3, 0.04), entries)]


def found() -> list[CommanderCombos]:
    return [CommanderCombos(KINNAN, tuple(details().values()))]


def test_meta_combos_are_those_enough_lists_hold_with_the_decks_answers(deck: Deck, db: CardDb) -> None:
    bolt = DeckCard("Lightning Bolt", 1, ("Removal",), True, "x", 1, 1, "tst", "1", "Normal")  # noqa: FBT003 — positional like the model
    with_bolt = replace(deck, cards=(*deck.cards, bolt))
    field = kinnan_field("C-")
    [kinnan] = meta_combos(with_bolt, field, found(), SINCE, db)
    assert kinnan.id == "129-4131"
    assert kinnan.played_by == ((KINNAN, 0.5),)
    assert kinnan.exposure == pytest.approx(0.5 * 0.04)
    assert kinnan.answers == (
        ComboAnswer("Disruptor Flute", "stops activated abilities", AnswerRole.STAX),
        ComboAnswer("Lightning Bolt", f"removes {KINNAN}", AnswerRole.REMOVAL),
    )


def test_a_combo_in_too_few_lists_is_left_out(deck: Deck, db: CardDb) -> None:
    field = kinnan_field("C-----")
    assert meta_combos(deck, field, found(), SINCE, db) == ()


def test_lists_before_the_window_do_not_count(deck: Deck, db: CardDb) -> None:
    old = entry(1, 40, 8, [(KINNAN, 0), ("Basalt Monolith", 0)], held_on=date(2026, 8, 1))
    field = [MetaCommander(CommanderStats(KINNAN, 1000, 300, 0.3, 0.04), (old,))]
    assert meta_combos(deck, field, found(), SINCE, db) == ()


def test_a_list_holds_its_commander_even_when_the_decklist_leaves_it_out(deck: Deck, db: CardDb) -> None:
    lists = (entry(1, 40, 8, [("Basalt Monolith", 0)]),)
    field = [MetaCommander(CommanderStats(KINNAN, 1000, 300, 0.3, 0.04), lists)]
    assert [c.id for c in meta_combos(deck, field, found(), SINCE, db)] == ["129-4131"]
