"""The deck cards a board card could replace: picked by hand, else the weakest cards in the same role."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from commander.analysis import ANALYZED_ROLES
from commander.answers import card_key
from commander.board import Actor, BoardCard
from commander.carddb import CardInfo, ResolvedCard, Role
from commander.deckprofile import Profile
from commander.models import COMMANDER_CATEGORY
from commander.suggest import fills_role, name_keys, protected_cards

MAX_TARGETS = 3
_PICKERS = frozenset({Actor.YOU, Actor.CLAUDE})


class TargetSource(Enum):
    # Named on the board card by whoever proposed it (Claude's research, meta outliers).
    PICKED = "picked"
    # Worked out here from the card's roles, the meta and popularity.
    ROLE = "role"


@dataclass(slots=True, frozen=True)
class Target:
    name: str
    reason: str
    source: TargetSource


def targets_for(
    card: BoardCard,
    info: CardInfo | None,
    main: Sequence[ResolvedCard],
    *,
    protected: Collection[str],
    shares: Mapping[str, float] | None,
) -> tuple[Target, ...]:
    """The replace options you or Claude gave the card, still in the deck; else the weakest in its role.

    A card with roles is matched to deck cards sharing one, a card without any to deck cards filling none;
    lands only to lands. Weakest means the smallest share of the meta's lists (when shares are known), then
    the least played on EDHREC, unranked cards last. Commanders and protected oracle ids are never offered.
    """
    in_deck = {r.card.name.casefold() for r in main}
    # Tools attach one generic cut list to all their ideas; only a person's or Claude's options are picks.
    options = card.replace if card.source in _PICKERS else ()
    picked = tuple(
        Target(o.name, o.reason, TargetSource.PICKED) for o in options if o.name.casefold() in in_deck
    )
    if picked or info is None:
        return picked
    roles = [role for role in ANALYZED_ROLES if role in info.roles]
    pool = [r for r in main if _eligible(r, info, protected) and _same_role(r, roles)]
    ranked = sorted(pool, key=lambda r: _weakness(r, shares))
    return tuple(
        Target(r.card.name, _reason(r, roles, shares), TargetSource.ROLE) for r in ranked[:MAX_TARGETS]
    )


def kept_by_profile(main: Sequence[ResolvedCard], profile: Profile) -> frozenset[str]:
    """Oracle ids of the deck cards the profile keeps by name or category: never targets."""
    keep_names = {key for name in profile.keep_cards for key in name_keys(name)}
    keep_categories = {category.casefold() for category in profile.keep_categories}
    return protected_cards(main, keep_names, keep_categories)


def _eligible(r: ResolvedCard, info: CardInfo, protected: Collection[str]) -> bool:
    other = r.info
    return (
        other is not None
        and COMMANDER_CATEGORY not in r.card.categories
        and other.oracle_id not in protected
        and other.oracle_id != info.oracle_id
        and other.is_land == info.is_land
    )


def _same_role(r: ResolvedCard, roles: Sequence[Role]) -> bool:
    if roles:
        return any(fills_role(r, role) for role in roles)
    return not any(fills_role(r, role) for role in ANALYZED_ROLES)


def _weakness(r: ResolvedCard, shares: Mapping[str, float] | None) -> tuple[float, bool, int, str]:
    share = shares.get(card_key(r.card.name), 0.0) if shares is not None else 0.0
    rank = r.info.edhrec_rank if r.info else None
    return (share, rank is None, -(rank or 0), r.card.name.casefold())


def _reason(r: ResolvedCard, roles: Sequence[Role], shares: Mapping[str, float] | None) -> str:
    shared = [role.value for role in roles if fills_role(r, role)]
    parts = [f"same role ({', '.join(shared)})" if shared else "fills no role either"]
    if shares is not None:
        parts.append(f"in {shares.get(card_key(r.card.name), 0.0):.0%} of meta lists")
    rank = r.info.edhrec_rank if r.info else None
    parts.append(f"EDHREC rank {rank:,}" if rank is not None else "unranked on EDHREC")
    return "; ".join(parts)
