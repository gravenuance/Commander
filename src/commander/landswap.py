"""Whether a swap that takes a land out or puts one in leaves the mana base sound.

A cut target or an idea that is a land is allowed only while the land count stays within LAND_TOLERANCE of
Karsten's count for the deck or inside the range of the lists compared with, and every colour keeps the
sources its spells need (ADR 0019).
"""

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from commander.carddb import CardDb, CardMana
from commander.manabase import ALTERNATIVE_COST_TAGS, LandCount, ManaBase, analyse_mana, info_is_land
from commander.manaplan import LAND_TOLERANCE, deck_with_counts
from commander.meta import performing_entries
from commander.metadata import CEDH_BRACKET, MetaData
from commander.models import Deck

log = logging.getLogger(__name__)

# A need is a card and the colour it asks sources of.
type _Need = tuple[str, str]
# Sources are counted to a tenth; this keeps float noise from reading as a loss.
_SOURCE_NOISE = 0.05


def comparison_lists(deck: Deck, data: MetaData | None) -> tuple[str, list[dict[str, int]]]:
    """What the land count is compared with: the label and the lists, each by card name and count.

    Bracket 5 compares with the commander's top-cut tournament lists; any other deck with its recent
    Archidekt decks that say how many of each basic land they run (ADR 0019).
    """
    if data is None:
        return "", []
    if deck.bracket == CEDH_BRACKET:
        return "top-cut lists", [{c.name: c.count for c in entry.cards} for entry in performing_entries(data)]
    lists: list[dict[str, int]] = []
    for peer in data.peers.data.decks if data.peers.data else ():
        # Peer caches from before ADR 0019 don't say how many of each basic land a deck runs.
        if peer.multiples is None:
            continue
        counts = dict.fromkeys(peer.cards, 1)
        counts.update(peer.multiples)
        lists.append(counts)
    return "recent decks", lists


@dataclass(slots=True, frozen=True)
class ManaGuard:
    """The deck's mana base as it stands, to judge a swap of one card for another against."""

    deck: Deck
    # Every card the deck holds and the swaps may bring in, by casefolded name.
    cards: Mapping[str, CardMana]
    alternative_cost: frozenset[str]
    # The fewest and the most lands the compared lists run; None without lists.
    land_range: tuple[float, float] | None
    # The sources each card short of them has now, by colour: a swap may not add to these nor lower them.
    short: Mapping[_Need, float]

    @staticmethod
    def of(
        deck: Deck,
        cards: Mapping[str, CardMana],
        *,
        alternative_cost: Iterable[str] = (),
        land_range: tuple[float, float] | None = None,
    ) -> "ManaGuard":
        alternative = frozenset(name.casefold() for name in alternative_cost)
        report = analyse_mana(deck, cards, alternative_cost=alternative)
        return ManaGuard(deck, cards, alternative, land_range, _shortfalls(report))

    def with_cards(self, cards: Mapping[str, CardMana]) -> "ManaGuard":
        """The guard knowing these cards too, such as the ideas a swap may bring in."""
        return replace(self, cards={**cards, **self.cards})

    def allows(self, add: str, cut: str) -> bool:
        """Whether swapping the deck's card `cut` for `add` is sound; one of them is a land."""
        added, cut_card = self.cards.get(add.casefold()), self.cards.get(cut.casefold())
        if added is None or cut_card is None:
            log.warning("No mana data for %s or %s: the swap is refused", add, cut)
            return False
        counts = {card.name: card.quantity for card in self.deck.main_cards}
        counts[cut] = counts.get(cut, 0) - 1
        counts[add] = counts.get(add, 0) + 1
        after = analyse_mana(
            deck_with_counts(self.deck, counts), self.cards, alternative_cost=self.alternative_cost
        )
        moves_count = info_is_land(added) != info_is_land(cut_card)
        return (not moves_count or self._count_holds(after.lands)) and self._sources_hold(after)

    def _count_holds(self, lands: LandCount) -> bool:
        if abs(round(lands.suggested - lands.lands, 2)) <= LAND_TOLERANCE:
            return True
        return self.land_range is not None and self.land_range[0] <= lands.lands <= self.land_range[1]

    def _sources_hold(self, after: ManaBase) -> bool:
        for colour in after.colours:
            for need in colour.shortfalls:
                had = self.short.get((need.card, colour.colour))
                if had is None or need.have < had - _SOURCE_NOISE:
                    return False
        return True


def mana_guard(deck: Deck, db: CardDb, data: MetaData | None) -> ManaGuard:
    """The guard for a deck: its cards' mana data, and the land range of the lists it is compared with."""
    label, lists = comparison_lists(deck, data)
    names = {card.name for card in deck.main_cards} | {name for listed in lists for name in listed}
    cards = db.card_mana(names)
    ids = [info.oracle_id for card in deck.main_cards if (info := cards.get(card.name.casefold()))]
    lineage = db.tag_lineage(ids)
    alternative = [
        info.name
        for info in cards.values()
        if lineage.get(info.oracle_id, frozenset()) & ALTERNATIVE_COST_TAGS
    ]
    report = analyse_mana(
        deck, cards, alternative_cost=alternative, comparison=(label, lists) if lists else None
    )
    compared = report.comparison
    land_range = (compared.lands_low, compared.lands_high) if compared is not None else None
    return ManaGuard(
        deck, cards, frozenset(name.casefold() for name in alternative), land_range, _shortfalls(report)
    )


def _shortfalls(report: ManaBase) -> dict[_Need, float]:
    return {(need.card, colour.colour): need.have for colour in report.colours for need in colour.shortfalls}
