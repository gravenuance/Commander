"""The goldfish loader reads a deck from the card data and the caches alone (ADR 0054)."""

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from commander.bracket import CACHE_FOLDER as ESTIMATE_FOLDER
from commander.bracket import estimate_cache, estimate_deck
from commander.carddb import CardDb
from commander.deckprofile import Profile
from commander.goldfish import deck_slots, goldfish
from commander.goldfish_load import (
    GoldfishError,
    GoldfishInput,
    incoming_card,
    load_goldfish,
    tagged_finishers,
)
from commander.models import Deck
from commander.net import Fetcher
from commander.spellbook import SpellbookClient
from conftest import FakeSpellbook, build_card_db
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


FLUTE_ORACLE = "d6c46906-1339-4440-8cd0-2bb9890d4975"
# Made up for these tests: the fixture tags hold no finisher.
OVERRUN_TAG = json.dumps(
    {
        "object": "tag",
        "id": "aaaaaaaa-0000-4000-8000-000000000003",
        "slug": "overrun",
        "type": "oracle",
        "child_ids": [],
        "taggings": [{"oracle_id": FLUTE_ORACLE, "weight": "median"}],
    }
)


@pytest.fixture(scope="module")
def finisher_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CardDb]:
    """The card data with the Disruptor Flute tagged as a finisher."""
    with CardDb(build_card_db(tmp_path_factory.mktemp("finisherdb"), [OVERRUN_TAG])) as opened:
        yield opened


def finisher_names(loaded: GoldfishInput) -> list[str]:
    return [card.name for card in loaded.deck.cards if card.finisher]


def test_the_tagger_marks_a_finisher_and_the_profile_adds_to_and_takes_from_it(
    deck: Deck, finisher_db: CardDb, tmp_path: Path
) -> None:
    assert tagged_finishers(deck, finisher_db) == ["Disruptor Flute"]
    plain = load_goldfish(deck, finisher_db, Profile(), tmp_path, now=lambda: NOW)
    assert finisher_names(plain) == ["Disruptor Flute"]
    added = Profile(finishers=("battered golem",))
    assert finisher_names(load_goldfish(deck, finisher_db, added, tmp_path, now=lambda: NOW)) == [
        "Battered Golem",
        "Disruptor Flute",
    ]
    removed = Profile(not_finishers=("Disruptor Flute",))
    assert finisher_names(load_goldfish(deck, finisher_db, removed, tmp_path, now=lambda: NOW)) == []


def test_a_finisher_the_profile_adds_is_played_as_one(
    deck: Deck, finisher_db: CardDb, tmp_path: Path
) -> None:
    profile = Profile(finishers=("Battered Golem",), not_finishers=("Disruptor Flute",))
    report = goldfish(
        load_goldfish(deck, finisher_db, profile, tmp_path, now=lambda: NOW).deck,
        bracket=5,
        seed=1,
        games=20,
    )
    assert report.ways_to_win is not None
    assert [f.name for f in report.ways_to_win.finishers] == ["Battered Golem"]


def test_an_incoming_card_is_read_as_the_decks_own_by_the_name_the_card_data_gives(
    deck: Deck, db: CardDb, tmp_path: Path
) -> None:
    loaded = load_goldfish(deck, db, Profile(), tmp_path, now=lambda: NOW)
    signet = incoming_card(loaded, db, "arcane signet", copies=1)
    assert signet.name == "Arcane Signet"
    assert signet.mana is not None
    assert signet.mana.amount == 1
    assert signet.copies == 1


def test_an_incoming_card_that_the_card_data_lacks_is_a_goldfish_error(
    deck: Deck, db: CardDb, tmp_path: Path
) -> None:
    loaded = load_goldfish(deck, db, Profile(), tmp_path, now=lambda: NOW)
    with pytest.raises(GoldfishError, match="'Not A Real Card' is no card the card data knows"):
        incoming_card(loaded, db, "Not A Real Card")


def test_an_incoming_card_on_the_decks_dominant_tag_is_synergy_though_the_deck_lacks_it(
    deck: Deck, db: CardDb, tmp_path: Path
) -> None:
    # The Flash tag picks the Flute out of the deck; the Hoverbike has Flash too, Chaos Warp does not.
    flash = replace(deck, tags=("Flash",))
    loaded = load_goldfish(flash, db, Profile(), tmp_path, now=lambda: NOW)
    assert incoming_card(loaded, db, "High-Speed Hoverbike").synergy
    assert not incoming_card(loaded, db, "Chaos Warp").synergy
