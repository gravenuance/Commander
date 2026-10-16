"""A deck's themes, read from what its owner says about it on Archidekt: its deck tags and its categories.

Deck tags come from Archidekt's fixed list (Dwarves, Landfall, Stax...) and map by name to type words,
keywords and Tagger tags, with OVERRIDES where the names differ. A category the owner made up (Leyline,
Sac Outlet) becomes the Tagger tags and subtypes most of its cards share but few other cards have.
"""

import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum

from commander.analysis import CARD_TYPES, CATEGORY_ALIASES
from commander.carddb import CardDb, ResolvedCard, Theme
from commander.deckprofile import (
    PROFILE_THEME,
    Profile,
    ResolvedThemes,
    ThemeSpec,
    resolve_spec,
    resolve_themes,
)
from commander.models import Deck

# Deck tags that say how a deck plays or how strong it is, not which cards it wants.
PLAY_STYLES = frozenset(
    {
        "activated abilities",
        "aggro",
        "budget",
        "casual",
        "cedh",
        "combo",
        "commander matters",
        "competitive",
        "control",
        "etb effects",
        "glass cannon",
        "good stuff",
        "goodstuff",
        "group hug",
        "midrange",
        "politics",
        "tempo",
        "triggered abilities",
        "turbo",
        "typal",
        "tribal",
        "value",
    }
)
# Deck tags whose name matches no Tagger tag, type or keyword, by casefolded name (checked 2026-10-16).
OVERRIDES: Mapping[str, ThemeSpec] = {
    "+1/+1 counters": ThemeSpec(tags=("pp-counters-matter",)),
    "aristocrats": ThemeSpec(tags=("death-trigger", "free-sacrifice-outlet")),
    "artifacts": ThemeSpec(tags=("synergy-artifact",)),
    "blink": ThemeSpec(tags=("flicker",)),
    "burn": ThemeSpec(tags=("burn-player",)),
    "enchantments": ThemeSpec(tags=("synergy-enchantment",)),
    "extra combats": ThemeSpec(tags=("extra-combat-phase",)),
    "go-wide": ThemeSpec(tags=("repeatable-creature-tokens", "anthem")),
    "graveyard": ThemeSpec(tags=("cards-in-graveyard-matter", "castable-from-graveyard")),
    "lifedrain": ThemeSpec(tags=("drain-life",)),
    "ltb effects": ThemeSpec(tags=("leaves-battlefield-trigger",)),
    "mill": ThemeSpec(tags=("mill-opponent",)),
    "reanimator": ThemeSpec(tags=("reanimate",)),
    "sacrifice": ThemeSpec(tags=("sacrifice-outlet",)),
    "self-mill": ThemeSpec(tags=("mill-self",)),
    "stax": ThemeSpec(
        tags=("rule-of-law", "prevent-cast", "hate-activation", "prevent-activation", "hatebear")
    ),
    "stompy": ThemeSpec(tags=("power-matters",)),
    "theft": ThemeSpec(tags=("theft-creature", "theft-permanent")),
    "tokens": ThemeSpec(tags=("repeatable-creature-tokens", "synergy-token-creature")),
    "treasure": ThemeSpec(tags=("repeatable-treasures", "synergy-treasure")),
    "weenies": ThemeSpec(tags=("synergy-low-power",)),
}
# Tagger tags that describe a card, or gather a whole family (typal), rather than name a plan.
TRIVIA_TAGS = frozenset(
    {
        "activated-ability",
        "alliteration",
        "manaless-value",
        "mix-and-match",
        "single-english-word-name",
        "symmetrical",
        "temporary-token",
        "triggered-ability",
        "typal",
        "unique-type-line",
    }
)
TRIVIA_PREFIXES = ("cycle-", "namesake-", "virtual-")
ROLES_AND_TYPES = frozenset(
    {alias for aliases in CATEGORY_ALIASES.values() for alias in aliases}
    | {card_type.casefold() for card_type in CARD_TYPES}
)
# Categories that hold roles, card types or deck parts, which suggest already covers or which carry no theme.
NOT_THEMES = frozenset(
    ROLES_AND_TYPES
    | {"land", "lands", "commander", "combo", "combos", "combo pieces", "maybeboard", "sideboard", "maybe"}
    | {"uncategorized"}
)


class ThemeSource(Enum):
    DECK_TAG = "deck tag"
    CATEGORY = "category"


class FeatureKind(Enum):
    """What cards in a category can share: a Tagger tag or a subtype."""

    TAG = "tag"
    TYPE = "type"


Feature = tuple[FeatureKind, str]


@dataclass(slots=True, frozen=True)
class DerivedTheme:
    source: ThemeSource
    # The deck tag or category name, as Archidekt shows it.
    name: str
    spec: ThemeSpec
    # For a category: how many of its cards the spec covers, out of how many the card data knows.
    covered: int | None = None
    size: int | None = None


@dataclass(slots=True, frozen=True)
class DeckThemes:
    themes: tuple[DerivedTheme, ...]
    # Deck tags that match no type, keyword or Tagger tag.
    unmatched: tuple[str, ...]
    # Theme categories whose cards share nothing distinctive.
    vague: tuple[str, ...]
    # Deck tags that say how the deck plays (cEDH, Tempo, Draw, Creatures): too broad to pick cards by,
    # but what to weigh when researching a replacement.
    broad: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class CoverRules:
    """When a category's shared features stand for it (tuned on the owner's decks, 2026-10-16)."""

    min_cards: int = 5
    # A feature must be this many times commoner in the category than among all cards.
    min_lift: float = 8.0
    # ...and cover this share of the category, and add this many cards not yet covered.
    min_share: float = 0.2
    min_gain: int = 2
    enough: float = 0.7
    max_picks: int = 3
    # Fewer covered than this share and the picks stand for too little of the category.
    min_covered: float = 0.5


@dataclass(slots=True, frozen=True)
class MergedThemes:
    """The themes suggest uses: the profile's own, then the derived ones less what the profile drops."""

    resolved: ResolvedThemes
    derived: DeckThemes
    # The derived themes in use, after the profile's ignore list and [exclude].
    used: tuple[DerivedTheme, ...]
    # Names in the profile's ignore list that are no deck tag or category the deck has.
    unknown_ignores: tuple[str, ...]


def merge_themes(deck: Deck, profile: Profile, db: CardDb) -> MergedThemes:
    derived = derive_themes(deck, db)
    ignored = {name.strip().casefold() for name in profile.ignore_themes}
    used = [
        replace(theme, spec=_without(theme.spec, profile.exclude))
        for theme in derived.themes
        if theme.name.strip().casefold() not in ignored
    ]
    used = [theme for theme in used if not theme.spec.empty]
    specs = [profile.themes, *(theme.spec for theme in used)]
    merged = ThemeSpec(
        types=_once(t for spec in specs for t in spec.types),
        keywords=_once(k for spec in specs for k in spec.keywords),
        tags=_once(t for spec in specs for t in spec.tags),
    )
    known = {name.strip().casefold() for name in (*(deck.tags or ()), *_categories(deck))}
    resolved = resolve_themes(replace(profile, themes=merged), db)
    ids = db.tag_ids(merged.tags)
    named = [(PROFILE_THEME, profile.themes), *((theme.name, theme.spec) for theme in used)]
    labelled = tuple((name, resolve_spec(spec, ids)) for name, spec in named)
    return MergedThemes(
        replace(resolved, labelled=tuple((name, theme) for name, theme in labelled if not theme.empty)),
        derived,
        tuple(used),
        tuple(name for name in profile.ignore_themes if name.strip().casefold() not in known),
    )


def derive_themes(deck: Deck, db: CardDb, rules: CoverRules = CoverRules()) -> DeckThemes:  # noqa: B008 — frozen, so one shared default is safe
    themes: list[DerivedTheme] = []
    unmatched: list[str] = []
    broad: list[str] = []
    for name in deck.tags or ():
        spec = deck_tag_spec(name, db)
        if spec is None:
            broad.append(name)
            continue
        if spec.empty:
            unmatched.append(name)
        else:
            themes.append(DerivedTheme(ThemeSource.DECK_TAG, name, spec))
    vague: list[str] = []
    categories = _theme_categories(db.resolve(deck.main_cards), rules)
    lineage = db.tag_lineage(oracle_id for cards in categories.values() for oracle_id in cards)
    base = _BaseShares(db)
    # A category says what the deck tags don't: in a Vampire deck, every category is full of Vampires.
    # The tags above a deck tag's tags (typal-creature above typal-vampire) say no more.
    tagged = {t for theme in themes for t in theme.spec.tags}
    named = {(FeatureKind.TYPE, t) for theme in themes for t in theme.spec.types} | {
        (FeatureKind.TAG, t) for t in tagged | db.tag_ancestors(tagged)
    }
    for name, cards in categories.items():
        members = _features(cards, lineage)
        # Only features held by enough of the category can be picked, so only theirs are looked up.
        shares = {f: base.share(f) for f, held in members.items() if len(held) >= _needed(len(cards), rules)}
        picks, covered = cover(
            {f: m for f, m in members.items() if f not in named}, shares, len(cards), rules
        )
        if not picks:
            if not cover(members, shares, len(cards), rules)[0]:
                vague.append(name)
            continue
        spec = ThemeSpec(
            types=tuple(value for kind, value in picks if kind is FeatureKind.TYPE),
            tags=tuple(value for kind, value in picks if kind is FeatureKind.TAG),
        )
        themes.append(DerivedTheme(ThemeSource.CATEGORY, name, spec, covered, len(cards)))
    return DeckThemes(tuple(themes), tuple(unmatched), tuple(vague), tuple(broad))


def deck_tag_spec(name: str, db: CardDb) -> ThemeSpec | None:
    """The cards a deck tag asks for; None when too broad, an empty spec for a name nothing matches."""
    key = name.strip().casefold()
    if key in PLAY_STYLES:
        return None
    if key in OVERRIDES:
        return OVERRIDES[key]
    forms = _singulars(key)
    # Roles have their own targets in suggest, and a card type ("Creatures") would take in half of all cards.
    if set(forms) & ROLES_AND_TYPES:
        return None
    words = [form for form in forms if " " not in form]
    type_word = next((w for w in words if db.theme_size(Theme(types=(w,)))), None)
    keyword = next((w for w in words if db.theme_size(Theme(keywords=(w,)))), None)
    slugs = [s for form in forms for s in (_slug(form), f"typal-{_slug(form)}", f"synergy-{_slug(form)}")]
    known = db.tag_ids(slugs)
    return ThemeSpec(
        types=(type_word.capitalize(),) if type_word else (),
        keywords=(keyword,) if keyword else (),
        tags=tuple(dict.fromkeys(s for s in slugs if s in known)),
    )


def cover(
    members: Mapping[Feature, frozenset[str]], base: Mapping[Feature, float], cards: int, rules: CoverRules
) -> tuple[tuple[Feature, ...], int]:
    """The few distinctive features that between them cover most of a category, and how many cards they do."""
    needed = _needed(cards, rules)
    lift = {
        f: (len(held) / cards) / base[f]
        for f, held in members.items()
        if len(held) >= needed and base.get(f, 0) > 0
    }
    pool = {f: members[f] for f, value in lift.items() if value >= rules.min_lift}
    covered: set[str] = set()
    picks: list[Feature] = []
    while pool and len(picks) < rules.max_picks and len(covered) < rules.enough * cards:
        # New cards (squared: covering the category matters most) times how distinctive the feature is, so a
        # specific tag beats an umbrella over it; then a type (it names a tribe outright), then by name.
        best = min(
            pool,
            key=lambda f: (
                -(len(pool[f] - covered) ** 2) * math.log(lift[f]),
                f[0] is not FeatureKind.TYPE,
                f[1],
            ),
        )
        if len(pool[best] - covered) < rules.min_gain:
            break
        picks.append(best)
        covered |= pool.pop(best)
    if len(covered) < rules.min_covered * cards:
        return (), len(covered)
    return tuple(picks), len(covered)


def _without(spec: ThemeSpec, exclude: ThemeSpec) -> ThemeSpec:
    """The spec less every type, keyword and tag the profile excludes."""

    def kept(values: tuple[str, ...], dropped: tuple[str, ...]) -> tuple[str, ...]:
        gone = {value.casefold() for value in dropped}
        return tuple(value for value in values if value.casefold() not in gone)

    return ThemeSpec(
        kept(spec.types, exclude.types), kept(spec.keywords, exclude.keywords), kept(spec.tags, exclude.tags)
    )


def _once(values: Iterable[str]) -> tuple[str, ...]:
    """Each value once, first spelling kept, compared without case."""
    seen: dict[str, str] = {}
    for value in values:
        seen.setdefault(value.casefold(), value)
    return tuple(seen.values())


def _categories(deck: Deck) -> set[str]:
    return {category for card in deck.main_cards for category in card.categories}


def _needed(cards: int, rules: CoverRules) -> int:
    return max(rules.min_gain, math.ceil(rules.min_share * cards))


def _theme_categories(resolved: Sequence[ResolvedCard], rules: CoverRules) -> dict[str, dict[str, str]]:
    """Each theme category's known cards (oracle id to type line), for categories with enough of them."""
    found: dict[str, dict[str, str]] = defaultdict(dict)
    for card in resolved:
        if card.info is None:
            continue
        for category in card.card.categories:
            if category.strip().casefold() not in NOT_THEMES:
                found[category][card.info.oracle_id] = card.info.type_line
    return {name: cards for name, cards in found.items() if len(cards) >= rules.min_cards}


def _features(
    cards: Mapping[str, str], lineage: Mapping[str, frozenset[str]]
) -> dict[Feature, frozenset[str]]:
    held: dict[Feature, set[str]] = defaultdict(set)
    for oracle_id, type_line in cards.items():
        for slug in lineage.get(oracle_id, frozenset()):
            if slug not in TRIVIA_TAGS and not slug.startswith(TRIVIA_PREFIXES):
                held[FeatureKind.TAG, slug].add(oracle_id)
        for subtype in _subtypes(type_line):
            held[FeatureKind.TYPE, subtype].add(oracle_id)
    return {feature: frozenset(ids) for feature, ids in held.items()}


class _BaseShares:
    """How common each feature is among all Commander-legal cards, asked once per feature."""

    def __init__(self, db: CardDb) -> None:
        self._db = db
        self._legal = db.legal_cards()
        self._shares: dict[Feature, float] = {}

    def share(self, feature: Feature) -> float:
        if feature not in self._shares:
            kind, value = feature
            if kind is FeatureKind.TYPE:
                theme = Theme(types=(value,))
            else:
                ids = self._db.tag_ids([value])
                theme = Theme(tag_ids=(ids[value],)) if value in ids else Theme()
            self._shares[feature] = self._db.theme_size(theme) / self._legal if self._legal else 0.0
        return self._shares[feature]


def _subtypes(type_line: str) -> list[str]:
    front = type_line.split(" // ", maxsplit=1)[0]
    return front.split(" — ", maxsplit=1)[1].split() if " — " in front else []


def _singulars(phrase: str) -> list[str]:
    """The phrase and the singulars its last word could have: dwarves, dwarf, dwarve..."""
    head, _, last = phrase.rpartition(" ")
    prefix = f"{head} " if head else ""
    forms = [last]
    for suffix, replacement in (("ves", "f"), ("ies", "y"), ("es", ""), ("s", "")):
        if last.endswith(suffix) and len(last) > len(suffix) + 1:
            forms.append(last[: -len(suffix)] + replacement)
    return list(dict.fromkeys(prefix + form for form in forms))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")
