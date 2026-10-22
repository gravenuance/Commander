"""The themes of a deck as every card search reads them (ADR 0055): the previews, the goldfish's synergy
cards and the Archidekt idea search all take the deck's dominant themes from here.

A theme is one of the deck's Archidekt theme tags. Tags that ask for the same cards are one theme, and a tag
that none of the deck's cards match is no theme, so neither takes one of the few places. A theme matches the
cards its tags name and the cards that feed them (`themewants`).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from commander.archidekt import MAX_SEARCH_TAGS
from commander.carddb import CardDb
from commander.deckprofile import Profile, ThemeSpec, resolve_spec
from commander.deckthemes import DerivedTheme, SpecKey, ThemeSource, merge_themes, spec_key
from commander.models import Deck
from commander.themewants import CardFacts, card_facts, spec_wants

# A theme needs at least this many of the deck's main cards.
MIN_DECK_MATCHES = 1


@dataclass(slots=True, frozen=True)
class ReadTheme:
    # The deck tags that ask for these cards, in the deck's order; the first stands for them all.
    names: tuple[str, ...]
    spec: ThemeSpec
    # The oracle ids of the deck's main cards the theme matches.
    cards: frozenset[str]

    @property
    def name(self) -> str:
        return self.names[0]


@dataclass(slots=True, frozen=True)
class DeckReading:
    # At most MAX_SEARCH_TAGS themes, the one matching the most of the deck's cards first.
    themes: tuple[ReadTheme, ...]

    @property
    def names(self) -> list[str]:
        return [theme.name for theme in self.themes]

    def matching(self, db: CardDb, oracle_ids: Iterable[str]) -> frozenset[str]:
        """The given cards that match one of the themes."""
        ids = list(oracle_ids)
        facts = card_facts(db, ids) if any(spec_wants(t.spec) for t in self.themes) else {}
        found: set[str] = set()
        for theme in self.themes:
            found |= _cards_matching(db, theme.spec, ids, facts)
        return frozenset(found)


def read_deck_themes(deck: Deck, profile: Profile, db: CardDb) -> DeckReading:
    """The deck's dominant themes: its Archidekt theme tags (after the profile's ignore list and [exclude])
    that match some of its main cards, tags with the same spec as one, the most matched first and ties in
    Archidekt's tag order, at most MAX_SEARCH_TAGS. The play style, role and card type tags are too broad to
    pick cards by (ADR 0009)."""
    themes = [t for t in merge_themes(deck, profile, db).used if t.source is ThemeSource.DECK_TAG]
    oracle_ids = [r.info.oracle_id for r in db.resolve(deck.main_cards) if r.info is not None]
    groups = _same_spec(themes)
    facts = card_facts(db, oracle_ids) if any(spec_wants(group[0].spec) for group in groups) else {}
    found: list[ReadTheme] = []
    for group in groups:
        spec = group[0].spec
        cards = _cards_matching(db, spec, oracle_ids, facts)
        if len(cards) >= MIN_DECK_MATCHES:
            found.append(ReadTheme(tuple(t.name for t in group), spec, cards))
    # sorted is stable, so equal counts stay in the order of the deck's tags.
    found.sort(key=lambda theme: -len(theme.cards))
    return DeckReading(tuple(found[:MAX_SEARCH_TAGS]))


def _cards_matching(
    db: CardDb, spec: ThemeSpec, oracle_ids: list[str], facts: Mapping[str, CardFacts]
) -> frozenset[str]:
    """The cards that have one of the spec's types, keywords or tags, or are a kind of card feeding it."""
    found = set(db.matching(resolve_spec(spec, db.tag_ids(spec.tags)), oracle_ids))
    wants = spec_wants(spec)
    found.update(
        oracle_id
        for oracle_id in oracle_ids
        if oracle_id in facts and any(want.matches(facts[oracle_id]) for want in wants)
    )
    return frozenset(found)


def _same_spec(themes: Iterable[DerivedTheme]) -> list[list[DerivedTheme]]:
    groups: dict[SpecKey, list[DerivedTheme]] = {}
    for theme in themes:
        groups.setdefault(spec_key(theme.spec), []).append(theme)
    return list(groups.values())
