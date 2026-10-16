"""What answer cards reach, read from their real rules text and checked against real combo pieces."""

import pytest

from commander.answers import AnswerKind, CardAnswer, Piece, read_modes

HULLBREAKER = Piece("Hullbreaker Horror", "Creature — Kraken Horror", "U", power=7, toughness=8, mana_value=7)
BREACH = Piece("Underworld Breach", "Enchantment", "R", mana_value=2)
LED = Piece("Lion's Eye Diamond", "Artifact", "", mana_value=0)
ORACLE = Piece("Thassa's Oracle", "Creature — Merfolk Wizard", "U", power=1, toughness=3, mana_value=2)
BRAIN_FREEZE = Piece("Brain Freeze", "Instant", "U", mana_value=2, storm=True)
INTUITION = Piece("Intuition", "Instant", "U", mana_value=3)
BALLISTA = Piece("Walking Ballista", "Artifact Creature — Construct", "", 0, 0, mana_value=0, x_cost=True)
TALISMAN = Piece("Talisman of Dominance", "Artifact", "", mana_value=2)
KINNAN = Piece(
    "Kinnan, Bonder Prodigy", "Legendary Creature — Human Druid", "GU", power=2, toughness=2, mana_value=2
)
ELVES = Piece("Llanowar Elves", "Creature — Elf Druid", "G", power=1, toughness=1, mana_value=1)
BIRDS = Piece("Birds of Paradise", "Creature — Bird", "G", power=0, toughness=1, flying=True, mana_value=1)
CRADLE = Piece("Gaea's Cradle", "Legendary Land", "", mana_value=0)
UNKNOWN = Piece("Some New Card", "Creature — Horror")

DEAD_GONE = (
    "Dead deals 2 damage to target creature.\n//\n"
    "Return target creature you don't control to its owner's hand."
)
PYROBLAST = "Choose one —\n• Counter target spell if it's blue.\n• Destroy target permanent if it's blue."
RED_BLAST = "Choose one —\n• Counter target blue spell.\n• Destroy target blue permanent."
BLOODFIRE_DWARF = "{R}, Sacrifice this creature: It deals 1 damage to each creature without flying."
TINDER_WALL = (
    "Defender (This creature can't attack.)\nSacrifice this creature: Add {R}{R}.\n"
    "{R}, Sacrifice this creature: It deals 2 damage to target creature it's blocking."
)
LIGHTNING_BOLT = "Lightning Bolt deals 3 damage to any target."
ABRADE = "Choose one —\n• Abrade deals 3 damage to target creature.\n• Destroy target artifact."
CHAOS_WARP = (
    "The owner of target permanent shuffles it into their library, "
    "then reveals the top card of their library. "
    "If it's a permanent card, they put it onto the battlefield."
)
ELDRAZI_CONFLUENCE = (
    "Choose three. You may choose the same mode more than once.\n"
    "• Target creature gets +3/-3 until end of turn.\n"
    "• Exile target nonland permanent, then return it to the battlefield tapped under its owner's control.\n"
    '• Create a 1/1 colorless Eldrazi Scion creature token with "Sacrifice this token: Add {C}."'
)
TOXIC_DELUGE = (
    "As an additional cost to cast this spell, pay X life.\nAll creatures get -X/-X until end of turn."
)
SNUFF_OUT = "Destroy target nonblack creature. It can't be regenerated."
VEXING_BAUBLE = (
    "Whenever a player casts a spell, if no mana was spent to cast it, counter that spell.\n"
    "{1}, {T}, Sacrifice this artifact: Draw a card."
)
MENTAL_MISSTEP = "({U/P} can be paid with either {U} or 2 life.)\nCounter target spell with mana value 1."
FIERCE_GUARDIANSHIP = (
    "If you control a commander, you may cast this spell without paying its mana cost.\n"
    "Counter target noncreature spell."
)
STERN_SCOLDING = "Counter target creature spell with power or toughness 2 or less."
MINDBREAK_TRAP = (
    "If an opponent cast three or more spells this turn, "
    "you may pay {0} rather than pay this spell's mana cost.\n"
    "Exile any number of target spells."
)
FORCE_OF_WILL = (
    "You may pay 1 life and exile a blue card from your hand rather than pay this spell's mana cost.\n"
    "Counter target spell."
)
FLUSTERSTORM = (
    "Counter target instant or sorcery spell unless its controller pays {1}.\n"
    "Storm (When you cast this spell, copy it for each spell cast before it this turn. "
    "You may choose new targets for the copies.)"
)
UNLICENSED_HEARSE = "{T}: Exile up to two target cards from a single graveyard."
EDICT = "Target player sacrifices a creature of their choice."
PORTAL_TO_PHYREXIA = (
    "When this artifact enters, each opponent sacrifices three creatures of their choice.\n"
    "At the beginning of your upkeep, put target creature card from a graveyard onto the battlefield "
    "under your control. It's a Phyrexian in addition to its other types."
)
KATAKI = 'All artifacts have "At the beginning of your upkeep, sacrifice this artifact unless you pay {1}."'
SPELL_PIERCE = "Counter target noncreature spell unless its controller pays {2}."
COUNTERBALANCE = (
    "Whenever an opponent casts a spell, you may reveal the top card of your library. "
    "If you do, counter that spell if it has the same mana value as the revealed card."
)
CONTROL_MAGIC = "Enchant creature\nYou control enchanted creature."

REMOVAL = (AnswerKind.PERMANENT_REMOVAL,)
COUNTER = (AnswerKind.COUNTERSPELL,)


def answer(text: str, kinds: tuple[AnswerKind, ...] = REMOVAL) -> CardAnswer:
    return CardAnswer("card", kinds, read_modes(text))


def removed(text: str, *pieces: Piece) -> list[bool]:
    return [answer(text).removes(p) for p in pieces]


def countered(text: str, *pieces: Piece) -> list[bool]:
    return [answer(text, COUNTER).counters(p) for p in pieces]


def test_bounce_reaches_only_the_types_it_names() -> None:
    # Gone returns a creature; Dead's 2 damage can't kill the 7/8.
    assert removed(DEAD_GONE, BREACH, LED, HULLBREAKER) == [False, False, True]


def test_colour_hosers_reach_only_their_colour() -> None:
    for text in (PYROBLAST, RED_BLAST):
        assert removed(text, BREACH, LED, TALISMAN, HULLBREAKER) == [False, False, False, True]
        assert countered(text, INTUITION, LED, ORACLE) == [True, False, True]


def test_damage_kills_only_what_it_out_toughs() -> None:
    assert removed(LIGHTNING_BOLT, KINNAN, ORACLE, HULLBREAKER, BREACH) == [True, True, False, False]
    assert removed(ABRADE, KINNAN, HULLBREAKER, LED) == [True, False, True]
    # A flyer escapes, and a land is out of reach.
    assert removed(BLOODFIRE_DWARF, ELVES, BIRDS, KINNAN, CRADLE) == [True, False, False, False]


def test_toughness_loss_kills_like_damage() -> None:
    assert removed(TOXIC_DELUGE, HULLBREAKER, BREACH) == [True, False]
    # The flicker mode returns the card at once, so only -3 toughness answers anything.
    assert removed(ELDRAZI_CONFLUENCE, KINNAN, HULLBREAKER, BREACH) == [True, False, False]


def test_combat_only_and_graveyard_targets_answer_nothing() -> None:
    assert removed(TINDER_WALL, ELVES) == [False]
    assert removed(UNLICENSED_HEARSE, BREACH) == [False]


def test_shuffling_and_exclusions_by_colour() -> None:
    assert removed(CHAOS_WARP, BREACH, LED, CRADLE) == [True, True, True]
    assert removed(
        SNUFF_OUT, ELVES, Piece("Dark Confidant", "Creature — Human Wizard", "B", toughness=1)
    ) == [
        True,
        False,
    ]


def test_removal_follows_its_mana_value_limit() -> None:
    zoyowa = (
        "The owner of target artifact or creature with mana value 1 or greater "
        "shuffles it into their library. Then that player discovers X, where X is its mana value."
    )
    grinding_station = Piece("Grinding Station", "Artifact", "", mana_value=2)
    assert removed(zoyowa, LED, grinding_station, BREACH) == [False, True, False]


def test_counters_follow_their_conditions() -> None:
    free_led = Piece("Lion's Eye Diamond", "Artifact", "", mana_value=0, cast_free=True)
    assert countered(VEXING_BAUBLE, free_led, INTUITION) == [True, False]
    assert countered(MENTAL_MISSTEP, ELVES, INTUITION) == [True, False]
    assert countered(FIERCE_GUARDIANSHIP, INTUITION, ORACLE) == [True, False]
    assert countered(STERN_SCOLDING, KINNAN, HULLBREAKER, INTUITION) == [True, False, False]
    assert countered(MINDBREAK_TRAP, ORACLE, INTUITION) == [True, True]
    assert countered(FORCE_OF_WILL, ORACLE, CRADLE) == [True, False]


def test_only_counters_that_meet_every_copy_answer_a_storm_spell() -> None:
    # Countering Brain Freeze leaves its copies; Flusterstorm's own copies, or Mindbreak Trap, meet them all.
    assert countered(FLUSTERSTORM, BRAIN_FREEZE, INTUITION, ORACLE) == [True, True, False]
    assert countered(MINDBREAK_TRAP, BRAIN_FREEZE) == [True]
    for text in (FORCE_OF_WILL, PYROBLAST, FIERCE_GUARDIANSHIP):
        assert countered(text, BRAIN_FREEZE) == [False]
    assert not CardAnswer("tagged", COUNTER, None).counters(BRAIN_FREEZE)


def test_soft_and_chance_counters_answer_with_their_catch() -> None:
    assert answer(SPELL_PIERCE, COUNTER).counter(INTUITION) == "unless they pay {2}"
    assert answer(SPELL_PIERCE, COUNTER).counter(ORACLE) is None
    assert answer(FLUSTERSTORM, COUNTER).counter(BRAIN_FREEZE) == "unless they pay {1}"
    assert answer(COUNTERBALANCE, COUNTER).counter(INTUITION) == "if the revealed card's mana value matches"
    assert answer(FORCE_OF_WILL, COUNTER).counter(INTUITION) == ""


def test_edicts_reach_their_type_but_the_opponent_chooses() -> None:
    for text in (EDICT, PORTAL_TO_PHYREXIA):
        edict = answer(text)
        assert edict.removal(HULLBREAKER) == "they choose what to sacrifice"
        assert edict.removal(BREACH) is None
    assert answer(LIGHTNING_BOLT).removal(KINNAN) == ""


def test_an_upkeep_tax_answers_nothing() -> None:
    # Kataki's tax falls in its controller's upkeep, before they combo, and costs them only {1}.
    assert read_modes(KATAKI) == ()


def test_what_the_card_data_does_not_know_is_not_ruled_out() -> None:
    assert removed(LIGHTNING_BOLT, UNKNOWN) == [True]
    assert removed(PYROBLAST, UNKNOWN) == [True]


def test_text_the_reader_cannot_follow_falls_back_to_the_tags() -> None:
    assert read_modes(CONTROL_MAGIC) is None
    steal = CardAnswer("steal", (AnswerKind.CREATURE_REMOVAL,), None)
    assert steal.removal(ELVES) == ""
    assert not steal.removes(BREACH)
    assert not steal.counters(BRAIN_FREEZE)


@pytest.mark.parametrize("text", [TINDER_WALL, UNLICENSED_HEARSE, ELDRAZI_CONFLUENCE])
def test_text_read_but_ruled_out_does_not_fall_back(text: str) -> None:
    assert read_modes(text) is not None
