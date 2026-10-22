"""The goldfish loader reads a deck from the card data and the caches alone (ADR 0054)."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from commander.bracket import CACHE_FOLDER as ESTIMATE_FOLDER
from commander.bracket import estimate_cache, estimate_deck
from commander.carddb import CardDb
from commander.deckprofile import Profile
from commander.goldfish import deck_slots, goldfish
from commander.goldfish_load import load_goldfish
from commander.models import Deck
from commander.net import Fetcher
from commander.spellbook import SpellbookClient
from conftest import FakeSpellbook
from test_meta import NOW, db, deck

__all__ = ["db", "deck"]  # fixtures shared with test_meta


def test_the_deck_is_compiled_from_the_card_data_with_its_dominant_tags_as_synergy(
    deck: Deck, db: CardDb, tmp_path: Path
) -> None:
    # Magda, a Golem, a Stax piece and six Mountains; the profile does not turn the tag search on.
    flash = replace(deck, tags=("Flash",))
    loaded = load_goldfish(flash, db, Profile(), tmp_path, now=lambda: NOW)
    cards = {card.name: card for card in loaded.deck.cards}
    assert [(name, card.copies) for name, card in cards.items()] == [
        ("Battered Golem", 1),
        ("Disruptor Flute", 1),
        ("Mountain", 6),
    ]
    assert [c.name for c in loaded.deck.commanders] == ["Magda, Brazen Outlaw"]
    assert cards["Mountain"].land is not None
    # Only the Flute is Flash, so it alone is the synergy card.
    assert {name for name, card in cards.items() if card.synergy} == {"Disruptor Flute"}
    assert loaded.deck.combos == ()
    # The deck's own bracket stands; the 8 cards of its library are the slots.
    assert loaded.bracket == 5
    assert len(deck_slots(loaded.deck)) == 8


def test_the_bracket_of_a_deck_with_none_set_comes_from_the_cached_estimate_and_never_a_fetch(
    deck: Deck, db: CardDb, tmp_path: Path
) -> None:
    unset = replace(deck, bracket=None)
    assert load_goldfish(unset, db, Profile(), tmp_path, now=lambda: NOW).bracket is None
    fake = FakeSpellbook()
    with fake.client() as http:
        client = SpellbookClient(Fetcher(http, max_attempts=1))
        estimate_deck(
            unset, client, estimate_cache(tmp_path / ESTIMATE_FOLDER), now=lambda: NOW, refresh=False
        )
    asked = len(fake.bodies)
    # The cached estimate allows bracket 4 at the least, however long ago it was read.
    later = NOW + timedelta(days=60)
    assert load_goldfish(unset, db, Profile(), tmp_path, now=lambda: later).bracket == 4
    assert len(fake.bodies) == asked


def test_a_loaded_deck_plays_through_to_a_report(deck: Deck, db: CardDb, tmp_path: Path) -> None:
    loaded = load_goldfish(deck, db, Profile(), tmp_path, now=lambda: NOW)
    report = goldfish(loaded.deck, bracket=loaded.bracket, seed=deck.id, games=30)
    assert report.turns == (1, 2, 3, 4, 5)
    assert report.commanders[0].name == "Magda, Brazen Outlaw"
    assert report.ways_to_win is None
