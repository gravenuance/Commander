"""Deck statistics: mana curve, card types, colour pips and how many cards fill each role."""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from commander.carddb import CardInfo, ResolvedCard, Role
from commander.models import Deck, DeckCard

ANALYZED_ROLES = (
    Role.RAMP,
    Role.DRAW,
    Role.REMOVAL,
    Role.BOARD_WIPE,
    Role.TUTOR,
    Role.PROTECTION,
    Role.RECURSION,
    Role.COUNTERSPELL,
)
# Archidekt category names (casefolded) that mean the same as a role; "counters" usually means +1/+1 counters.
CATEGORY_ALIASES: Mapping[Role, frozenset[str]] = {
    Role.RAMP: frozenset({"ramp", "mana", "mana ramp", "acceleration"}),
    Role.DRAW: frozenset({"draw", "card draw", "card advantage"}),
    Role.REMOVAL: frozenset({"removal", "spot removal", "interaction"}),
    Role.BOARD_WIPE: frozenset(
        {"board wipe", "board wipes", "boardwipe", "wipe", "wipes", "sweeper", "sweepers"}
    ),
    Role.TUTOR: frozenset({"tutor", "tutors"}),
    Role.PROTECTION: frozenset({"protection"}),
    Role.RECURSION: frozenset({"recursion", "graveyard recursion"}),
    Role.COUNTERSPELL: frozenset({"counterspell", "counterspells", "counter magic"}),
}
CARD_TYPES = ("Creature", "Artifact", "Enchantment", "Planeswalker", "Battle", "Instant", "Sorcery", "Land")
PIP_COLORS = "WUBRGC"
CURVE_TOP = 7
_SYMBOL = re.compile(r"\{([^}]+)\}")


@dataclass(slots=True, frozen=True)
class RoleCount:
    role: Role
    # None when the deck has no category for the role, or the tag data lacks it.
    yours: int | None
    tagged: int | None
    only_yours: tuple[str, ...]
    only_tagged: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class DeckAnalysis:
    deck: Deck
    cards: int
    lands: int
    average_mana_value: float | None
    # Index n counts nonland cards of mana value n; the last bucket holds CURVE_TOP and above.
    curve: tuple[int, ...]
    types: Mapping[str, int]
    pips: Mapping[str, int]
    roles: tuple[RoleCount, ...]
    game_changers: tuple[str, ...]
    not_legal: tuple[str, ...]
    unknown: tuple[str, ...]


def analyze(deck: Deck, resolved: Sequence[ResolvedCard], available_roles: frozenset[Role]) -> DeckAnalysis:
    """Statistics for the main deck; resolved must cover the deck's main cards."""
    known = _known(resolved)
    lands = sum(card.quantity for card, info in known if info.is_land)
    spells = [(info.cmc, card.quantity) for card, info in known if not info.is_land]
    spell_count = sum(quantity for _, quantity in spells)
    curve = [0] * (CURVE_TOP + 1)
    for cmc, quantity in spells:
        curve[min(int(cmc), CURVE_TOP)] += quantity
    types: dict[str, int] = dict.fromkeys(CARD_TYPES, 0)
    pips: dict[str, int] = dict.fromkeys(PIP_COLORS, 0)
    for card, info in known:
        # Types come before the dash; subtypes like "Elf" or "Equipment" after it.
        for word in info.front_type_line.split("—")[0].split():
            if word in types:
                types[word] += card.quantity
        for color, count in count_pips(info.mana_cost).items():
            pips[color] += count * card.quantity
    return DeckAnalysis(
        deck=deck,
        cards=deck.main_count,
        lands=lands,
        average_mana_value=sum(c * q for c, q in spells) / spell_count if spell_count else None,
        curve=tuple(curve),
        types=types,
        pips=pips,
        roles=tuple(_role_count(deck, resolved, role, available_roles) for role in ANALYZED_ROLES),
        game_changers=_names(card for card, info in known if info.game_changer),
        not_legal=_names(card for card, info in known if not info.commander_legal),
        unknown=_names(r.card for r in resolved if r.card.in_deck and r.info is None),
    )


def count_pips(mana_cost: str) -> dict[str, int]:
    """Coloured and colourless pips in a cost; a hybrid symbol counts once for each colour in it."""
    pips: dict[str, int] = {}
    for symbol in _SYMBOL.findall(mana_cost):
        for part in set(symbol.split("/")):
            if part in PIP_COLORS:
                pips[part] = pips.get(part, 0) + 1
    return pips


def _role_count(
    deck: Deck, resolved: Sequence[ResolvedCard], role: Role, available_roles: frozenset[Role]
) -> RoleCount:
    aliases = CATEGORY_ALIASES.get(role, frozenset())
    deck_categories = {category.casefold() for card in deck.cards for category in card.categories}
    has_category = bool(aliases & deck_categories)
    main = [r for r in resolved if r.card.in_deck]
    mine = [r for r in main if aliases & {category.casefold() for category in r.card.categories}]
    tagged = [r for r in main if r.info is not None and role in r.info.roles]
    can_tag = role in available_roles
    only_yours: tuple[str, ...] = ()
    only_tagged: tuple[str, ...] = ()
    if has_category and can_tag:
        mine_names = {r.card.name for r in mine}
        tagged_names = {r.card.name for r in tagged}
        only_yours = tuple(sorted(mine_names - tagged_names, key=str.casefold))
        only_tagged = tuple(sorted(tagged_names - mine_names, key=str.casefold))
    return RoleCount(
        role=role,
        yours=sum(r.card.quantity for r in mine) if has_category else None,
        tagged=sum(r.card.quantity for r in tagged) if can_tag else None,
        only_yours=only_yours,
        only_tagged=only_tagged,
    )


def _known(resolved: Sequence[ResolvedCard]) -> list[tuple[DeckCard, CardInfo]]:
    return [(r.card, r.info) for r in resolved if r.card.in_deck and r.info is not None]


def _names(cards: Iterable[DeckCard]) -> tuple[str, ...]:
    return tuple(sorted({card.name for card in cards}, key=str.casefold))
