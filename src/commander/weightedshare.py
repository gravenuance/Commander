"""One share per card from every list read about a commander: its EDHTop16 lists and recent Archidekt decks
(ADR 0026).

Each list or deck gets a weight, recent and well placed ones more. A source's weights are scaled to a mean
of one, so each list still counts as about one vote and the weights only reorder within a source; the two
sources then pool by their list counts. A card counts once per list.

A card is only an idea when the lower bound of its share is high enough: the Wilson score interval, taken on
the Kish effective sample size of the weights, so a few heavy lists count as the few they are.
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from commander.answers import card_key
from commander.deckcache import Clock
from commander.edhtop16 import Entry
from commander.metadata import MetaData, PeerList, listed_entries

# A list or deck weighs half as much for every 30 days since its tournament or last update.
HALF_LIFE_DAYS = 30.0
# The event size a placement counts once over: the default `min_event_size`.
EVENT_SIZE_BASE = 16
# Under about ten players the size factor would reach zero or below; a small event stays a small voice.
MIN_SIZE_FACTOR = 0.25
# EDHTop16 gives some entries no standing: they weigh as the middle of the field.
UNKNOWN_PERCENTILE = 0.5
# 95% confidence, two-sided.
WILSON_Z = 1.96
# A card is an idea when the Wilson lower bound of its weighted share reaches this. A backtest's best F1 was
# at 0.32; 0.30 is within noise of it and was chosen for recall.
IDEA_LOWER_BOUND = 0.30


@dataclass(slots=True, frozen=True)
class SourceShare:
    """A card's share of one source: the weighted share, and the raw count of lists playing it."""

    share: float
    count: int
    total: int


@dataclass(slots=True, frozen=True)
class WeightedShare:
    """A card's share of all the lists read (`count` of `total` run it) and its part from each source."""

    share: float
    count: int
    total: int
    # None when that source gave no lists.
    meta: SourceShare | None
    archidekt: SourceShare | None


@dataclass(slots=True, frozen=True)
class ShareTable:
    """Every card's weighted share, by `card_key`; falsy when no list was read."""

    cards: Mapping[str, WeightedShare]
    # What a card no list plays gets: none of the lists read.
    absent: WeightedShare
    # The Kish effective sample size of every list's weight; 0 when no list was read.
    effective_n: float = 0.0

    def __bool__(self) -> bool:
        return self.absent.total > 0

    def of(self, name: str) -> WeightedShare | None:
        """The card's share, zero when no list plays it; None when no list was read at all."""
        return self.cards.get(card_key(name), self.absent) if self else None

    def fractions(self) -> dict[str, float]:
        """Just the weighted shares, by `card_key`."""
        return {key: share.share for key, share in self.cards.items()}

    def is_idea(self, name: str) -> bool:
        """Whether the Wilson lower bound of the card's weighted share, on the effective sample size, reaches
        IDEA_LOWER_BOUND."""
        share = self.cards.get(card_key(name), self.absent)
        return wilson_lower_bound(share.share, self.effective_n) >= IDEA_LOWER_BOUND


# The key of no card, which no list plays.
_NO_CARD = ""
NO_SHARES = ShareTable({}, WeightedShare(0.0, 0, 0, None, None))


def wilson_lower_bound(share: float, n: float, z: float = WILSON_Z) -> float:
    """The lower end of the Wilson score interval for a share seen in n trials; 0 without any."""
    if n <= 0:
        return 0.0
    centre = share + z * z / (2 * n)
    margin = z * math.sqrt(share * (1 - share) / n + z * z / (4 * n * n))
    return max((centre - margin) / (1 + z * z / n), 0.0)


def effective_sample_size(weights: Iterable[float]) -> float:
    """Kish's n = (sum w)^2 / sum w^2: how many equally weighted lists would carry as much evidence."""
    held = list(weights)
    squares = sum(w * w for w in held)
    return sum(held) ** 2 / squares if squares > 0 else 0.0


def recency_weight(age_days: float) -> float:
    return 0.5 ** (max(age_days, 0.0) / HALF_LIFE_DAYS)


def size_factor(players: int) -> float:
    # One doubling of the field over EVENT_SIZE_BASE counts one more.
    return max(1 + math.log2(max(players, 1) / EVENT_SIZE_BASE), MIN_SIZE_FACTOR)


def placement_weight(entry: Entry) -> float:
    """(N - s + 1) / N for standing s in an event of N players, so 1.0 for the winner and 1/N for last (a
    standing of 0, unknown, gets UNKNOWN_PERCENTILE), times the event's size factor."""
    players = max(entry.tournament.size, 1)
    if entry.standing < 1:
        percentile = UNKNOWN_PERCENTILE
    else:
        percentile = min(max(1 - (entry.standing - 1) / players, 0.0), 1.0)
    return percentile * size_factor(players)


def weighted_shares(data: MetaData | None, *, now: Clock) -> ShareTable:
    """Each card's weighted share of the commander's lists with a decklist in the window and of its recent
    Archidekt decks; NO_SHARES when there are none."""
    if data is None:
        return NO_SHARES
    moment = now()
    lists = listed_entries(data)
    peers = data.peers.data.decks if data.peers.data else ()
    meta = _Source.of(
        [_keys(c.name for c in e.cards) for e in lists], [_list_weight(e, moment) for e in lists]
    )
    archidekt = _Source.of([_keys(p.cards) for p in peers], [_deck_weight(p, moment) for p in peers])
    total = meta.total + archidekt.total
    if not total:
        return NO_SHARES
    cards = {
        key: WeightedShare(
            (meta.weight.get(key, 0.0) + archidekt.weight.get(key, 0.0)) / total,
            meta.count.get(key, 0) + archidekt.count.get(key, 0),
            total,
            meta.share_of(key),
            archidekt.share_of(key),
        )
        for key in meta.count.keys() | archidekt.count.keys()
    }
    return ShareTable(
        cards,
        WeightedShare(0.0, 0, total, meta.share_of(_NO_CARD), archidekt.share_of(_NO_CARD)),
        effective_sample_size([*meta.scaled, *archidekt.scaled]),
    )


def list_age_days(entry: Entry, moment: datetime) -> int:
    """Whole days from the entry's tournament to `moment` (UTC date); negative when it is still to come."""
    return (moment.astimezone(UTC).date() - entry.tournament.held_on).days


def event_weight(entry: Entry, moment: datetime) -> float:
    """A list's weight without its placing: half per HALF_LIFE_DAYS since its tournament, times the event's
    size factor."""
    return recency_weight(list_age_days(entry, moment)) * size_factor(entry.tournament.size)


def _list_weight(entry: Entry, moment: datetime) -> float:
    return recency_weight(list_age_days(entry, moment)) * placement_weight(entry)


def _deck_weight(deck: PeerList, moment: datetime) -> float:
    return recency_weight((moment - deck.deck.updated_at) / timedelta(days=1))


@dataclass(slots=True, frozen=True)
class _Source:
    total: int
    # Per card, the scaled weights of the lists playing it summed, and how many lists play it.
    weight: Mapping[str, float]
    count: Mapping[str, int]
    # The list weights scaled to a mean of one.
    scaled: Sequence[float]

    @classmethod
    def of(cls, held: Sequence[frozenset[str]], weights: Sequence[float]) -> "_Source":
        scaled = _scaled(weights)
        weight: dict[str, float] = {}
        count: dict[str, int] = {}
        for keys, w in zip(held, scaled, strict=True):
            for key in keys:
                weight[key] = weight.get(key, 0.0) + w
                count[key] = count.get(key, 0) + 1
        return cls(len(held), weight, count, scaled)

    def share_of(self, key: str) -> SourceShare | None:
        if not self.total:
            return None
        return SourceShare(self.weight.get(key, 0.0) / self.total, self.count.get(key, 0), self.total)


def _scaled(weights: Sequence[float]) -> list[float]:
    """The weights divided by their mean, so they add up to the number of lists; all ones when none count."""
    mean = sum(weights) / len(weights) if weights else 0.0
    return [w / mean for w in weights] if mean > 0 else [1.0] * len(weights)


def _keys(names: Iterable[str]) -> frozenset[str]:
    return frozenset(card_key(name) for name in names)


def share_label(share: WeightedShare) -> str:
    return f"{share.share:.0%}"


def share_reason(share: WeightedShare) -> str:
    """'in 34% of meta lists and recent decks (12/35)', naming only the sources there are."""
    sources = " and ".join(
        name for name, part in (("meta lists", share.meta), ("recent decks", share.archidekt)) if part
    )
    return f"in {share_label(share)} of {sources} ({share.count}/{share.total})"


def share_tip(share: WeightedShare) -> str:
    """'12/35 · meta 41% (8/20 lists) · Archidekt 27% (4/15 decks)'; the raw count leads only when two
    sources add up to it."""
    parts: list[str] = []
    if share.meta:
        parts.append(f"meta {share.meta.share:.0%} ({share.meta.count}/{share.meta.total} lists)")
    if share.archidekt:
        parts.append(
            f"Archidekt {share.archidekt.share:.0%} ({share.archidekt.count}/{share.archidekt.total} decks)"
        )
    if share.meta and share.archidekt:
        parts.insert(0, f"{share.count}/{share.total}")
    return " · ".join(parts)
