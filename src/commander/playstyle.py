"""How a deck's play-style deck tags weigh what the meta says it misses (ADR 0022).

A Turbo deck races: the meta's interaction staples are off its plan, and a meta combo it has no answer to
is no gap. A Stax deck wants stax pieces and cheap interaction; a Control deck wants interaction. Other
play styles weigh nothing.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

from commander.carddb import CardDb, CardInfo, Role
from commander.manabase import ALTERNATIVE_COST_TAGS
from commander.metacombos import pool_answers
from commander.models import Deck

INTERACTION = frozenset({Role.REMOVAL, Role.COUNTERSPELL, Role.BOARD_WIPE})
SPEED = frozenset({Role.RAMP, Role.TUTOR, Role.DRAW})
# Cheap enough to hold up beside a stax piece; free spells count whatever their mana value.
CHEAP_MANA_VALUE = 2
HATEBEAR_TAG = "hatebear"


class Style(Enum):
    """A play-style deck tag that weighs the meta's staples, by its casefolded name."""

    TURBO = "turbo"
    STAX = "stax"
    CONTROL = "control"


class Fit(Enum):
    """How a staple the deck misses sits with its play style; rows sort in this order."""

    FAVOURED = "fits"
    NEUTRAL = ""
    OFF_PLAN = "off-plan"


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


def deck_play_style(deck: Deck) -> PlayStyle:
    tags = {tag.strip().casefold() for tag in deck.tags or ()}
    return PlayStyle(frozenset(style for style in Style if style.value in tags))


@dataclass(slots=True, frozen=True)
class _Traits:
    speed: bool
    interaction: bool
    cheap_interaction: bool
    stax: bool


def staple_fits(names: Iterable[str], style: PlayStyle, db: CardDb) -> dict[str, Fit]:
    """Each named card's fit with the style, by casefolded name; every card is neutral for a neutral style."""
    wanted = list(names)
    if not style.styles:
        return {}
    infos = db.cards_named(wanted)
    traits = _traits(infos, db)
    return {key: _fit(style, traits[key]) for key in traits}


def _traits(infos: Mapping[str, CardInfo], db: CardDb) -> dict[str, _Traits]:
    lineage = db.tag_lineage(info.oracle_id for info in infos.values())
    stax = set(pool_answers((info.name for info in infos.values()), db).stops)
    found: dict[str, _Traits] = {}
    for key, info in infos.items():
        tags = lineage.get(info.oracle_id, frozenset())
        interaction = bool(info.roles & INTERACTION)
        cheap = info.cmc <= CHEAP_MANA_VALUE or bool(tags & ALTERNATIVE_COST_TAGS)
        found[key] = _Traits(
            speed=bool(info.roles & SPEED),
            interaction=interaction,
            cheap_interaction=interaction and cheap,
            stax=info.name in stax or HATEBEAR_TAG in tags,
        )
    return found


def _fit(style: PlayStyle, traits: _Traits) -> Fit:
    favoured = (
        (Style.TURBO in style.styles and traits.speed)
        or (Style.STAX in style.styles and (traits.stax or traits.cheap_interaction))
        or (Style.CONTROL in style.styles and traits.interaction)
    )
    if favoured:
        return Fit.FAVOURED
    if style.races and traits.interaction:
        return Fit.OFF_PLAN
    return Fit.NEUTRAL


def fit_order(fit: Fit) -> int:
    return list(Fit).index(fit)
