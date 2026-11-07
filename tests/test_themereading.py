"""A deck's themes as the card searches read them: what feeds a payoff tag counts, and the commander asks too
(ADR 0055)."""

from collections.abc import Iterator
from dataclasses import replace

import pytest

from commander.carddb import CardDb
from commander.deckprofile import Profile, ThemeSpec
from commander.deckthemes import merge_themes
from commander.models import Deck
from commander.scryfall import parse_search_page
from commander.themereading import read_deck_themes
from commander.themewants import (
    BIG_SPELL,
    FEEDERS_BY_TAG,
    GROWS_CREATURES,
    LOW_POWER,
    OPPONENT_REMOVAL,
    TEAM_PROTECTION,
    TOKEN_MAKER,
    TREASURE,
    VEHICLE,
    CardFacts,
    Want,
    commander_conflicts,
    commander_wants,
    preview_facts,
    spec_wants,
    theme_wants,
    type_words,
    want_sizes,
)
from conftest import (
    BATTERED_GOLEM,
    DISRUPTOR_FLUTE,
    HOVERBIKE,
    MAGDA_ORACLE,
    build_card_db,
    extra_tag_line,
    load_fixture,
)
from test_ideas import themed_deck
from test_meta import db, deck

__all__ = ["db", "deck"]  # fixtures shared with test_meta


@pytest.fixture(scope="module")
def low_power_commander_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CardDb]:
    """The card data with a made-up synergy-low-power tag on Magda, the fixture deck's commander."""
    path = build_card_db(
        tmp_path_factory.mktemp("lowpowerdb"), [extra_tag_line("synergy-low-power", MAGDA_ORACLE)]
    )
    with CardDb(path) as opened:
        yield opened


def previews() -> dict[str, CardFacts]:
    """The facts of the real Star Trek previews of the search fixture, with no tags."""
    cards = parse_search_page(load_fixture("scryfall_search_trk.json")).cards
    return {card.name: preview_facts(card, frozenset()) for card in cards}


def test_each_feeder_wants_the_cards_that_feed_its_payoff_tag() -> None:
    facts = previews()
    wanting = {
        name: sorted(card for card, f in facts.items() if FEEDERS_BY_TAG[name].matches(f))
        for name in FEEDERS_BY_TAG
    }
    assert wanting == {
        "synergy-artifact": ["D-7 Battle Cruiser"],
        # Captain's Tutelage is an Aura; no preview is Equipment.
        "synergy-aura": ["Captain's Tutelage"],
        "synergy-enchantment": ["Captain's Tutelage", "The Wrath of Khan"],
        "synergy-equipment": [],
        # Cloistered Telepath has 2 power and Quark 1; D-7 Battle Cruiser has 3 but is no creature.
        "synergy-low-power": ["Cloistered Telepath", "Quark, Humble Proprietor"],
        # Quark's text creates a Treasure, and no tag is needed to know.
        "synergy-treasure": ["Quark, Humble Proprietor"],
        "synergy-vehicle": ["D-7 Battle Cruiser"],
    }


def test_a_treasure_made_at_the_start_of_a_sentence_or_a_mode_feeds_the_treasure_tag() -> None:
    quark = previews()["Quark, Humble Proprietor"]
    # Strike It Rich's text, and a mode of Atsushi, the Blazing Sky.
    assert TREASURE.matches(replace(quark, text="Create a Treasure token."))
    assert TREASURE.matches(replace(quark, text="• Create three Treasure tokens."))
    assert not TREASURE.matches(replace(quark, text="Create a Food token."))


def test_a_power_that_is_not_a_number_is_no_low_power_and_a_card_without_power_is_none() -> None:
    telepath = previews()["Cloistered Telepath"]
    assert LOW_POWER.matches(telepath)
    assert not LOW_POWER.matches(replace(telepath, power="*"))
    assert not LOW_POWER.matches(replace(telepath, power=None))
    assert not LOW_POWER.matches(replace(telepath, power="3"))


def test_a_deck_tag_for_vehicles_wants_spacecraft_without_the_payoff_tag() -> None:
    assert spec_wants(ThemeSpec(types=("Vehicle",))) == (VEHICLE,)
    assert spec_wants(ThemeSpec(types=("Vehicle",), tags=("synergy-vehicle",))) == (VEHICLE,)
    assert spec_wants(ThemeSpec(types=("Dwarf",), tags=("typal-dwarf",))) == ()


def wanted_by(wants: tuple[Want, ...], facts: CardFacts) -> bool:
    return any(want.matches(facts) for want in wants)


def test_a_commander_that_animates_enchantments_and_cares_for_big_cards_wants_big_enchantments_but_no_auras(
    db: CardDb,
) -> None:
    saga = previews()["The Wrath of Khan"]  # Enchantment - Saga, mana value 3
    wants = commander_wants(db, {"animate-enchantment", "high-mana-value-matters"})
    assert not wanted_by(wants, saga)
    assert wanted_by(wants, replace(saga, mana_value=4))
    assert not wanted_by(wants, replace(saga, type_line="Enchantment — Aura", mana_value=6))
    assert not wanted_by(wants, replace(saga, type_line="Artifact", mana_value=6))
    # Without the care for big cards, any enchantment that is no Aura will do.
    assert wanted_by(commander_wants(db, {"animate-enchantment"}), saga)


def test_a_commander_that_animates_artifacts_wants_artifacts_but_no_equipment_of_mana_value_four_or_more(
    db: CardDb,
) -> None:
    cruiser = previews()["D-7 Battle Cruiser"]  # Artifact - Spacecraft, mana value 2
    wants = commander_wants(db, {"animate-artifact", "high-mana-value-matters"})
    assert not wanted_by(wants, cruiser)
    assert wanted_by(wants, replace(cruiser, mana_value=5))
    # Equipment is the one artifact such a commander leaves alone.
    assert not wanted_by(wants, replace(cruiser, type_line="Artifact — Equipment", mana_value=5))


def test_a_commanders_payoff_tags_want_what_feeds_them_and_its_treasure_makers_want_treasure(
    db: CardDb,
) -> None:
    facts = previews()
    quark, telepath, khan = (
        facts["Quark, Humble Proprietor"],
        facts["Cloistered Telepath"],
        facts["The Wrath of Khan"],
    )
    assert wanted_by(commander_wants(db, {"synergy-low-power"}), telepath)
    assert not wanted_by(commander_wants(db, {"synergy-low-power"}), khan)
    assert wanted_by(commander_wants(db, {"repeatable-treasures"}), quark)
    assert wanted_by(commander_wants(db, {"synergy-treasure"}), quark)
    assert not wanted_by(commander_wants(db, {"synergy-treasure"}), telepath)
    assert commander_wants(db, {"flash", "ramp"}) == ()


def test_a_typal_commander_wants_its_type_but_a_typal_tag_that_is_no_type_wants_nothing(db: CardDb) -> None:
    facts = previews()
    # Dwarf is a type in the card data; "creature", "choose" and "non-human" are no tribe.
    magda = replace(facts["Quark, Humble Proprietor"], type_line="Legendary Creature — Dwarf Berserker")
    assert wanted_by(commander_wants(db, {"typal-dwarf"}), magda)
    assert not wanted_by(commander_wants(db, {"typal-dwarf"}), facts["Quark, Humble Proprietor"])
    assert commander_wants(db, {"typal-creature", "typal-choose", "typal-non-human"}) == ()


def test_a_commander_that_adds_to_the_tokens_made_wants_cards_that_make_them_for_you(db: CardDb) -> None:
    quark = previews()["Quark, Humble Proprietor"]
    for tag in ("synergy-token", "token-doubler", "token-increaser", "token-replacer"):
        wants = commander_wants(db, {tag})
        assert wants == (TOKEN_MAKER,)
        # Quark's text creates a Treasure token each upkeep.
        assert wanted_by(wants, quark)
    makers = ("Investigate.", "Amass Orcs 2.", "Incubate 3.", "• Create a 1/1 red Officer creature token.")
    for text in makers:
        assert TOKEN_MAKER.matches(replace(quark, text=text))
    # Impossible Choice's mode: the opponent creates the tokens.
    removal = "Destroy target creature. Its controller creates two 1/1 Borg artifact creature tokens."
    # A payoff for tokens, a doubler and a hoser make none themselves.
    for text in (
        removal,
        "Whenever you create one or more tokens, draw a card.",
        "If you would create one or more tokens, create twice that many of those tokens instead.",
        "Your opponents can't create tokens.",
        "Whenever you investigate, put a +1/+1 counter on this creature.",
    ):
        assert not TOKEN_MAKER.matches(replace(quark, text=text)), text


def test_a_commander_that_profits_from_the_opponents_creatures_dying_wants_removal(db: CardDb) -> None:
    khan = previews()["The Wrath of Khan"]
    # Vren, the Relentless's text.
    vren = "Ward {2}\nIf a creature an opponent controls would die, exile it instead."
    wants = commander_wants(db, (), [vren])
    assert wants == (OPPONENT_REMOVAL,)
    assert commander_wants(db, (), ["Whenever a creature an opponent controls dies, draw a card."]) == wants
    assert commander_wants(db, (), ["Whenever a creature you control dies, draw a card."]) == ()
    for text in (
        "Each opponent sacrifices a creature of their choice.",
        "All creatures get -2/-2 until end of turn.",
        "Destroy all nonartifact creatures.",
    ):
        assert wanted_by(wants, replace(khan, text=text))
    assert not wanted_by(wants, replace(khan, text="Destroy target artifact."))


def test_a_commander_casting_big_spells_from_the_library_wants_spells_of_mana_value_four_or_more(
    db: CardDb,
) -> None:
    khan = previews()["The Wrath of Khan"]  # Enchantment - Saga, mana value 3
    wants = commander_wants(db, {"high-mana-value-matters"})
    assert wants == (BIG_SPELL,)
    assert not wanted_by(wants, khan)
    assert wanted_by(wants, replace(khan, mana_value=4))
    assert not wanted_by(wants, replace(khan, type_line="Land", mana_value=4))


def test_a_commander_counting_small_creatures_has_cards_that_grow_them_for_good_work_against_it() -> None:
    assert commander_conflicts({"synergy-low-power", "lifegain"}) == (GROWS_CREATURES,)
    assert commander_conflicts({"lifegain"}) == ()
    quark = previews()["Quark, Humble Proprietor"]
    for text in (
        "Other creatures you control get +1/+1.",
        "Creatures you control get +1/+0.",
        "Put a +1/+1 counter on each creature you control.",
        "When this creature enters, put a +1/+1 counter on another target creature you control.",
        "Other creatures you control enter with an additional +1/+1 counter on them.",
    ):
        assert GROWS_CREATURES.matches(replace(quark, text=text)), text
    for text in (
        "Creatures you control get +1/+0 until end of turn.",
        "Until end of turn, creatures you control get +1/+1 and gain trample.",
        # Cards that care about counters put none.
        "Whenever a creature you control with a +1/+1 counter on it attacks, draw a card.",
        "Whenever you put one or more +1/+1 counters on a creature you control, draw a card.",
    ):
        assert not GROWS_CREATURES.matches(replace(quark, text=text)), text
    assert GROWS_CREATURES.matches(replace(quark, slugs=frozenset({"anthem"})))


def test_a_want_needs_one_of_its_kinds_and_every_limit_whether_or_not_given_the_type_words() -> None:
    rule = Want(
        "big treasure maker",
        types=("Dragon",),
        text=r"\bTreasure\b",
        not_types=("Legendary",),
        max_power=3,
        min_mana_value=4,
    )
    card = CardFacts("Creature — Elf", 5.0, "2", "Create a Treasure token.", frozenset(), frozenset())
    cases = [
        (card, True),
        (replace(card, text="Draw a card."), False),
        (replace(card, type_line="Creature — Dragon", text="Draw a card."), True),
        (replace(card, type_line="Legendary Creature — Dragon"), False),
        (replace(card, power="4"), False),
        (replace(card, power="*"), False),
        (replace(card, mana_value=3.0), False),
        (replace(card, mana_value=4.0), True),
    ]
    for facts, expected in cases:
        assert rule.matches(facts) is expected, facts
        assert rule.matches(facts, type_words(facts.type_line)) is expected, facts


def test_a_wants_size_is_the_commander_legal_cards_it_matches(db: CardDb) -> None:
    # Of the fixture's legal creatures Magda has power 2, Tempestra 1 and Battered Golem 3; Magda's text
    # creates Treasure; the filler is artifacts. Asked again, the build's count answers.
    expected = {LOW_POWER: 2, TREASURE: 1, VEHICLE: 1}
    assert want_sizes(db, [LOW_POWER, TREASURE, VEHICLE]) == expected
    assert want_sizes(db, [TREASURE]) == {TREASURE: 1}


def test_a_wide_board_wants_protection_for_its_creatures_and_a_sacrifice_deck_wants_fodder() -> None:
    khan = previews()["The Wrath of Khan"]
    # The Needs of the Many's text, its sentence starting with a capital.
    needs = "Creatures you control gain hexproof and indestructible until end of turn."
    assert theme_wants(ThemeSpec(tags=("repeatable-creature-tokens",))) == (TEAM_PROTECTION,)
    assert TEAM_PROTECTION.matches(replace(khan, text=needs))
    assert not TEAM_PROTECTION.matches(replace(khan, text="Target creature gains indestructible."))
    assert theme_wants(ThemeSpec(tags=("sacrifice-outlet",))) == (TOKEN_MAKER,)
    # The feeders come first, and a want asked for twice is one.
    assert theme_wants(ThemeSpec(tags=("synergy-low-power", "death-trigger", "free-sacrifice-outlet"))) == (
        LOW_POWER,
        TOKEN_MAKER,
    )
    # The theme reading counts the feeders alone.
    assert spec_wants(ThemeSpec(tags=("repeatable-creature-tokens",))) == ()


def test_the_commanders_wants_are_read_though_the_deck_has_no_theme_tags(
    deck: Deck, low_power_commander_db: CardDb
) -> None:
    # Magda is a made-up synergy-low-power commander here, and cards that grow creatures work against her.
    reading = read_deck_themes(themed_deck(deck, ()), Profile(), low_power_commander_db)
    assert reading.names == []
    assert reading.commander == (LOW_POWER,)
    assert reading.against == (GROWS_CREATURES,)


def test_suggests_themes_still_name_the_payoff_tags_alone(deck: Deck, db: CardDb) -> None:
    # The feeders are the reading's, not the spec's: suggest ranks ideas and cuts by these specs.
    used = merge_themes(themed_deck(deck, ("Artifacts", "Weenies", "Vehicles")), Profile(), db).used
    assert {theme.name: theme.spec for theme in used} == {
        "Artifacts": ThemeSpec(tags=("synergy-artifact",)),
        "Weenies": ThemeSpec(tags=("synergy-low-power",)),
        "Vehicles": ThemeSpec(types=("Vehicle",)),
    }


def test_every_artifact_of_the_deck_feeds_the_artifacts_tag_though_it_is_a_payoff_tag(
    deck: Deck, db: CardDb
) -> None:
    # The card data has no synergy-artifact tag here, so only the artifacts themselves can match.
    reading = read_deck_themes(themed_deck(deck, ("Artifacts",)), Profile(), db)
    assert reading.names == ["Artifacts"]
    assert reading.themes[0].cards == {DISRUPTOR_FLUTE, HOVERBIKE, BATTERED_GOLEM}


def test_a_creature_with_power_two_or_less_feeds_the_weenies_tag(deck: Deck, db: CardDb) -> None:
    # Magda is a 2-power creature; the Golem has 3 and the Hoverbike is no creature.
    reading = read_deck_themes(themed_deck(deck, ("Weenies",)), Profile(), db)
    assert reading.names == ["Weenies"]
    assert reading.themes[0].cards == {MAGDA_ORACLE}


def test_a_card_that_creates_treasure_feeds_the_treasure_tag(deck: Deck, db: CardDb) -> None:
    # Magda's text creates a Treasure; the card data holds no repeatable-treasures tag for her.
    reading = read_deck_themes(themed_deck(deck, ("Treasure",)), Profile(), db)
    assert reading.themes[0].cards == {MAGDA_ORACLE}
