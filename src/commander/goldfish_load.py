"""The inputs of a goldfish run (ADR 0054), read from the card data and the caches and nothing else: a deck,
its cached Spellbook combos and bracket estimate, and the cards matching its dominant tags. Nothing here
fetches anything."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from commander.bracket import CACHE_FOLDER as ESTIMATE_FOLDER
from commander.bracket import cached_effective_deck, estimate_cache
from commander.carddb import CardDb, CardInfo, CardMana
from commander.deckcache import Clock
from commander.deckprofile import Profile
from commander.goldfish_cards import FINISHER_TAGS, GoldfishCard, GoldfishDeck, compile_card, compile_deck
from commander.ideas import dominant_card_ids
from commander.metadata import cached_own_combos
from commander.models import Deck
from commander.spellbook import ComboDetail


class GoldfishError(Exception):
    """A goldfish run or swap that cannot be played, in words for the person who asked."""


@dataclass(slots=True, frozen=True)
class CardContext:
    """What the deck's cards were read against, kept to read a swap's incoming card the same way."""

    deck: Deck
    profile: Profile
    cards: Mapping[str, CardMana]
    infos: Mapping[str, CardInfo]
    tags: Mapping[str, frozenset[str]]
    combos: tuple[ComboDetail, ...]
    finishers_added: tuple[str, ...]
    finishers_removed: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class GoldfishInput:
    """A compiled deck and the bracket it is played by: the deck's set bracket, else the lowest its cached
    estimate allows, else None."""

    deck: GoldfishDeck
    bracket: int | None
    context: CardContext


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
    (`config.cache_dir`); a deck with no cached combos is played without any. The finishers the profile names
    count with the ones given."""
    added = (*profile.finishers, *finishers_added)
    removed = (*profile.not_finishers, *finishers_removed)
    cards = db.card_mana(card.name for card in deck.main_cards)
    ids = [info.oracle_id for info in cards.values()]
    infos = db.cards(ids)
    tags = db.tag_lineage(ids)
    bracket = cached_effective_deck(deck, estimate_cache(cache_root / ESTIMATE_FOLDER), now=now).bracket
    combos = cached_own_combos(deck, cache_root, now=now) or ()
    compiled = compile_deck(
        deck,
        cards,
        infos,
        tags,
        combos=combos,
        synergy=dominant_card_ids(deck, profile, db),
        finishers_added=added,
        finishers_removed=removed,
    )
    context = CardContext(deck, profile, cards, infos, tags, tuple(combos), added, removed)
    return GoldfishInput(compiled, bracket, context)


def tagged_finishers(deck: Deck, db: CardDb) -> list[str]:
    """The deck's cards the Tagger marks as finishers, before the profile adds to or takes from them."""
    cards = db.card_mana(card.name for card in deck.main_cards)
    tags = db.tag_lineage(info.oracle_id for info in cards.values())
    return [info.name for info in cards.values() if tags.get(info.oracle_id, frozenset()) & FINISHER_TAGS]


def incoming_card(loaded: GoldfishInput, db: CardDb, name: str, copies: int = 1) -> GoldfishCard:
    """A card that is not in the deck, read as the deck's own are, for a swap. The card data names it, so a
    front face or another case finds it; a card it lacks is a GoldfishError."""
    info = db.card_named(name)
    mana = db.card_mana([info.name]) if info is not None else {}
    if info is None or not mana:
        raise GoldfishError(f"{name!r} is no card the card data knows")
    context = loaded.context
    ids = [info.oracle_id]
    return compile_card(
        info.name,
        context.deck,
        {**context.cards, **mana},
        {**context.infos, **db.cards(ids)},
        {**context.tags, **db.tag_lineage(ids)},
        copies=copies,
        combos=context.combos,
        synergy=dominant_card_ids(context.deck, context.profile, db, also=ids),
        finishers_added=context.finishers_added,
        finishers_removed=context.finishers_removed,
    )
