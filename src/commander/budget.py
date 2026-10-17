"""A casual deck's budget, read from what it already runs (ADR 0025).

Below bracket 5 a deck shows its budget in its cards: an idea a little over its average card still fits it,
and one dearer than its dearest card by more than a margin doesn't. Bracket 5 decks have no budget beyond the
profile's price limit, since cEDH lists buy what wins.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from commander.carddb import ResolvedCard
from commander.metadata import CEDH_BRACKET

# Ideas may cost this much above the deck's dearest card, so a few options beyond the working budget remain.
OVER_BUDGET_MARGIN = 1.25
# An idea this much above the deck's average card is still within budget: Boros Charm at EUR 4.19 fits a
# deck averaging EUR 3.72, as the owner judged it (2026-10-17).
WITHIN_MARGIN = 1.25


@dataclass(slots=True, frozen=True)
class Budget:
    # The deck's average card: ideas up to WITHIN_MARGIN over it come first. None when nothing tells it.
    working: int | None
    # Nothing dearer is offered: OVER_BUDGET_MARGIN above the dearest card, or the profile's price limit.
    ceiling: int | None

    @property
    def line(self) -> int | None:
        """The dearest an idea within budget may be; never above the ceiling."""
        if self.working is None:
            return None
        line = round(self.working * WITHIN_MARGIN)
        return min(line, self.ceiling) if self.ceiling is not None else line

    def within(self, cents: int | None) -> bool:
        """Whether an idea at this price fits the working budget; with one, an unpriced idea doesn't."""
        line = self.line
        return line is None or (cents is not None and cents <= line)

    def allows(self, cents: int | None) -> bool:
        """Whether an idea at this price may be offered at all; an unpriced one may not under a ceiling."""
        return self.ceiling is None or (cents is not None and cents <= self.ceiling)

    def json(self) -> dict[str, object]:
        return {"working": self.working, "within": self.line, "ceiling": self.ceiling}


NO_BUDGET = Budget(None, None)


def deck_budget(bracket: int | None, prices: Iterable[tuple[int, int | None]], limit: int | None) -> Budget:
    """The budget from the deck's (copies, cents) pairs, basics left out by the caller; limit is the
    profile's price limit, which caps both."""
    if bracket == CEDH_BRACKET:
        return Budget(None, limit)
    priced = [(copies, cents) for copies, cents in prices if cents is not None]
    dearest = max((cents for _, cents in priced), default=None)
    ceiling = round(dearest * OVER_BUDGET_MARGIN) if dearest is not None else None
    return Budget(_capped(average_cents(priced), limit), _capped(ceiling, limit))


def resolved_budget(bracket: int | None, main: Sequence[ResolvedCard], limit: int | None) -> Budget:
    """deck_budget over the deck's resolved main cards, basics left out: they cost next to nothing."""
    prices = [
        (r.card.quantity, r.info.cheapest_cents)
        for r in main
        if r.info is not None and not r.info.front_type_line.startswith("Basic ")
    ]
    return deck_budget(bracket, prices, limit)


def average_cents(prices: Iterable[tuple[int, int | None]]) -> int | None:
    """The average price from (copies, cents) pairs, weighted by copies, unpriced ones left out."""
    priced = [(copies, cents) for copies, cents in prices if cents is not None]
    total = sum(copies for copies, _ in priced)
    return round(sum(copies * cents for copies, cents in priced) / total) if total else None


def _capped(cents: int | None, limit: int | None) -> int | None:
    return min((c for c in (cents, limit) if c is not None), default=None)
