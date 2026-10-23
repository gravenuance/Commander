"""Cards that enter as a copy and the cost reducers of the goldfish game (ADR 0054): a copy is cast for the
reduced cost, and a copy of a reducer reduces. Hand-built decks of seven cards or fewer, which are dealt whole
into the opening hand, so each plays the same game every time."""

from dataclasses import replace

from commander.goldfish_cards import CardKind, CopyOf, GoldfishCard, Reducer, SpellKind
from test_goldfish import ARTIFACT, CREATURE, FOREST, make, only_outcome, reducer, repeat, run
from test_goldfish_copies import GIANT, clone, elf, mana_of, rock

ARTIFACT_SPELLS = SpellKind(kinds=frozenset({CardKind.ARTIFACT}))
BIRDS = SpellKind(subtype="bird")


def bird(mv: int) -> GoldfishCard:
    """A creature that enters as a copy of a creature of up to the mana spent on it."""
    return replace(
        clone("Bird", CopyOf.CREATURE, mv=mv, kinds=CREATURE, mana_spent=True), subtypes=frozenset({"bird"})
    )


def test_a_card_that_enters_as_a_copy_is_cast_for_the_cost_the_reducers_leave() -> None:
    stone = rock("Stone", 1)
    steel = clone("Steel", CopyOf.ARTIFACT, mv=4, kinds=ARTIFACT)
    cutter = reducer("Cutter", 1, 1, ARTIFACT_SPELLS)
    # The Cutter comes on turn one. On turn two Steel costs 3, the 3 mana there are, and a second Stone makes
    # 1 a turn for 4 turns: more than it costs, so it is made, and the turn makes 3 and 1 more: 4.
    # Turn three has 3 Forests and 2 Stones: 5.
    assert mana_of([*repeat(FOREST, 3), stone, cutter, steel, GIANT]) == (2, 4, 5, 5, 5)
    # Without the reduction Steel costs 4, more than the second Stone would make in turns three to five (3),
    # and it is never cast: turn three has 3 Forests and 1 Stone.
    plain = replace(cutter, reducers=())
    assert mana_of([*repeat(FOREST, 3), stone, plain, steel, GIANT]) == (2, 3, 4, 4, 4)


def test_a_clone_that_copies_for_the_mana_it_spends_pays_that_much_off_the_reduced_cost() -> None:
    cutter = reducer("Cutter", 1, 3, BIRDS)
    little = elf("Elf", 1, mv=3)
    deck = [*repeat(FOREST, 3), cutter, little, bird(5)]
    # The Cutter comes on turn one and the Elf on turn three. On turn four the 3 Forests and the Elf make 4.
    # The bird costs {5} less 3, 2, and 1 more to copy a creature of mana value 3: 3, not the 5 it costs
    # unreduced, which 4 mana could not pay. The copy makes its 1 from turn five: 3 Forests and 2 Elves.
    assert mana_of(deck, bracket=2) == (1, 2, 3, 4, 5, 5, 5, 5, 5, 5)
    plain = [*repeat(FOREST, 3), replace(cutter, reducers=()), little, bird(5)]
    assert mana_of(plain, bracket=2) == (1, 2, 3, 4, 4, 4, 4, 4, 4, 4)


def test_a_card_that_enters_as_a_copy_of_a_reducer_reduces_as_it_does() -> None:
    lamp = replace(rock("Lamp", 1), reducers=(Reducer((ARTIFACT_SPELLS,), 1),))
    steel = clone("Steel", CopyOf.ARTIFACT, mv=2, kinds=ARTIFACT)
    boss = make("Boss", mv=6, kinds=ARTIFACT)

    def boss_turn(the_lamp: GoldfishCard) -> int:
        slots = [*repeat(FOREST, 4), the_lamp, steel, GIANT]
        return only_outcome(run(slots, commanders=(boss,))).commander_turns[0]

    # The Lamp comes on turn one, and Steel for the 1 it then costs as a second Lamp. On turn two the Forests
    # and both Lamps make 4, which pays for the boss at {6} less 2.
    assert boss_turn(lamp) == 2
    # A legendary Lamp cannot be copied, so Steel is never cast and the boss costs 5, which turn four pays.
    assert boss_turn(replace(lamp, legendary=True)) == 4
