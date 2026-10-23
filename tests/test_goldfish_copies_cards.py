"""How the goldfish reads doublers and copy effects (ADR 0054); rules texts are real, from the card data."""

import pytest

from commander.goldfish_cards import (
    AbilityCopy,
    CardKind,
    CopyEffect,
    CopyKind,
    CopyOf,
    TriggerCause,
    TriggerDoubler,
)
from test_goldfish_cards import (
    CULTIVATE,
    DOCKSIDE,
    FOREST,
    GRIZZLY_BEARS,
    IMPERIAL_RECRUITER,
    JAYEMDAE_TOME,
    LANNERY_STORM,
    LLANOWAR,
    MULLDRIFTER,
    PLUNDERING_BARBARIAN,
    PROTEAN_HULK,
    RAMP,
    WAYFARERS_BAUBLE,
    WOOD_ELVES,
    Real,
    build,
    incoming,
    one,
    real,
)

# --- doublers of Treasures and of triggers ------------------------------------------------------------

XORN = real(
    "Xorn",
    "{2}{R}",
    3,
    "Creature — Elemental",
    "If you would create one or more Treasure tokens, instead create those tokens plus an additional "
    "Treasure token.",
    identity="R",
    tags=("token-increaser",),
)
PARALLEL_LIVES = real(
    "Parallel Lives",
    "{3}{G}",
    4,
    "Enchantment",
    "If an effect would create one or more tokens under your control, it creates twice that many of those "
    "tokens instead.",
    identity="G",
    tags=("token-doubler",),
)
MONDRAK = real(
    "Mondrak, Glory Dominus",
    "{2}{W}{W}",
    4,
    "Legendary Creature — Phyrexian Horror",
    "If one or more tokens would be created under your control, twice that many of those tokens are created "
    "instead.\n{1}{W/P}{W/P}, Sacrifice two other artifacts and/or creatures: Put an indestructible counter "
    "on Mondrak. ({W/P} can be paid with either {W} or 2 life.)",
    identity="W",
    tags=("token-doubler",),
)
OJER_TAQ = real(
    "Ojer Taq, Deepest Foundation",
    "{4}{W}{W}",
    6,
    "Legendary Creature — God",
    "Vigilance\nIf one or more creature tokens would be created under your control, three times that many of "
    "those tokens are created instead.\nWhen Ojer Taq dies, return it to the battlefield tapped and "
    "transformed under its owner's control.",
    identity="W",
    tags=("token-doubler",),
)
ACADEMY_MANUFACTOR = real(
    "Academy Manufactor",
    "{3}",
    3,
    "Artifact Creature — Assembly-Worker",
    "If you would create a Clue, Food, or Treasure token, instead create one of each.",
    tags=("token-increaser", "token-replacer"),
)
PANHARMONICON = real(
    "Panharmonicon",
    "{4}",
    4,
    "Artifact",
    "If an artifact or creature entering causes a triggered ability of a permanent you control to trigger, "
    "that ability triggers an additional time.",
    tags=("trigger-doubler",),
)
YAROK = real(
    "Yarok, the Desecrated",
    "{2}{B}{G}{U}",
    5,
    "Legendary Creature — Elemental Horror",
    "Deathtouch, lifelink\nIf a permanent entering causes a triggered ability of a permanent you control to "
    "trigger, that ability triggers an additional time.",
    identity="BGU",
    tags=("trigger-doubler",),
)
WINDCRAG_SIEGE = real(
    "Windcrag Siege",
    "{1}{R}{W}",
    3,
    "Enchantment",
    "As this enchantment enters, choose Mardu or Jeskai.\n• Mardu — If a creature attacking causes a "
    "triggered ability of a permanent you control to trigger, that ability triggers an additional time.\n"
    "• Jeskai — At the beginning of your upkeep, create a 1/1 red Goblin creature token. It gains lifelink "
    "and haste until end of turn.",
    identity="RW",
    tags=("trigger-doubler",),
)
ROAMING_THRONE = real(
    "Roaming Throne",
    "{4}",
    4,
    "Artifact Creature — Golem",
    "Ward {2}\nAs this creature enters, choose a creature type.\nThis creature is the chosen type in "
    "addition to its other types.\nIf a triggered ability of another creature you control of the chosen "
    "type triggers, it triggers an additional time.",
    tags=("trigger-doubler",),
)
DELNEY = real(
    "Delney, Streetwise Lookout",
    "{2}{W}",
    3,
    "Legendary Creature — Human Scout",
    "Creatures you control with power 2 or less can't be blocked by creatures with power 3 or greater.\nIf a "
    "triggered ability of a creature you control with power 2 or less triggers, that ability triggers an "
    "additional time.",
    identity="W",
    power="2",
    tags=("trigger-doubler",),
)
TWINFLAME_TRAVELERS = real(
    "Twinflame Travelers",
    "{2}{U}{R}",
    4,
    "Creature — Elemental Sorcerer",
    "Flying\nIf a triggered ability of another Elemental you control triggers, it triggers an additional "
    "time.",
    identity="UR",
    tags=("trigger-doubler",),
)


def test_an_additional_treasure_for_each_creation_is_read() -> None:
    xorn = one(XORN)
    assert (xorn.treasure_extra, xorn.treasure_factor) == (1, 1)
    assert xorn.modelled
    assert not xorn.legendary


@pytest.mark.parametrize("doubler", [PARALLEL_LIVES, MONDRAK], ids=lambda c: c.mana.name)
def test_twice_as_many_tokens_is_read_as_twice_the_treasures(doubler: Real) -> None:
    card = one(doubler)
    assert (card.treasure_extra, card.treasure_factor) == (0, 2)
    assert card.modelled


@pytest.mark.parametrize("neutral", [OJER_TAQ, ACADEMY_MANUFACTOR], ids=lambda c: c.mana.name)
def test_tripled_creature_tokens_and_one_of_each_token_leave_the_treasures_as_they_are(neutral: Real) -> None:
    card = one(neutral)
    assert (card.treasure_extra, card.treasure_factor) == (0, 1)
    assert card.modelled


@pytest.mark.parametrize(
    ("card", "expected"),
    [
        (
            PANHARMONICON,
            TriggerDoubler(TriggerCause.ENTERING, frozenset({CardKind.ARTIFACT, CardKind.CREATURE})),
        ),
        (YAROK, TriggerDoubler(TriggerCause.ENTERING)),
        (WINDCRAG_SIEGE, TriggerDoubler(TriggerCause.ATTACKING)),
        (ROAMING_THRONE, TriggerDoubler(TriggerCause.ANY, chosen_type=True, another=True)),
        (DELNEY, TriggerDoubler(TriggerCause.ANY, max_power=2)),
        (TWINFLAME_TRAVELERS, TriggerDoubler(TriggerCause.ANY, subtype="elemental", another=True)),
    ],
    ids=lambda x: x.mana.name if isinstance(x, Real) else "",
)
def test_a_trigger_doubler_is_read_with_the_triggers_it_doubles(card: Real, expected: TriggerDoubler) -> None:
    read = one(card)
    assert read.trigger_doubler == expected
    assert read.modelled


def test_a_card_is_legendary_by_its_type_line() -> None:
    assert one(YAROK).legendary
    assert not one(ROAMING_THRONE).legendary


@pytest.mark.parametrize(
    "card", [PLUNDERING_BARBARIAN, WOOD_ELVES, IMPERIAL_RECRUITER, MULLDRIFTER], ids=lambda c: c.mana.name
)
def test_the_effects_a_permanent_has_as_it_enters_are_entry_triggers(card: Real) -> None:
    assert one(card).entry_trigger


@pytest.mark.parametrize(
    "card", [LANNERY_STORM, CULTIVATE, WAYFARERS_BAUBLE, JAYEMDAE_TOME, DOCKSIDE], ids=lambda c: c.mana.name
)
def test_a_spell_an_activated_ability_and_an_attack_trigger_are_no_entry_triggers(card: Real) -> None:
    assert not one(card).entry_trigger


# --- copies of a creature until end of turn -----------------------------------------------------------------

GOGO = real(
    "Gogo, Mysterious Mime",
    "{3}{R}",
    4,
    "Legendary Creature — Wizard",
    "At the beginning of combat on your turn, you may have Gogo become a copy of another target creature "
    "you control until end of turn, except its name is Gogo, Mysterious Mime. If you do, Gogo and that "
    "creature each get +2/+0 and gain haste until end of turn and attack this turn if able.",
    identity="R",
    tags=("copy-creature", "copy-legendary"),
)
HALL_OF_ECHOES = real(
    "Hall of Echoes",
    "",
    0,
    "Land",
    "{T}: Add {C}.\n{5}: This land becomes a copy of target creature you control until end of turn. The "
    '"legend rule" doesn\'t apply to permanents you control this turn.',
    produced="C",
    tags=("copy-creature", "copy-legendary"),
)
CURSED_MIRROR = real(
    "Cursed Mirror",
    "{2}{R}",
    3,
    "Artifact",
    "{T}: Add {R}.\nAs this artifact enters, you may have it become a copy of any creature on the "
    "battlefield until end of turn, except it has haste.",
    produced="R",
    identity="R",
    roles=(RAMP,),
    tags=("clone", "copy-creature"),
)


def test_a_creature_that_becomes_a_copy_in_combat_is_read() -> None:
    gogo = one(GOGO)
    assert gogo.copy == CopyEffect(CopyKind.COMBAT, CopyOf.CREATURE, haste=True, legend_exempt=True)
    assert gogo.legendary
    assert gogo.modelled


def test_a_land_that_becomes_a_copy_for_mana_is_read_and_is_still_a_land() -> None:
    hall = one(HALL_OF_ECHOES)
    assert hall.copy == CopyEffect(CopyKind.ACTIVATED, CopyOf.CREATURE, cost=5, legend_exempt=True)
    assert hall.land is not None
    assert hall.land.colours == {"C"}
    assert hall.modelled


def test_an_artifact_that_becomes_a_copy_as_it_enters_keeps_its_mana() -> None:
    mirror = one(CURSED_MIRROR)
    assert mirror.copy == CopyEffect(CopyKind.TURN, CopyOf.CREATURE, haste=True)
    assert mirror.mana is not None
    assert mirror.mana.amount == 1
    assert mirror.modelled


# --- clones that stay copies --------------------------------------------------------------------------------

SCULPTING_STEEL = real(
    "Sculpting Steel",
    "{3}",
    3,
    "Artifact",
    "You may have this artifact enter as a copy of any artifact on the battlefield.",
    tags=("clone", "copy-artifact"),
)
PHANTASMAL_IMAGE = real(
    "Phantasmal Image",
    "{1}{U}",
    2,
    "Creature — Illusion",
    "You may have this creature enter as a copy of any creature on the battlefield, except it's an Illusion "
    'in addition to its other types and it has "When this creature becomes the target of a spell or '
    'ability, sacrifice it."',
    identity="U",
    tags=("clone", "copy-creature"),
)
MOCKINGBIRD = real(
    "Mockingbird",
    "{X}{U}",
    1,
    "Creature — Bird Bard",
    "Flying\nYou may have this creature enter as a copy of any creature on the battlefield with mana value "
    "less than or equal to the amount of mana spent to cast this creature, except it's a Bird in addition "
    "to its other types and it has flying.",
    identity="U",
    tags=("clone", "copy-creature"),
)
PHYREXIAN_METAMORPH = real(
    "Phyrexian Metamorph",
    "{3}{U/P}",
    4,
    "Artifact Creature — Phyrexian Shapeshifter",
    "({U/P} can be paid with either {U} or 2 life.)\nYou may have this creature enter as a copy of any "
    "artifact or creature on the battlefield, except it's an artifact in addition to its other types.",
    identity="U",
    tags=("clone", "copy-artifact"),
)
VESUVA = real(
    "Vesuva",
    "",
    0,
    "Land",
    "You may have this land enter tapped as a copy of any land on the battlefield.",
    tags=("clone", "copy-land"),
)


@pytest.mark.parametrize(
    ("card", "expected"),
    [
        (SCULPTING_STEEL, CopyEffect(CopyKind.ENTERS, CopyOf.ARTIFACT)),
        (PHANTASMAL_IMAGE, CopyEffect(CopyKind.ENTERS, CopyOf.CREATURE)),
        (MOCKINGBIRD, CopyEffect(CopyKind.ENTERS, CopyOf.CREATURE, mana_spent=True)),
        (PHYREXIAN_METAMORPH, CopyEffect(CopyKind.ENTERS, CopyOf.ARTIFACT_OR_CREATURE)),
        (VESUVA, CopyEffect(CopyKind.ENTERS, CopyOf.LAND, tapped=True)),
    ],
    ids=lambda x: x.mana.name if isinstance(x, Real) else "",
)
def test_a_card_that_enters_as_a_copy_is_read_with_what_it_copies(card: Real, expected: CopyEffect) -> None:
    read = one(card, commander=BLUE)
    assert read.copy == expected
    assert read.modelled
    assert not read.legendary


BLUE = real("Blue Commander", "{U}", 1, "Legendary Creature — Merfolk", "", identity="U")


# --- copies of an ability -----------------------------------------------------------------------------------

PETER_PARKERS_CAMERA = real(
    "Peter Parker's Camera",
    "{1}",
    1,
    "Artifact",
    "This artifact enters with three film counters on it.\n{2}, {T}, Remove a film counter from this "
    "artifact: Copy target activated or triggered ability you control. You may choose new targets for the "
    "copy.",
    tags=("copy-ability",),
)


def test_an_artifact_that_copies_an_ability_for_the_counters_on_it_is_read() -> None:
    camera = one(PETER_PARKERS_CAMERA)
    assert camera.ability_copy == AbilityCopy(cost=2, counters=3)
    assert camera.modelled


# --- effects the reading does not follow --------------------------------------------------------------------

CHATTERFANG = real(
    "Chatterfang, Squirrel General",
    "{2}{G}",
    3,
    "Legendary Creature — Squirrel Warrior",
    "Forestwalk\nIf one or more tokens would be created under your control, those tokens plus that many 1/1 "
    "green Squirrel creature tokens are created instead.\n{B}, Sacrifice X Squirrels: Target creature gets "
    "+X/-X until end of turn.",
    identity="BG",
    tags=("token-increaser",),
)
TEYSA_KARLOV = real(
    "Teysa Karlov",
    "{2}{W}{B}",
    4,
    "Legendary Creature — Human Advisor",
    "If a creature dying causes a triggered ability of a permanent you control to trigger, that ability "
    "triggers an additional time.\nCreature tokens you control have vigilance and lifelink.",
    identity="WB",
    tags=("trigger-doubler",),
)
MANA_REFLECTION = real(
    "Mana Reflection",
    "{4}{G}{G}",
    6,
    "Enchantment",
    "If you tap a permanent for mana, it produces twice as much of that mana instead.",
    identity="G",
    roles=(RAMP,),
    tags=("mana-increaser",),
)
NYXBLOOM_ANCIENT = real(
    "Nyxbloom Ancient",
    "{4}{G}{G}{G}",
    7,
    "Enchantment Creature — Elemental",
    "Trample\nIf you tap a permanent for mana, it produces three times as much of that mana instead.",
    identity="G",
    tags=("mana-increaser",),
)
KIKI_JIKI = real(
    "Kiki-Jiki, Mirror Breaker",
    "{2}{R}{R}{R}",
    5,
    "Legendary Creature — Goblin Shaman",
    "Haste\n{T}: Create a token that's a copy of target nonlegendary creature you control, except it has "
    "haste. Sacrifice it at the beginning of the next end step.",
    identity="R",
    tags=("copy-creature",),
)
THESPIANS_STAGE = real(
    "Thespian's Stage",
    "",
    0,
    "Land",
    "{T}: Add {C}.\n{2}, {T}: This land becomes a copy of target land, except it has this ability.",
    produced="C",
    tags=("copy-land",),
)


def flagged(deck_cards: tuple[Real, ...], card: Real) -> bool:
    """Whether the deck of the cards and this one lists it under not modelled."""
    return card.mana.name in build(*deck_cards, card).not_modelled


def test_a_token_effect_that_is_not_read_is_listed_when_the_deck_makes_treasures() -> None:
    assert flagged((PLUNDERING_BARBARIAN,), CHATTERFANG)
    assert not flagged((GRIZZLY_BEARS,), CHATTERFANG)


def test_a_token_effect_that_is_read_is_never_listed() -> None:
    for card in (XORN, PARALLEL_LIVES, ACADEMY_MANUFACTOR, OJER_TAQ):
        assert not flagged((PLUNDERING_BARBARIAN,), card)


def test_a_doubler_the_reading_does_not_follow_is_listed_when_the_deck_has_a_trigger_it_counts() -> None:
    assert flagged((PROTEAN_HULK,), TEYSA_KARLOV)
    assert flagged((PLUNDERING_BARBARIAN,), TEYSA_KARLOV)
    assert not flagged((GRIZZLY_BEARS,), TEYSA_KARLOV)
    # A doubler that is read is never listed.
    assert not flagged((PLUNDERING_BARBARIAN,), PANHARMONICON)


def test_a_card_that_makes_more_mana_is_listed_when_the_deck_makes_mana() -> None:
    assert flagged((LLANOWAR,), NYXBLOOM_ANCIENT)
    assert flagged((FOREST,), NYXBLOOM_ANCIENT)
    assert not flagged((GRIZZLY_BEARS,), NYXBLOOM_ANCIENT)
    # With the Tagger's ramp role the card is listed whatever the deck holds, as any ramp card the reading
    # does not follow is.
    assert flagged((GRIZZLY_BEARS,), MANA_REFLECTION)


def test_a_copy_effect_that_is_not_read_is_listed_when_the_deck_has_something_worth_copying() -> None:
    assert flagged((LLANOWAR,), KIKI_JIKI)
    assert flagged((PLUNDERING_BARBARIAN,), KIKI_JIKI)
    assert not flagged((GRIZZLY_BEARS,), KIKI_JIKI)
    # A land that copies a land needs another land, not a mana permanent.
    assert flagged((FOREST,), THESPIANS_STAGE)
    assert not flagged((LLANOWAR,), THESPIANS_STAGE)
    assert not flagged((), THESPIANS_STAGE)


def test_a_copy_effect_that_is_read_is_never_listed() -> None:
    for card in (GOGO, CURSED_MIRROR, SCULPTING_STEEL, PHANTASMAL_IMAGE, PHYREXIAN_METAMORPH):
        assert not flagged((LLANOWAR,), card)
    assert not flagged((LLANOWAR,), PETER_PARKERS_CAMERA)
    assert not flagged((FOREST,), HALL_OF_ECHOES)
    assert not flagged((FOREST,), VESUVA)


def test_a_swapped_in_card_with_an_effect_that_is_not_read_is_not_modelled() -> None:
    assert not incoming(CHATTERFANG, GRIZZLY_BEARS).modelled
    assert incoming(XORN, GRIZZLY_BEARS).modelled
