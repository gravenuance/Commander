"""How a deck's play-style deck tags weigh what the meta says it misses (ADR 0031).

Each style favours some kinds of card and may find others off-plan. A staple's weight is the product of
what each of the deck's styles makes of it, and it multiplies the card's share into its score: the order
the missing staples and each source's ideas are taken in.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

from commander.carddb import CardDb, CardInfo, Role
from commander.manabase import ALTERNATIVE_COST_TAGS
from commander.metacombos import pool_stops
from commander.models import Deck

# Cheap enough to hold up beside a stax piece; free spells count whatever their mana value.
CHEAP_MANA_VALUE = 2
HATEBEAR_TAG = "hatebear"
BOUNCE_TAG = "removal-bounce"
HASTE = "haste"
# The weight of a card the data clearly shows decks of the style play more, one it only hints at, and one
# that is off-plan: the inverse of the clear one, since the data shows no stronger dislike.
CLEAR_FIT = 1.25
BORDERLINE_FIT = 1.1
OFF_PLAN_WEIGHT = 0.8


class Style(Enum):
    """A play-style deck tag that weighs the meta's staples, by its casefolded name."""

    TURBO = "turbo"
    STAX = "stax"
    CONTROL = "control"
    TEMPO = "tempo"
    MIDRANGE = "midrange"
    AGGRO = "aggro"
    COMBO = "combo"


class Fit(Enum):
    """How a staple the deck misses sits with its play style."""

    FAVOURED = "fits"
    NEUTRAL = ""
    OFF_PLAN = "off-plan"


class Trait(Enum):
    """What a style looks for in a card; one card has several."""

    RAMP = "ramp"
    TUTOR = "tutor"
    DRAW = "draw"
    CHEAP_DRAW = "cheap draw"
    # One-for-one removal that is not a bounce spell, nor a board wipe.
    REMOVAL = "removal"
    BOUNCE = "bounce"
    WIPE = "board wipe"
    COUNTERSPELL = "counterspell"
    # Removal, bounce or a counterspell that is cheap; board wipes are not interaction of this kind.
    CHEAP_INTERACTION = "cheap interaction"
    STAX = "stax piece"
    PROTECTION = "protection"
    CREATURE = "creature"
    HASTE_CREATURE = "haste creature"


@dataclass(slots=True, frozen=True)
class _Plan:
    """What one style favours, by weight, and the traits it finds off-plan."""

    favoured: Mapping[Trait, float]
    off_plan: frozenset[Trait] = frozenset()


_INTERACTION = frozenset({Trait.REMOVAL, Trait.BOUNCE, Trait.COUNTERSPELL, Trait.WIPE})
_CHEAPENED_INTERACTION = frozenset({Trait.REMOVAL, Trait.BOUNCE, Trait.COUNTERSPELL})
_ROLE_TRAITS = {
    Role.RAMP: Trait.RAMP,
    Role.TUTOR: Trait.TUTOR,
    Role.DRAW: Trait.DRAW,
    Role.COUNTERSPELL: Trait.COUNTERSPELL,
    Role.PROTECTION: Trait.PROTECTION,
}
_PLANS: Mapping[Style, _Plan] = {
    Style.TURBO: _Plan(
        {Trait.RAMP: CLEAR_FIT, Trait.TUTOR: CLEAR_FIT, Trait.DRAW: CLEAR_FIT}, off_plan=_INTERACTION
    ),
    Style.STAX: _Plan({Trait.STAX: CLEAR_FIT, Trait.REMOVAL: CLEAR_FIT, Trait.BOUNCE: CLEAR_FIT}),
    Style.CONTROL: _Plan(
        {
            Trait.REMOVAL: CLEAR_FIT,
            Trait.BOUNCE: CLEAR_FIT,
            Trait.CHEAP_DRAW: CLEAR_FIT,
            Trait.COUNTERSPELL: CLEAR_FIT,
        }
    ),
    Style.TEMPO: _Plan(
        {Trait.REMOVAL: CLEAR_FIT, Trait.CHEAP_INTERACTION: CLEAR_FIT, Trait.BOUNCE: BORDERLINE_FIT},
        off_plan=frozenset({Trait.WIPE}),
    ),
    Style.MIDRANGE: _Plan(
        {Trait.COUNTERSPELL: CLEAR_FIT, Trait.CHEAP_INTERACTION: CLEAR_FIT, Trait.TUTOR: CLEAR_FIT}
    ),
    # A haste creature is a creature too: a further step on top of the creature's.
    Style.AGGRO: _Plan({Trait.CREATURE: BORDERLINE_FIT, Trait.HASTE_CREATURE: BORDERLINE_FIT**2}),
    Style.COMBO: _Plan({Trait.TUTOR: BORDERLINE_FIT, Trait.PROTECTION: BORDERLINE_FIT}),
}


@dataclass(slots=True, frozen=True)
class PlayStyle:
    styles: frozenset[Style] = frozenset()

    @property
    def races(self) -> bool:
        """A Turbo deck with no Control tag: it wins before it needs to answer the meta's combos."""
        return Style.TURBO in self.styles and Style.CONTROL not in self.styles

    @property
    def label(self) -> str:
        return ", ".join(style.value for style in Style if style in self.styles)


NEUTRAL = PlayStyle()


@dataclass(slots=True, frozen=True)
class Weight:
    """A staple's fit with the deck's styles, and the factor its share is multiplied by."""

    fit: Fit
    multiplier: float


def deck_play_style(deck: Deck) -> PlayStyle:
    tags = {tag.strip().casefold() for tag in deck.tags or ()}
    return PlayStyle(frozenset(style for style in Style if style.value in tags))


def staple_weights(names: Iterable[str], style: PlayStyle, db: CardDb) -> dict[str, Weight]:
    """Each named card's weight for the style, by casefolded name; empty for a neutral style.

    A style counts a card once, by the greatest weight of the traits it favours in it. The weights of the
    deck's styles multiply. A card is off-plan, and weighs OFF_PLAN_WEIGHT whatever else favours it, when a
    style finds one of its traits off-plan and neither that style favours the card nor another style
    favours that trait.
    """
    if not style.styles:
        return {}
    infos = db.cards_named(list(names))
    traits = _traits(infos, db)
    return {key: _weigh(style, found) for key, found in traits.items()}


def _traits(infos: Mapping[str, CardInfo], db: CardDb) -> dict[str, frozenset[Trait]]:
    ids = [info.oracle_id for info in infos.values()]
    lineage = db.tag_lineage(ids)
    texts = db.card_texts(ids)
    stax = set(pool_stops(infos.values(), db))
    found: dict[str, frozenset[Trait]] = {}
    for key, info in infos.items():
        tags = lineage.get(info.oracle_id, frozenset())
        text = texts.get(info.oracle_id)
        keywords = text.keywords if text is not None else ()
        found[key] = _card_traits(info, tags, keywords, is_stax=info.name in stax)
    return found


def _card_traits(
    info: CardInfo, tags: frozenset[str], keywords: Iterable[str], *, is_stax: bool
) -> frozenset[Trait]:
    traits = {trait for role, trait in _ROLE_TRAITS.items() if role in info.roles}
    traits |= _removal_traits(info.roles, tags)
    cheap = info.cmc <= CHEAP_MANA_VALUE or bool(tags & ALTERNATIVE_COST_TAGS)
    if cheap and traits & _CHEAPENED_INTERACTION:
        traits.add(Trait.CHEAP_INTERACTION)
    if cheap and Trait.DRAW in traits:
        traits.add(Trait.CHEAP_DRAW)
    if is_stax or HATEBEAR_TAG in tags:
        traits.add(Trait.STAX)
    if "Creature" in info.front_type_line.partition("—")[0].split():
        traits.add(Trait.CREATURE)
        if HASTE in keywords:
            traits.add(Trait.HASTE_CREATURE)
    return frozenset(traits)


def _removal_traits(roles: frozenset[Role], tags: frozenset[str]) -> set[Trait]:
    """A board wipe, else a bounce spell, else other removal: a card is one of the three."""
    if Role.BOARD_WIPE in roles:
        return {Trait.WIPE}
    if BOUNCE_TAG in tags:
        return {Trait.BOUNCE}
    return {Trait.REMOVAL} if Role.REMOVAL in roles else set()


def _weigh(style: PlayStyle, traits: frozenset[Trait]) -> Weight:
    plans = {s: _PLANS[s] for s in Style if s in style.styles}
    favours = {s: _best_favour(plan, traits) for s, plan in plans.items()}
    if any(_off_plan(s, plan, traits, plans, favours) for s, plan in plans.items()):
        return Weight(Fit.OFF_PLAN, OFF_PLAN_WEIGHT)
    multiplier = 1.0
    for favour in favours.values():
        multiplier *= favour
    return Weight(Fit.FAVOURED if multiplier > 1 else Fit.NEUTRAL, multiplier)


def _best_favour(plan: _Plan, traits: frozenset[Trait]) -> float:
    return max((weight for trait, weight in plan.favoured.items() if trait in traits), default=1.0)


def _off_plan(
    owner: Style,
    plan: _Plan,
    traits: frozenset[Trait],
    plans: Mapping[Style, _Plan],
    favours: Mapping[Style, float],
) -> bool:
    if favours[owner] > 1:
        return False
    return any(
        not any(trait in other.favoured for style, other in plans.items() if style is not owner)
        for trait in plan.off_plan & traits
    )
