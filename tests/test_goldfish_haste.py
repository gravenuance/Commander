"""Creatures with haste in the goldfish game (ADR 0054): one that comes before combat attacks, and taps for
mana, the turn it enters. Hand-built decks whose turns are worked out by hand.

A deck of seven cards or fewer is dealt whole into the opening hand, whatever the shuffle, so its game is the
same each time."""

from dataclasses import replace

from commander.goldfish_cards import GoldfishCard, ManaAbility, Strike
from test_goldfish import CREATURE, FOREST, VANILLA, make, only_outcome, repeat, run
from test_goldfish_copies import MAGDA, attack_race, landed, piece, raider
from test_goldfish_damage import IDLE, striker


def hasty(card: GoldfishCard) -> GoldfishCard:
    return replace(card, haste=True)


def test_a_haste_creature_attacks_the_turn_it_comes_for_its_own_treasure() -> None:
    # The raider comes on turn one and attacks from it: 1 Treasure a turn, so turn three has 3 Forests and 2
    # Treasures, and after the attack 6 for the piece.
    assert attack_race(IDLE, hasty(raider()), 6) == (3,)
    # Without haste it attacks from turn two: 3 Forests and 1 Treasure are 4, and 5 after the attack; turn
    # four's Forest and the 2 Treasures make the 6.
    assert attack_race(IDLE, raider(), 6) == (4,)


def test_a_haste_creature_deals_combat_damage_the_turn_it_comes() -> None:
    assert attack_race(IDLE, hasty(striker()), 6) == (3,)
    assert attack_race(IDLE, striker(), 6) == (4,)


def test_a_haste_creature_with_double_strike_makes_two_treasures_the_turn_it_comes() -> None:
    # Knuckles the Echidna's keywords: 2 Treasures an attack from turn one, so 4 by the end of turn two. Turn
    # three has 3 Forests and 4 Treasures, and the attack makes 2 more: 9 for the piece.
    knuckles = hasty(striker(strike=Strike.DOUBLE))
    assert attack_race(IDLE, knuckles, 9) == (3,)
    # Attacking from turn two it has 2 by the end of turn three: 7, and 3 Forests and 4 on turn four make 8,
    # until the attack of turn four makes 2 more.
    assert attack_race(IDLE, striker(strike=Strike.DOUBLE), 9) == (4,)


def test_a_haste_commander_taps_for_magdas_treasure_the_turn_it_comes() -> None:
    # Magda is cast on turn one and makes a Treasure for each Dwarf that becomes tapped, itself: 1 a turn.
    # Turn three has 3 Forests and 2 Treasures, and 6 after the attack.
    slots = [*repeat(FOREST, 4), piece(6)]
    assert landed(slots, commanders=(hasty(MAGDA),)) == (3,)
    # Attacking from turn two, the 6 is there on turn four: 4 Forests and 2 Treasures.
    assert landed(slots, commanders=(MAGDA,)) == (4,)


def test_a_haste_creature_that_taps_for_mana_gives_it_the_turn_it_comes() -> None:
    elf = make(
        "Elf",
        mv=1,
        kinds=CREATURE,
        mana=ManaAbility(1, frozenset("G"), delayed=True, ritual=False),
    )
    slots = [FOREST, FOREST, elf, *repeat(VANILLA, 4)]
    assert only_outcome(run(slots)).mana == (1, 3, 3, 3, 3)
    # The Forest pays for the elf on turn one, which taps at once: 2 and then 3 with the second Forest.
    assert only_outcome(run([FOREST, FOREST, hasty(elf), *repeat(VANILLA, 4)])).mana == (2, 3, 3, 3, 3)


def test_a_haste_creature_tapped_for_its_own_mana_does_not_attack() -> None:
    mana = ManaAbility(1, frozenset("G"), delayed=True, ritual=False)
    maker = hasty(replace(make("Maker", mv=1, kinds=CREATURE, mana=mana), treasures_on_attack=1))
    # Three Forests and the maker's mana are 4 on turn three; the Treasures an attack would make are not
    # there for the 5, which never comes.
    assert landed([FOREST, FOREST, FOREST, maker, piece(5)]) == (0,)
