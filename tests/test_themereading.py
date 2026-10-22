"""A deck's themes as the card searches read them: what feeds a payoff tag counts, and the commander asks too
(ADR 0055)."""

from dataclasses import replace

from commander.carddb import CardDb
from commander.deckprofile import Profile, ThemeSpec
from commander.ideas import dominant_card_ids, dominant_tags
from commander.models import Deck
from commander.scryfall import parse_search_page
from commander.themewants import FEEDERS_BY_TAG, LOW_POWER, VEHICLE, CardFacts, preview_facts, spec_wants
from conftest import BATTERED_GOLEM, DISRUPTOR_FLUTE, HOVERBIKE, MAGDA_ORACLE, load_fixture
from test_ideas import themed_deck
from test_meta import db, deck

__all__ = ["db", "deck"]  # fixtures shared with test_meta


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
        "synergy-enchantment": ["Captain's Tutelage", "The Wrath of Khan"],
        # Cloistered Telepath has 2 power and Quark 1; D-7 Battle Cruiser has 3 but is no creature.
        "synergy-low-power": ["Cloistered Telepath", "Quark, Humble Proprietor"],
        # Quark's text creates a Treasure, and no tag is needed to know.
        "synergy-treasure": ["Quark, Humble Proprietor"],
        "synergy-vehicle": ["D-7 Battle Cruiser"],
    }


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


def test_every_artifact_of_the_deck_feeds_the_artifacts_tag_though_it_is_a_payoff_tag(
    deck: Deck, db: CardDb
) -> None:
    # The card data has no synergy-artifact tag here, so only the artifacts themselves can match.
    themed = themed_deck(deck, ("Artifacts",))
    assert dominant_tags(themed, Profile(), db) == ["Artifacts"]
    assert dominant_card_ids(themed, Profile(), db) == {DISRUPTOR_FLUTE, HOVERBIKE, BATTERED_GOLEM}


def test_a_creature_with_power_two_or_less_feeds_the_weenies_tag(deck: Deck, db: CardDb) -> None:
    # Magda is a 2-power creature; the Golem has 3 and the Hoverbike is no creature.
    themed = themed_deck(deck, ("Weenies",))
    assert dominant_tags(themed, Profile(), db) == ["Weenies"]
    assert dominant_card_ids(themed, Profile(), db) == {MAGDA_ORACLE}


def test_a_card_that_creates_treasure_feeds_the_treasure_tag(deck: Deck, db: CardDb) -> None:
    # Magda's text creates a Treasure; the card data holds no repeatable-treasures tag for her.
    themed = themed_deck(deck, ("Treasure",))
    assert dominant_card_ids(themed, Profile(), db) == {MAGDA_ORACLE}
