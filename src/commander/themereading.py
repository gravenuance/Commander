"""The themes of a deck as every card search reads them (ADR 0055): the previews, the goldfish's synergy
cards and the Archidekt idea search all take the deck's dominant themes from here.

A theme is one of the deck's Archidekt theme tags. Tags that ask for the same cards are one theme, and a tag
that none of the deck's cards match is no theme, so neither takes one of the few places.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from commander.archidekt import MAX_SEARCH_TAGS
from commander.carddb import CardDb
from commander.deckprofile import Profile, ThemeSpec, resolve_spec
from commander.deckthemes import DerivedTheme, SpecKey, ThemeSource, merge_themes, spec_key
from commander.models import Deck

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
        slugs = db.tag_ids(slug for theme in self.themes for slug in theme.spec.tags)
        found: set[str] = set()
        for theme in self.themes:
            found |= db.matching(resolve_spec(theme.spec, slugs), ids)
        return frozenset(found)


def read_deck_themes(deck: Deck, profile: Profile, db: CardDb) -> DeckReading:
    """The deck's dominant themes: its Archidekt theme tags (after the profile's ignore list and [exclude])
    that match some of its main cards, tags with the same spec as one, the most matched first and ties in
    Archidekt's tag order, at most MAX_SEARCH_TAGS. The play style, role and card type tags are too broad to
    pick cards by (ADR 0009)."""
    themes = [t for t in merge_themes(deck, profile, db).used if t.source is ThemeSource.DECK_TAG]
    oracle_ids = [r.info.oracle_id for r in db.resolve(deck.main_cards) if r.info is not None]
    found: list[ReadTheme] = []
    for group in _same_spec(themes):
        spec = group[0].spec
        ids = db.tag_ids(spec.tags)
        cards = db.matching(resolve_spec(spec, ids), oracle_ids)
        if len(cards) >= MIN_DECK_MATCHES:
            found.append(ReadTheme(tuple(t.name for t in group), spec, cards))
    # sorted is stable, so equal counts stay in the order of the deck's tags.
    found.sort(key=lambda theme: -len(theme.cards))
    return DeckReading(tuple(found[:MAX_SEARCH_TAGS]))


def _same_spec(themes: Iterable[DerivedTheme]) -> list[list[DerivedTheme]]:
    groups: dict[SpecKey, list[DerivedTheme]] = {}
    for theme in themes:
        groups.setdefault(spec_key(theme.spec), []).append(theme)
    return list(groups.values())
