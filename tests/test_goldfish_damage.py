"""Treasures made as creatures deal combat damage in the goldfish game (ADR 0054): hand-built decks whose
turns are worked out by hand. No creature is blocked, so every attacker deals its damage to a player.

A deck of seven cards or fewer is dealt whole into the opening hand, whatever the shuffle, so its game is the
same each time."""

from dataclasses import replace

import pytest

from commander.goldfish_cards import DamageScope, DamageTreasures, GoldfishCard, Strike, TriggerCause
from test_goldfish import CREATURE, FOREST, make, repeat
from test_goldfish_copies import (
    ENCHANTMENT,
    PARALLEL_LIVES,
    XORN,
    attack_race,
    doubler,
    landed,
    piece,
)

BEAR = replace(make("Bear", mv=2, kinds=CREATURE), power=2)
# A creature of two mana that cannot hurt anyone; a power that varies would.
IDLE = replace(make("Idle", mv=2, kinds=CREATURE), power=0)


def striker(
    scope: DamageScope = DamageScope.ANY,
    treasures: int = 1,
    *,
    power: int | None = 2,
    strike: Strike = Strike.REGULAR,
    types: frozenset[str] = frozenset(),
) -> GoldfishCard:
    """A creature that makes Treasures as creatures deal combat damage, cast on turn one for a single mana."""
    return replace(
        make("Striker", mv=1, kinds=CREATURE),
        on_damage=DamageTreasures(scope, treasures, types),
        power=power,
        strike=strike,
    )


def silent(scope: DamageScope = DamageScope.ANY) -> GoldfishCard:
    """The same creature with the trigger taken away."""
    return replace(striker(scope), on_damage=None)


@pytest.mark.parametrize("scope", list(DamageScope), ids=lambda s: s.name)
def test_a_creature_that_deals_combat_damage_makes_a_treasure_each_attack(scope: DamageScope) -> None:
    # It attacks on turns two and three, with 1 Treasure after each: turn three has 3 Forests and the first,
    # and after the attack the second: 5. Turn four's Forest makes 6.
    assert attack_race(IDLE, striker(scope), 6) == (4,)
    # Without the trigger 4 Forests are all there is.
    assert attack_race(IDLE, silent(scope), 6) == (0,)


def test_the_treasure_of_the_damage_is_there_for_the_second_main_phase_of_the_turn() -> None:
    # Turn two has 2 Forests; the Treasure of its attack makes the 3 for the piece, after combat.
    assert landed([*repeat(FOREST, 3), striker(), piece(3)]) == (2,)
    # Without it the third Forest of turn three pays.
    assert landed([*repeat(FOREST, 3), silent(), piece(3)]) == (3,)


def test_a_creature_with_no_power_deals_no_damage() -> None:
    assert attack_race(IDLE, striker(power=0), 6) == (0,)
    assert attack_race(IDLE, striker(power=1), 6) == (4,)
    # A power that varies is taken to deal damage.
    assert attack_race(IDLE, striker(power=None), 6) == (4,)


def test_a_creature_the_shrinking_leaves_with_no_power_deals_no_damage() -> None:
    shrinking = make("Shrinking", mv=2, kinds=ENCHANTMENT, shrink=1)
    # It comes on turn two, before the attack: a 1/- creature has no power left, a 2/- one has 1.
    assert attack_race(shrinking, striker(power=1), 6) == (0,)
    assert attack_race(shrinking, striker(power=2), 6) == (4,)


def test_a_creatures_own_damage_alone_sets_off_its_trigger() -> None:
    # The Bear comes on turn two and deals damage from turn three; the carrier, of power 0, never does. The
    # carrier's trigger for its own damage makes nothing, so the 4 waits for turn four's Forest.
    own = striker(DamageScope.SELF, power=0)
    assert landed([*repeat(FOREST, 4), own, BEAR, piece(4)]) == (4,)
    # For one or more creatures the Bear's damage on turn three makes 1 Treasure: 3 Forests and it are 4.
    assert landed([*repeat(FOREST, 4), striker(DamageScope.ANY, power=0), BEAR, piece(4)]) == (3,)


def test_one_or_more_creatures_make_one_trigger_where_each_creature_makes_its_own() -> None:
    # The Bear comes on turn two, so both attack on turn three; turn two's attack of the striker alone made 1
    # Treasure. Turn three has 3 Forests and it, and after the attack ANY makes 1 more (5) and EACH 2 (6).
    assert landed([*repeat(FOREST, 4), striker(DamageScope.ANY), BEAR, piece(6)]) == (4,)
    assert landed([*repeat(FOREST, 4), striker(DamageScope.EACH), BEAR, piece(6)]) == (3,)


def test_only_the_creatures_of_the_types_named_count() -> None:
    ninja = frozenset({"ninja"})
    bear = replace(BEAR, subtypes=frozenset({"bear"}))
    # The carrier has no power and the Bear is no Ninja: no damage of a Ninja, so turn four's Forest pays.
    named = striker(power=0, types=ninja)
    assert landed([*repeat(FOREST, 4), named, bear, piece(4)]) == (4,)
    # A Ninja Bear deals damage on turn three: 3 Forests and the Treasure after the attack are 4.
    assert landed([*repeat(FOREST, 4), named, replace(bear, subtypes=ninja), piece(4)]) == (3,)


def test_double_strike_deals_damage_in_two_steps_and_makes_two_triggers() -> None:
    # Each attack makes 2 Treasures: turn three has 3 Forests and 2, and after the attack 7, for the 6.
    assert attack_race(IDLE, striker(strike=Strike.DOUBLE), 6) == (3,)
    assert attack_race(IDLE, striker(strike=Strike.REGULAR), 6) == (4,)
    # Each creature makes its trigger once for each step it deals damage in.
    assert attack_race(IDLE, striker(DamageScope.EACH, strike=Strike.DOUBLE), 6) == (3,)


def test_a_first_strike_creature_and_a_regular_one_deal_damage_in_two_steps() -> None:
    # Turn two's attack of the striker alone is 1 step and 1 Treasure. Both attack on turn three: the steps
    # make 2 more, so 3 Forests and 3 Treasures are 6; one step of regular creatures would make 5.
    assert landed([*repeat(FOREST, 4), striker(strike=Strike.FIRST), BEAR, piece(6)]) == (3,)
    assert landed([*repeat(FOREST, 4), striker(), BEAR, piece(6)]) == (4,)


def test_the_treasures_of_a_damage_trigger_are_made_more_by_the_permanents_that_make_more() -> None:
    # Xorn makes each trigger's Treasure 2, as it does for a creature that makes one as it attacks.
    assert attack_race(XORN, striker(), 6) == (3,)
    # Parallel Lives makes 2 Treasures 4: turn three has 3 Forests and 4, and 4 more after the attack.
    assert attack_race(PARALLEL_LIVES, striker(treasures=2), 10) == (3,)
    assert attack_race(IDLE, striker(treasures=2), 10) == (4,)


def test_a_doubler_of_any_trigger_repeats_the_damage_trigger_and_one_of_attacks_does_not() -> None:
    assert attack_race(doubler(TriggerCause.ANY, mv=2), striker(), 6) == (3,)
    assert attack_race(doubler(TriggerCause.ATTACKING, mv=2), striker(), 6) == (4,)
