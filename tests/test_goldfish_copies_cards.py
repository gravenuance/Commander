"""How the goldfish reads doublers and copy effects (ADR 0054); rules texts are real, from the card data."""

import pytest

from commander.goldfish_cards import CardKind, TriggerCause, TriggerDoubler
from test_goldfish_cards import (
    CULTIVATE,
    DOCKSIDE,
    IMPERIAL_RECRUITER,
    JAYEMDAE_TOME,
    LANNERY_STORM,
    MULLDRIFTER,
    PLUNDERING_BARBARIAN,
    WAYFARERS_BAUBLE,
    WOOD_ELVES,
    Real,
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
