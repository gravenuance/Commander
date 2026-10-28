"""Scryfall's EUR prices of one card's printings, less the glitches (ADR 0001).

Cardmarket now and then lists a printing at a few cents, such as the Summer Magic Badlands against hundreds
of euros for every other copy, or a Secret Lair foil far below its own nonfoil. Taken as the card's cheapest
price, such a figure makes the card look free to buy and to save into a deck.
"""

import statistics
from collections.abc import Sequence

# A foil dearer than this share of its own printing's nonfoil is believable: it is rarely cheaper at all.
FOIL_MIN_SHARE_OF_NONFOIL = 5
# A price below one twentieth of the median of the card's prices in the same finish is a glitch.
MIN_SHARE_OF_MEDIAN = 20
# Fewer priced printings make a median meaningless; the card is left alone.
MIN_PRICED_FOR_MEDIAN = 3

type Prices = tuple[int | None, int | None]


def drop_glitches(printings: Sequence[Prices]) -> list[Prices]:
    """Each printing's (nonfoil, foil) EUR cents, with a glitch price replaced by None.

    A foil under a fifth of its printing's nonfoil is a glitch, and so is a price under a twentieth of the
    median of the card's prices in the same finish (judged only with three or more printings priced in it).
    """
    nonfoil_median = _median([nonfoil for nonfoil, _ in printings])
    foil_median = _median([foil for _, foil in printings])
    return [
        (
            None if _below_median(nonfoil, nonfoil_median) else nonfoil,
            None if _foil_glitch(nonfoil, foil, foil_median) else foil,
        )
        for nonfoil, foil in printings
    ]


def _foil_glitch(nonfoil: int | None, foil: int | None, foil_median: float | None) -> bool:
    if foil is None:
        return False
    if nonfoil is not None and foil * FOIL_MIN_SHARE_OF_NONFOIL < nonfoil:
        return True
    return _below_median(foil, foil_median)


def _below_median(price: int | None, median: float | None) -> bool:
    return price is not None and median is not None and price * MIN_SHARE_OF_MEDIAN < median


def _median(prices: Sequence[int | None]) -> float | None:
    known = [price for price in prices if price is not None]
    return statistics.median(known) if len(known) >= MIN_PRICED_FOR_MEDIAN else None
