"""How many cards of each role and theme recent decks with the commander hold, to judge a cut against.

A cut that changes the deck's roles or themes is only fair when the deck stays within what other players of
the commander run: the 25th percentile of a role's count is the least a deck may fall to, the median what it
may be said to over-run.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from statistics import median, quantiles

from commander.analysis import ANALYZED_ROLES
from commander.answers import card_key
from commander.carddb import CardDb, CardInfo, Role, Theme
from commander.meta import MIN_PEER_LISTS
from commander.metadata import PeerList


@dataclass(slots=True, frozen=True)
class RoleBand:
    """The peer decks' count of one role: the least a deck may fall to, and the middle."""

    floor: float
    median: float


@dataclass(slots=True, frozen=True)
class PeerCounts:
    roles: Mapping[Role, RoleBand]
    # The median peer count of each theme, by the name the rules know it by; a theme the peers were not
    # counted for (the profile's own words) is missing.
    themes: Mapping[str, float]


def peer_counts(
    peers: Sequence[PeerList],
    commanders: Collection[str],
    db: CardDb,
    themes: Mapping[str, Theme],
    *,
    nonland_only: Collection[str] = (),
) -> PeerCounts | None:
    """Role and theme counts over the peer decks, commanders left out; None below MIN_PEER_LISTS decks.

    Each card counts once per deck, as `leanings` does. A role is the card's Tagger role, themes are
    matched like the deck's own; those named in nonland_only count nonland cards.
    """
    if len(peers) < MIN_PEER_LISTS:
        return None
    infos = db.cards_named(
        {name for p in peers for name in p.cards if card_key(name) not in commanders},
    )
    decks = [_held(p, commanders, infos) for p in peers]
    roles = {
        role: _band([sum(1 for info in held if role in info.roles) for held in decks])
        for role in ANALYZED_ROLES
    }
    return PeerCounts(roles, _theme_medians(decks, themes, set(nonland_only), db))


def _held(peer: PeerList, commanders: Collection[str], infos: Mapping[str, CardInfo]) -> list[CardInfo]:
    names = {name.casefold() for name in peer.cards if card_key(name) not in commanders}
    return [info for name in names if (info := infos.get(name)) is not None]


def _band(counts: Sequence[int]) -> RoleBand:
    return RoleBand(quantiles(counts, n=4, method="inclusive")[0], median(counts))


def _theme_medians(
    decks: Sequence[Sequence[CardInfo]],
    themes: Mapping[str, Theme],
    nonland_only: Collection[str],
    db: CardDb,
) -> dict[str, float]:
    every = {info.oracle_id for held in decks for info in held}
    nonland = {info.oracle_id for held in decks for info in held if not info.is_land}
    found: dict[str, float] = {}
    for name, theme in themes.items():
        matching = db.matching(theme, nonland if name in nonland_only else every)
        found[name] = median(sum(1 for info in held if info.oracle_id in matching) for held in decks)
    return found
