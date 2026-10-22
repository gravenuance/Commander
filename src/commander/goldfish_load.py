"""The inputs of a goldfish run (ADR 0054), read from the card data and the caches and nothing else: a deck,
its cached Spellbook combos and bracket estimate, and the cards matching its dominant tags. Nothing here
fetches anything."""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from commander.bracket import CACHE_FOLDER as ESTIMATE_FOLDER
from commander.bracket import cached_effective_deck, estimate_cache
from commander.carddb import CardDb
from commander.deckcache import Clock
from commander.deckprofile import Profile
from commander.goldfish_cards import GoldfishDeck, compile_deck
from commander.ideas import dominant_card_ids
from commander.metadata import cached_own_combos
from commander.models import Deck


@dataclass(slots=True, frozen=True)
class GoldfishInput:
    """A compiled deck and the bracket it is played by: the deck's set bracket, else the lowest its cached
    estimate allows, else None."""

    deck: GoldfishDeck
    bracket: int | None


def load_goldfish(
    deck: Deck,
    db: CardDb,
    profile: Profile,
    cache_root: Path,
    *,
    now: Clock,
    finishers_added: Iterable[str] = (),
    finishers_removed: Iterable[str] = (),
) -> GoldfishInput:
    """The deck as the goldfish reads it. cache_root is the folder holding the Spellbook and meta caches
    (`config.cache_dir`); a deck with no cached combos is played without any."""
    cards = db.card_mana(card.name for card in deck.main_cards)
    ids = [info.oracle_id for info in cards.values()]
    bracket = cached_effective_deck(deck, estimate_cache(cache_root / ESTIMATE_FOLDER), now=now).bracket
    compiled = compile_deck(
        deck,
        cards,
        db.cards(ids),
        db.tag_lineage(ids),
        combos=cached_own_combos(deck, cache_root, now=now) or (),
        synergy=dominant_card_ids(deck, profile, db),
        finishers_added=finishers_added,
        finishers_removed=finishers_removed,
    )
    return GoldfishInput(compiled, bracket)
