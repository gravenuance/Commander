"""Doublers and copy effects in the goldfish game (ADR 0054 "Doublers and copies"): hand-built decks whose
turns are worked out by hand.

A deck of seven cards or fewer is dealt whole into the opening hand, whatever the shuffle, so its game is the
same each time. Each test pairs the card under test with a control that differs by that card alone."""

from collections.abc import Sequence
from dataclasses import replace

import pytest

from commander.goldfish import chosen_creature_type, compare
from commander.goldfish_cards import (
    CHANGELING,
    AbilityCopy,
    ActivatedTutor,
    CardKind,
    CopyEffect,
    CopyKind,
    CopyOf,
    Draw,
    GoldfishCard,
    LandDestination,
    LandRamp,
    ManaAbility,
    TriggerCause,
    TriggerDoubler,
    Tutor,
    TutorDestination,
)
from test_goldfish import (
    ARTIFACT,
    CREATURE,
    FOREST,
    VANILLA,
    combo_of,
    land,
    make,
    only_outcome,
    repeat,
    run,
)

ENCHANTMENT = frozenset({CardKind.ENCHANTMENT})
ARTIFACT_CREATURE = frozenset({CardKind.ARTIFACT, CardKind.CREATURE})


def piece(mv: int) -> GoldfishCard:
    return make("Piece", mv=mv, kinds=CREATURE, combo=True)


def landed(slots: Sequence[GoldfishCard], *, commanders: Sequence[GoldfishCard] = ()) -> tuple[int, ...]:
    """The turn the one combo, of the card named Piece, lands in the one game a deck of seven plays."""
    the_piece = next(card for card in slots if card.name == "Piece")
    return only_outcome(run(slots, commanders=commanders, combos=(combo_of(the_piece),))).combo_turns


def raider(
    treasures: int = 1,
    *,
    power: int | None = None,
    subtypes: frozenset[str] = frozenset(),
    trigger_doubler: TriggerDoubler | None = None,
) -> GoldfishCard:
    """A creature that makes Treasures as it attacks, cast on turn one for a single mana."""
    return replace(
        make("Raider", mv=1, kinds=CREATURE),
        treasures_on_attack=treasures,
        power=power,
        subtypes=subtypes,
        trigger_doubler=trigger_doubler,
    )


def barbarian(kinds: frozenset[CardKind] = CREATURE) -> GoldfishCard:
    """A permanent that makes a Treasure as it enters."""
    return replace(make("Barbarian", mv=2, kinds=kinds), treasures_on_entry=1, entry_trigger=True)


def doubler(
    cause: TriggerCause, *, entering: frozenset[CardKind] = frozenset(), mv: int = 1, name: str = "Doubler"
) -> GoldfishCard:
    """An artifact of the given cost that doubles triggers."""
    return replace(make(name, mv=mv, kinds=ARTIFACT), trigger_doubler=TriggerDoubler(cause, entering))


def attack_race(support: GoldfishCard, the_raider: GoldfishCard, piece_value: int) -> tuple[int, ...]:
    """Four Forests, a raider that comes on turn one, a card of two mana that comes on turn two, and a piece
    the Treasures pay for."""
    return landed([*repeat(FOREST, 4), the_raider, support, piece(piece_value)])


def entry_race(support: GoldfishCard, maker: GoldfishCard) -> tuple[int, ...]:
    """Three Forests, a one-mana card on turn one, a Treasure maker on turn two and a piece of five mana."""
    return landed([*repeat(FOREST, 3), support, maker, piece(5)])


XORN = replace(make("Xorn", mv=2, kinds=CREATURE), treasure_extra=1)
PARALLEL_LIVES = replace(make("Parallel Lives", mv=2, kinds=ENCHANTMENT), treasure_factor=2)
INERT = make("Inert", mv=2, kinds=CREATURE)


# --- Treasures made more -----------------------------------------------------------------------------------


def test_an_additional_treasure_is_made_each_time_a_trigger_makes_one() -> None:
    # The raider attacks on turns two and three. With Xorn, which comes on turn two, each attack makes 2
    # Treasures: turn three has 3 Forests, 2 Treasures and, after the attack, 2 more: 7, for the 6.
    assert attack_race(XORN, raider(), 6) == (3,)
    # Without it 2 Treasures by the end of turn three make 5, and turn four's Forest makes 6.
    assert attack_race(INERT, raider(), 6) == (4,)


def test_twice_as_many_treasures_are_made_as_the_trigger_says_and_not_one_more() -> None:
    # An attack for 2 Treasures is 4 with Parallel Lives: after turn three, 3 Forests and 8 Treasures.
    assert attack_race(PARALLEL_LIVES, raider(2), 10) == (3,)
    # Xorn makes it 3: 3 Forests and 6 Treasures are 9, short of 10 until turn four's Forest.
    assert attack_race(XORN, raider(2), 10) == (4,)
    assert attack_race(INERT, raider(2), 10) == (4,)


def test_an_increaser_and_a_multiplier_of_one_card_add_before_they_multiply() -> None:
    # An attack for 1 Treasure is (1 + 1) * 2 = 4, and not 1 * 2 + 1: turn two has 2 Forests and 4 Treasures
    # for the 6.
    boosted = replace(raider(), treasure_extra=1, treasure_factor=2)
    assert landed([*repeat(FOREST, 4), boosted, piece(6)]) == (2,)


@pytest.mark.parametrize(
    "boost",
    [
        replace(XORN, mana_value=1),
        replace(doubler(TriggerCause.ATTACKING), mana_value=1, kinds=CREATURE),
    ],
    ids=["an increaser", "a doubler"],
)
def test_a_permanent_that_has_left_play_makes_no_more_treasures(boost: GoldfishCard) -> None:
    # Turn three casts the sacrifice spell, which takes the cheapest creature, the boost, before the raider
    # attacks for 1 Treasure: the 5 is only reached when turn four's attack makes the second.
    cull = make("Cull", mv=1, needs_sacrifice=True)
    slots = [*repeat(FOREST, 3), boost, replace(raider(), mana_value=2), cull, piece(5)]
    assert landed(slots) == (4,)
    # With nothing to sacrifice for, the boost stays and the turn three attack makes 2 Treasures.
    dud = make("Dud", mv=20, kinds=CREATURE)
    assert landed([*repeat(FOREST, 3), boost, replace(raider(), mana_value=2), dud, piece(5)]) == (3,)


def test_a_permanent_that_doubles_its_own_arrival_is_doubled_by_itself() -> None:
    selfish = replace(
        barbarian(ARTIFACT_CREATURE),
        mana_value=1,
        trigger_doubler=TriggerDoubler(TriggerCause.ENTERING, ARTIFACT_CREATURE),
    )
    # It comes on turn one with 2 Treasures, and turn two's 2 Forests and 2 Treasures pay for the 4.
    assert landed([*repeat(FOREST, 4), selfish, piece(4)]) == (2,)
    # A Treasure alone leaves turn two at 3, and turn three's Forest makes the 4.
    assert landed([*repeat(FOREST, 4), replace(selfish, trigger_doubler=None), piece(4)]) == (3,)


# --- Treasures made at the same time as another permanent comes -----------------------------------------


def test_a_doubler_of_permanents_entering_makes_the_entry_treasure_twice() -> None:
    pano = doubler(TriggerCause.ENTERING, entering=ARTIFACT_CREATURE)
    # The Treasure maker is a creature: 2 Treasures and 3 Forests pay for the 5 on turn three.
    assert entry_race(pano, barbarian()) == (3,)
    # Nothing doubles it without the doubler, and the 4 that 3 Forests and a Treasure make fall short.
    assert entry_race(INERT_ONE, barbarian()) == (0,)


def test_a_doubler_of_artifacts_and_creatures_does_not_double_an_enchantment_entering() -> None:
    pano = doubler(TriggerCause.ENTERING, entering=ARTIFACT_CREATURE)
    anything = doubler(TriggerCause.ENTERING)
    assert entry_race(pano, barbarian(ENCHANTMENT)) == (0,)
    assert entry_race(anything, barbarian(ENCHANTMENT)) == (3,)


def test_a_doubler_of_attacks_does_not_double_the_treasure_of_a_permanent_entering() -> None:
    assert entry_race(doubler(TriggerCause.ATTACKING), barbarian()) == (0,)


def test_a_doubler_of_permanents_entering_does_not_double_an_attack() -> None:
    assert attack_race(INERT, raider(), 6) == (4,)
    # The doubler is a two-mana card here, cast on turn two like Xorn.
    anything = doubler(TriggerCause.ENTERING, mv=2)
    assert attack_race(anything, raider(), 6) == (4,)
    assert attack_race(doubler(TriggerCause.ATTACKING, mv=2), raider(), 6) == (3,)


def test_each_doubler_in_play_makes_the_trigger_happen_once_more() -> None:
    first = doubler(TriggerCause.ENTERING)
    second = doubler(TriggerCause.ENTERING, mv=2, name="Second Doubler")
    maker = replace(barbarian(), mana_value=3)
    # Turn one casts one doubler, turn two the other and turn three the maker: 3 Treasures and 3 Forests are
    # the 6 of turn four. One doubler makes 2 Treasures, which is 5.
    slots = [*repeat(FOREST, 3), first, second, maker, piece(6)]
    assert landed(slots) == (4,)
    assert landed([*repeat(FOREST, 3), first, INERT, maker, piece(6)]) == (0,)


INERT_ONE = make("Inert One", mv=1, kinds=ARTIFACT)


# --- the triggers of some creatures -------------------------------------------------------------------------


def test_a_doubler_of_creatures_with_little_power_doubles_only_the_triggers_of_those() -> None:
    delney = replace(
        make("Delney", mv=2, kinds=CREATURE), trigger_doubler=TriggerDoubler(TriggerCause.ANY, max_power=2)
    )
    assert attack_race(delney, raider(power=2), 6) == (3,)
    assert attack_race(delney, raider(power=3), 6) == (4,)
    assert attack_race(delney, raider(power=None), 6) == (4,)


def test_a_doubler_that_names_no_other_creature_than_itself_doubles_its_own_trigger() -> None:
    both = raider(power=2, trigger_doubler=TriggerDoubler(TriggerCause.ANY, max_power=2))
    # Alone with four Forests and the piece: 2 Treasures on turn two, 2 more on turn three make 7 with the 3.
    assert landed([*repeat(FOREST, 4), both, piece(6)]) == (3,)


def test_a_doubler_of_another_creature_does_not_double_its_own_trigger() -> None:
    other = TriggerDoubler(TriggerCause.ANY, subtype="elemental", another=True)
    both = raider(subtypes=frozenset({"elemental"}), trigger_doubler=other)
    # One Treasure an attack: 1 and 1 are 2, and turn four's fourth Forest makes the 6.
    assert landed([*repeat(FOREST, 4), both, piece(6)]) == (4,)


def test_a_doubler_of_another_elemental_doubles_the_triggers_of_a_creature_with_that_subtype() -> None:
    twinflame = replace(
        make("Twinflame", mv=2, kinds=CREATURE),
        trigger_doubler=TriggerDoubler(TriggerCause.ANY, subtype="elemental", another=True),
    )
    elemental = frozenset({"elemental"})
    assert attack_race(twinflame, raider(subtypes=elemental), 6) == (3,)
    assert attack_race(twinflame, raider(subtypes=frozenset({"goblin"})), 6) == (4,)
    # A changeling has every creature type.
    assert attack_race(twinflame, raider(subtypes=frozenset({CHANGELING})), 6) == (3,)


MAGDA = replace(
    make("Magda", mv=1, kinds=CREATURE),
    subtypes=frozenset({"dwarf", "berserker"}),
    treasure_on_tap=frozenset({"dwarf"}),
)
THRONE = replace(
    make("Throne", mv=2, kinds=ARTIFACT_CREATURE),
    subtypes=frozenset({"golem"}),
    trigger_doubler=TriggerDoubler(TriggerCause.ANY, chosen_type=True, another=True),
)


def test_a_doubler_of_the_chosen_type_doubles_a_dwarf_that_makes_a_treasure_as_a_dwarf_taps() -> None:
    # Magda is the commander, cast on turn one. Throne names Dwarf, the commander's type, and is a Dwarf too.
    # Turn two: Magda attacks, 2 Treasures. Turn three: Magda and Throne attack, each tapping a Dwarf for 2
    # Treasures: 6 and 3 Forests, 9, for the 8.
    slots = [*repeat(FOREST, 4), THRONE, piece(8)]
    assert landed(slots, commanders=(MAGDA,)) == (3,)
    # Without the doubler Magda makes 1 Treasure an attack: 4 Forests and 4 Treasures on turn five.
    plain = replace(THRONE, trigger_doubler=None)
    assert landed([*repeat(FOREST, 4), plain, piece(8)], commanders=(MAGDA,)) == (5,)


def test_the_type_a_chosen_type_doubler_names_is_the_commanders_even_when_the_deck_has_more_dwarves() -> None:
    # Throne names Elf, the commander's type, so it is no Dwarf and Magda's trigger is not doubled: the
    # Treasures of turns four and five do not reach the 8.
    elf = replace(make("Elf Commander", mv=1, kinds=CREATURE), subtypes=frozenset({"elf"}))
    slots = [*repeat(FOREST, 4), THRONE, MAGDA, piece(8)]
    assert landed(slots, commanders=(elf,)) == (0,)


def test_the_type_a_chosen_type_doubler_names() -> None:
    def creature(*types: str) -> GoldfishCard:
        return replace(make("Card", kinds=CREATURE), subtypes=frozenset(types))

    artifact = make("Rock", kinds=ARTIFACT)
    # The commander's type that Treasures are made for as it taps.
    assert chosen_creature_type([], [MAGDA]) == "dwarf"
    # The type of the most creatures in the deck when the commander has none; the first by name on a tie.
    elf, dwarf = creature("elf"), creature("dwarf")
    assert chosen_creature_type([elf, dwarf, dwarf], [artifact]) == "dwarf"
    assert chosen_creature_type([elf, elf, dwarf], [artifact]) == "elf"
    assert chosen_creature_type([elf, dwarf], [artifact]) == "dwarf"
    # A changeling commander has every type, so none is its own.
    assert chosen_creature_type([elf, elf, dwarf], [creature(CHANGELING)]) == "elf"
    # A card that is no creature has subtypes that are no creature types.
    assert chosen_creature_type([replace(artifact, subtypes=frozenset({"equipment"}))], [artifact]) is None
    assert chosen_creature_type([], []) is None


# --- other effects of a permanent that enters ---------------------------------------------------------------


def test_a_doubled_arrival_fetches_lands_twice() -> None:
    pano = doubler(TriggerCause.ENTERING, entering=ARTIFACT_CREATURE)
    elves = replace(
        make("Elves", mv=2, kinds=CREATURE),
        land_ramp=LandRamp(1, basic_only=False, destination=LandDestination.UNTAPPED),
        entry_trigger=True,
    )
    library = repeat(FOREST, 40)
    # Turn two casts the Elves and puts two Forests onto the battlefield: 2 lands and 2 fetched.
    doubled = only_outcome(run(library, commanders=(pano, elves)))
    assert doubled.mana == (1, 4, 5, 6, 7)
    plain = only_outcome(run(library, commanders=(replace(pano, trigger_doubler=None), elves)))
    assert plain.mana == (1, 3, 4, 5, 6)


def test_a_doubled_arrival_draws_twice() -> None:
    pano = doubler(TriggerCause.ENTERING, entering=ARTIFACT_CREATURE)
    drifter = replace(make("Drifter", mv=2, kinds=CREATURE), draw=Draw(2, engine=False), entry_trigger=True)
    library = repeat(FOREST, 40)
    # The seven of the hand, a card a turn for five turns and the Drifter's cards.
    assert only_outcome(run(library, commanders=(pano, drifter))).shuffle_depths == (16,)
    plain = replace(pano, trigger_doubler=None)
    assert only_outcome(run(library, commanders=(plain, drifter))).shuffle_depths == (14,)


def test_a_spell_that_draws_is_not_a_trigger_so_a_doubler_leaves_it_alone() -> None:
    pano = doubler(TriggerCause.ENTERING)
    harmonize = make("Harmonize", mv=2, draw=Draw(2, engine=False))
    assert only_outcome(run(repeat(FOREST, 40), commanders=(pano, harmonize))).shuffle_depths == (14,)


def test_a_doubled_search_fetches_a_second_card() -> None:
    pano = doubler(TriggerCause.ENTERING, entering=ARTIFACT_CREATURE)
    recruiter = replace(
        make("Recruiter", mv=2, kinds=CREATURE),
        tutor=Tutor(frozenset({CardKind.CREATURE}), TutorDestination.HAND),
        entry_trigger=True,
    )
    pieces = [make(name, mv=1, kinds=CREATURE, combo=True) for name in ("One", "Two", "Three")]
    combo = combo_of(*pieces)
    library = [*repeat(FOREST, 9), *pieces]

    def turns(commander: GoldfishCard) -> list[int]:
        report = run(library, commanders=(commander, recruiter), combos=(combo,), games=300, seed=5)
        return [o.combo_turns[0] or 99 for o in report.outcomes]

    doubled = turns(pano)
    plain = turns(replace(pano, trigger_doubler=None))
    # Two of the three pieces are fetched at once, so no game lands later and some land earlier.
    assert all(a <= b for a, b in zip(doubled, plain, strict=True))
    assert any(a < b for a, b in zip(doubled, plain, strict=True))


# --- becoming a copy of a creature until end of turn -------------------------------------------------------

LEGENDARY_MAGDA = replace(MAGDA, legendary=True)
GOGO = replace(
    make("Gogo", mv=2, kinds=CREATURE),
    legendary=True,
    copy=CopyEffect(CopyKind.COMBAT, CopyOf.CREATURE, haste=True, legend_exempt=True),
)
VAULT = make("Vault", mv=1, kinds=ARTIFACT, mana=ManaAbility(4, frozenset("C"), delayed=False, ritual=False))
HALL = replace(
    land("Hall", "C"), copy=CopyEffect(CopyKind.ACTIVATED, CopyOf.CREATURE, cost=5, legend_exempt=True)
)


def test_a_creature_that_becomes_a_copy_in_combat_doubles_the_dwarves_that_tap() -> None:
    # Magda comes on turn one and Gogo on turn two, and in combat Gogo becomes a copy of Magda, whatever it
    # is that is legendary. Two Dwarves attack and two Magdas make a Treasure for each: 4, and turn three
    # has 3 Forests for the 7.
    slots = [*repeat(FOREST, 4), GOGO, piece(7)]
    assert landed(slots, commanders=(LEGENDARY_MAGDA,)) == (3,)
    # Without Gogo's copy Magda attacks alone for 1 a turn: turn four has 4 Forests and 2 Treasures, and 3
    # after its attack, for the 7.
    assert landed(
        [*repeat(FOREST, 4), replace(GOGO, copy=None), piece(7)], commanders=(LEGENDARY_MAGDA,)
    ) == (4,)


def test_a_copy_that_keeps_the_legend_rule_copies_no_legendary_creature() -> None:
    kept = replace(GOGO, copy=CopyEffect(CopyKind.COMBAT, CopyOf.CREATURE, haste=True))
    assert landed([*repeat(FOREST, 4), kept, piece(7)], commanders=(LEGENDARY_MAGDA,)) == (4,)
    # It copies the creature that is not legendary, which Magda's Dwarves make more of.
    assert landed([*repeat(FOREST, 4), kept, piece(7)], commanders=(MAGDA,)) == (3,)


def test_the_creature_a_combat_copy_copies_gains_haste_and_attacks_the_turn_it_comes() -> None:
    gogo = replace(GOGO, mana_value=1)
    magda = replace(LEGENDARY_MAGDA, mana_value=2)
    # Gogo comes on turn one and Magda on turn two. Both attack on turn two, Gogo as a copy of Magda: 4
    # Treasures, the 4 that the piece costs.
    assert landed([*repeat(FOREST, 4), magda, gogo, piece(4)]) == (2,)
    # Without the copy Magda first attacks on turn three, for 3 Forests and 1 Treasure.
    assert landed([*repeat(FOREST, 4), magda, replace(gogo, copy=None), piece(4)]) == (3,)


def test_a_land_that_becomes_a_copy_for_five_mana_needs_five_mana_besides_its_own() -> None:
    # The Vault pays for itself with the Hall's mana on turn one, and Magda is cast. Turn two has the Hall, a
    # Forest and the Vault's 4: 6 mana, the Hall's own one that cannot be spent on its becoming a copy. It
    # becomes a copy of Magda and attacks with her: 4 Treasures. Turn three has 7 mana and the 11.
    slots = [HALL, *repeat(FOREST, 3), VAULT, piece(11)]
    assert landed(slots, commanders=(LEGENDARY_MAGDA,)) == (3,)
    plain = replace(HALL, copy=None)
    assert landed([plain, *repeat(FOREST, 3), VAULT, piece(11)], commanders=(LEGENDARY_MAGDA,)) == (4,)
    # A Vault of 3 leaves 4 mana besides the Hall's own on turn two, so the Hall waits for turn three;
    # spending its own mana on turn two would make 4 Treasures that land the 10 on turn three.
    small = replace(VAULT, mana=ManaAbility(3, frozenset("C"), delayed=False, ritual=False))
    assert landed([HALL, *repeat(FOREST, 3), small, piece(10)], commanders=(LEGENDARY_MAGDA,)) == (4,)


CURSED_MIRROR = replace(
    make("Mirror", mv=2, kinds=ARTIFACT, mana=ManaAbility(1, frozenset("C"), delayed=False, ritual=False)),
    copy=CopyEffect(CopyKind.TURN, CopyOf.CREATURE, haste=True),
)


def test_an_artifact_that_enters_as_a_copy_of_a_creature_attacks_with_haste_and_makes_no_mana() -> None:
    # The raider comes on turn one. The Mirror comes on turn two as a copy of it, with haste, and both attack
    # for 2 Treasures: 4, and the 4 that the piece costs. The Mirror makes no mana, being a creature.
    two = raider(2)
    assert landed([*repeat(FOREST, 3), two, CURSED_MIRROR, piece(4)]) == (2,)
    # As a plain artifact it makes 1 mana, but the raider alone makes 2 Treasures: 3 on turn two. Turn three
    # has 3 Forests and the Mirror's mana.
    assert landed([*repeat(FOREST, 3), two, replace(CURSED_MIRROR, copy=None), piece(4)]) == (3,)
    # A piece of 5 shows that the copy's mana is not made on turn two: its 4 Treasures wait for turn three.
    assert landed([*repeat(FOREST, 3), two, CURSED_MIRROR, piece(5)]) == (3,)


def test_an_artifact_that_enters_as_a_copy_makes_its_mana_again_the_next_turn() -> None:
    # The copy ends with turn two. Turn three has 3 Forests and the Mirror's 1, the 4 Treasures of turn two
    # and the raider's 2 on turn three: 10, for the 10. Without the Mirror's mana it is 9.
    assert landed([*repeat(FOREST, 3), raider(2), CURSED_MIRROR, piece(10)]) == (3,)


def test_an_artifact_that_enters_as_a_copy_copies_no_legendary_creature() -> None:
    legendary = replace(raider(2), legendary=True)
    assert landed([*repeat(FOREST, 3), legendary, CURSED_MIRROR, piece(4)]) == (3,)


def test_an_artifact_that_makes_more_mana_than_the_copy_gains_is_not_a_copy() -> None:
    # The Mirror makes 2 mana and a copy of the raider gains 1 Treasure: it stays an artifact, and turn two
    # has its 2 mana and the raider's Treasure for the 3.
    rock = replace(CURSED_MIRROR, mana=ManaAbility(2, frozenset("C"), delayed=False, ritual=False))
    assert landed([*repeat(FOREST, 3), raider(1), rock, piece(3)]) == (2,)


# --- clones that stay copies --------------------------------------------------------------------------------


def rock(name: str, amount: int, *, mv: int = 1, legendary: bool = False) -> GoldfishCard:
    mana = ManaAbility(amount, frozenset("C"), delayed=False, ritual=False)
    return replace(make(name, mv=mv, kinds=ARTIFACT, mana=mana), legendary=legendary)


def elf(name: str, amount: int, *, mv: int = 1, legendary: bool = False) -> GoldfishCard:
    mana = ManaAbility(amount, frozenset("G"), delayed=True, ritual=False)
    return replace(make(name, mv=mv, kinds=CREATURE, mana=mana), legendary=legendary)


def clone(name: str, of: CopyOf, *, mv: int, kinds: frozenset[CardKind], **effect: bool) -> GoldfishCard:
    return replace(make(name, mv=mv, kinds=kinds), copy=CopyEffect(CopyKind.ENTERS, of, **effect))


GIANT = make("Giant", mv=20, kinds=CREATURE)


def mana_of(slots: Sequence[GoldfishCard], *, bracket: int = 5) -> tuple[int, ...]:
    return only_outcome(run(slots, bracket=bracket)).mana


def test_an_artifact_that_enters_as_a_copy_is_a_copy_of_the_artifact_that_makes_the_most_mana() -> None:
    steel = clone("Steel", CopyOf.ARTIFACT, mv=3, kinds=ARTIFACT)
    slots = [*repeat(FOREST, 4), rock("Ring", 2), steel, GIANT]
    # The Ring comes on turn one: 1 Forest and 2. Steel comes on turn two as a second Ring, 2 more at once.
    # The fourth Forest is the last land.
    assert mana_of(slots) == (3, 6, 7, 8, 8)
    # As a plain artifact it adds nothing.
    assert mana_of([*repeat(FOREST, 4), rock("Ring", 2), replace(steel, copy=None), GIANT]) == (3, 4, 5, 6, 6)


def test_a_copy_is_made_only_when_what_it_makes_in_the_turns_left_is_more_than_it_costs() -> None:
    stone = rock("Stone", 1)
    # Steel is first cast on turn two with 3 mana, and a Stone makes 1 a turn for turns two to five: 4.
    cheap = clone("Steel", CopyOf.ARTIFACT, mv=3, kinds=ARTIFACT)
    assert mana_of([*repeat(FOREST, 4), stone, cheap, GIANT]) == (2, 4, 5, 6, 6)
    # With 4 mana it is first cast on turn three, and the Stone makes 1 a turn for turns three to five: 3.
    dear = clone("Steel", CopyOf.ARTIFACT, mv=4, kinds=ARTIFACT)
    assert mana_of([*repeat(FOREST, 4), stone, dear, GIANT]) == (2, 3, 4, 5, 5)


def test_a_copy_of_a_legendary_permanent_is_not_made() -> None:
    steel = clone("Steel", CopyOf.ARTIFACT, mv=3, kinds=ARTIFACT)
    assert mana_of([*repeat(FOREST, 4), rock("Ring", 2, legendary=True), steel, GIANT]) == (3, 4, 5, 6, 6)


def test_a_creature_that_enters_as_a_copy_makes_its_mana_from_the_turn_after() -> None:
    image = clone("Image", CopyOf.CREATURE, mv=2, kinds=CREATURE)
    # The Elf comes on turn one and Image on turn two as a second Elf; both make their mana from turn three.
    assert mana_of([*repeat(FOREST, 4), elf("Elf", 1), image, GIANT]) == (1, 3, 5, 6, 6)
    assert mana_of([*repeat(FOREST, 4), elf("Elf", 1, legendary=True), image, GIANT]) == (1, 3, 4, 5, 5)


def test_a_creature_that_enters_as_a_copy_copies_the_creature_that_makes_the_most_mana() -> None:
    image = clone("Image", CopyOf.CREATURE, mv=3, kinds=CREATURE)
    slots = [*repeat(FOREST, 3), elf("Small Elf", 1), elf("Big Elf", 2, mv=2), image]
    # Turn three has 3 Forests, 1 and 2: 6, and Image copies the elf of 2 for 8 from turn four.
    assert mana_of(slots) == (1, 3, 6, 8, 8)


def test_a_clone_that_copies_for_the_mana_it_spends_can_copy_a_creature_of_that_mana_value() -> None:
    bird = clone("Bird", CopyOf.CREATURE, mv=1, kinds=CREATURE, mana_spent=True)
    slots = [*repeat(FOREST, 4), elf("Big Elf", 3, mv=3), bird, GIANT]
    # Bracket 4 plays six turns. The Big Elf comes on turn three. Turn four has 4 Forests and its 3: 7, and
    # the bird spends 3 as a second Big Elf, whose 3 come from turn five.
    assert mana_of(slots, bracket=4) == (1, 2, 3, 7, 10, 10)
    # As a plain creature it adds nothing.
    plain = [*repeat(FOREST, 4), elf("Big Elf", 3, mv=3), replace(bird, copy=None), GIANT]
    assert mana_of(plain, bracket=4) == (1, 2, 3, 7, 7, 7)
    # The 3 it spends leave 4 of turn four's 7, short of a commander of 5, which comes on turn five.
    boss = make("Boss", mv=5, kinds=CREATURE)
    assert only_outcome(run(slots, commanders=(boss,), bracket=4)).commander_turns == (5,)


TOMB = land("Tomb", "C", amount=2)
VESUVA = replace(land("Vesuva", ""), copy=CopyEffect(CopyKind.ENTERS, CopyOf.LAND, tapped=True))


def test_a_land_that_enters_tapped_as_a_copy_copies_the_land_that_makes_the_most_mana() -> None:
    slots = [TOMB, FOREST, VESUVA, *repeat(GIANT, 4)]
    # The Tomb is played on turn one for 2. Vesuva comes on turn two, tapped, as a second Tomb: its 2 come on
    # turn three, with the Forest's 1.
    assert mana_of(slots) == (2, 2, 5, 5, 5)
    # A legendary land is not copied. Turn two plays the Forest, and Vesuva comes on turn three, tapped, as a
    # second Forest.
    assert mana_of([replace(TOMB, legendary=True), FOREST, VESUVA, *repeat(GIANT, 4)]) == (2, 3, 3, 4, 4)
    # With no copy the land makes 1, untapped, as it always has.
    assert mana_of([TOMB, FOREST, replace(VESUVA, copy=None), *repeat(GIANT, 4)]) == (2, 3, 4, 4, 4)
    # With the Tomb and a Forest out it copies the Tomb. A spell of 3 on turn two needs the Forest untapped.
    spell = make("Spell", mv=3, kinds=CREATURE)
    assert mana_of([FOREST, TOMB, VESUVA, spell, *repeat(GIANT, 3)]) == (2, 3, 3, 5, 5)


# --- a copy of an ability -----------------------------------------------------------------------------------

CAMERA = replace(make("Camera", mv=1, kinds=ARTIFACT), ability_copy=AbilityCopy(cost=2, counters=3))
PLAIN_CAMERA = replace(CAMERA, ability_copy=None)


def camera_turn(camera: GoldfishCard, mv: int) -> int:
    """The turn a piece of this mana value lands, in the games that keep their seven cards, which all agree.
    Turn one: a Forest and the Vault's 4 cast the Raider and the camera. From turn two the Vault and the
    Forests (3 at most) make 6, 7, 7 ... and the Raider makes 3 Treasures as it attacks."""
    the_piece = piece(mv)
    slots = [*repeat(FOREST, 3), VAULT, raider(3), camera, the_piece]
    outcomes = run(slots, combos=(combo_of(the_piece),), games=60).outcomes
    turns = {o.combo_turns for o in outcomes if o.hand_size == 7}
    assert len(turns) == 1
    return turns.pop()[0]


def test_a_copy_of_the_attack_trigger_is_paid_for_with_the_mana_left_over() -> None:
    # Turn two: 6 mana and 3 Treasures make 9, short of 10. With a copy, 2 of the 6 pay for 3 more Treasures
    # at once: 4 and 6 make 10.
    assert camera_turn(PLAIN_CAMERA, 10) == 3
    assert camera_turn(CAMERA, 10) == 2
    # With 11 it is turn three either way.
    assert camera_turn(CAMERA, 11) == 3


def test_a_camera_copies_an_ability_only_as_often_as_it_has_counters() -> None:
    # Without a copy, 7 mana and the 12 Treasures of four attacks make 19 on turn five, short of 20.
    assert camera_turn(PLAIN_CAMERA, 20) == 0
    # A copy each turn doubles the Treasures: turn four has 7 - 2 mana and 18 Treasures, 23 (turn three 17).
    # One copy leaves turn four 7 and 12, 19, and turn five 7 and 15, 22.
    assert camera_turn(CAMERA, 20) == 4
    assert camera_turn(replace(CAMERA, ability_copy=AbilityCopy(cost=2, counters=1)), 20) == 5


def test_a_copy_of_a_trigger_that_makes_no_more_than_it_costs_is_not_made() -> None:
    # The Raider's 2 Treasures cost 2 to copy: there is nothing to gain, so no counter is spent and turn two's
    # 6 mana and 2 Treasures stay 8.
    the_piece = piece(8)
    slots = [*repeat(FOREST, 3), VAULT, raider(2), CAMERA, the_piece]
    outcomes = run(slots, combos=(combo_of(the_piece),), games=60).outcomes
    assert {o.combo_turns for o in outcomes if o.hand_size == 7} == {(2,)}


def search_copy(camera: GoldfishCard, relics: int, *, diggers: int = 1) -> list[int]:
    """The turn the combo of the relics lands in each of 3000 games, or 99. The diggers are the commanders:
    each searches once for a relic, which comes onto the battlefield. A land of the deck makes 7 mana."""
    finds = Tutor(frozenset({CardKind.ARTIFACT}), TutorDestination.BATTLEFIELD)
    commanders = tuple(
        replace(make(f"Digger {i}", mv=1, kinds=ARTIFACT), activated_tutor=ActivatedTutor(finds, loyalty=1))
        for i in range(diggers)
    )
    pieces = [make(f"Relic {i}", mv=20, kinds=ARTIFACT, combo=True) for i in range(relics)]
    big = land("Big", "C", amount=7)
    slots = [big, big, camera, *pieces, *repeat(GIANT, 24)]
    report = run(slots, commanders=commanders, combos=(combo_of(*pieces),), games=3000, seed=11)
    return [outcome.combo_turns[0] or 99 for outcome in report.outcomes]


def test_a_search_is_copied_for_a_second_card_when_the_mana_for_it_is_left_over() -> None:
    # One search finds one relic, and a relic in the hand is too dear to cast: two relics never land alone.
    assert set(search_copy(PLAIN_CAMERA, 2)) == {99}
    with_camera = search_copy(CAMERA, 2)
    assert any(turn < 99 for turn in with_camera)
    # The camera and the search are on the battlefield when the second relic is fetched, on turn one at the
    # earliest.
    assert min(with_camera) == 1


def test_a_camera_is_tapped_for_one_copy_each_turn() -> None:
    # Two diggers and a camera find 3 relics a turn, not 4, on the turn all three are in play.
    assert min(search_copy(CAMERA, 3, diggers=2)) == 1
    assert min(search_copy(CAMERA, 4, diggers=2)) > 1


# --- a swap -------------------------------------------------------------------------------------------------


PLAYS: dict[str, object] = {
    "treasures_on_entry": 1,
    "treasures_on_attack": 1,
    "treasure_on_tap": frozenset({"dwarf"}),
    "treasure_extra": 1,
    "treasure_factor": 2,
    "trigger_doubler": TriggerDoubler(TriggerCause.ENTERING),
    "entry_trigger": True,
    "activated_tutor": ActivatedTutor(Tutor(frozenset(), TutorDestination.HAND), treasures=1),
    "dies_tutor": Tutor(frozenset(), TutorDestination.HAND),
    "free_outlet": True,
    "tap_ability": True,
}


@pytest.mark.parametrize("field", PLAYS)
def test_a_swap_for_a_card_that_does_more_than_its_cost_is_played_and_not_taken_for_a_plain_spell(
    field: str,
) -> None:
    slots = [*repeat(FOREST, 6), VANILLA]
    base = run(slots, games=20)
    incoming = replace(VANILLA, name="Incoming", **{field: PLAYS[field]})
    result = compare(base, slots, [(VANILLA, incoming)], (), (), average_mana_value=2.0, seed=7)
    # Dealt whole, every game of the deck draws the card cut.
    assert len(result.replayed) == 20


def test_a_swap_for_a_plain_spell_of_the_same_cost_is_not_played() -> None:
    slots = [*repeat(FOREST, 6), VANILLA]
    base = run(slots, games=20)
    incoming = replace(VANILLA, name="Incoming")
    result = compare(base, slots, [(VANILLA, incoming)], (), (), average_mana_value=2.0, seed=7)
    assert result.replayed == ()
