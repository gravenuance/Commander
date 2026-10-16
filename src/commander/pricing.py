"""Deck value in EUR (Cardmarket prices via Scryfall), for the printings in the deck and the cheapest ones."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from commander.carddb import ResolvedCard
from commander.models import Deck

NONFOIL_FINISH = "Normal"


class PriceSource(Enum):
    PRINTING = "printing"
    # The deck's printing has no price for its finish, so the card's cheapest printing stands in.
    CHEAPEST = "cheapest"
    NONE = "none"


@dataclass(slots=True, frozen=True)
class CardPrice:
    name: str
    quantity: int
    unit_cents: int | None
    source: PriceSource
    cheapest_cents: int | None

    @property
    def total_cents(self) -> int:
        return (self.unit_cents or 0) * self.quantity

    @property
    def cheapest_total_cents(self) -> int:
        unit = self.cheapest_cents if self.cheapest_cents is not None else self.unit_cents
        return (unit or 0) * self.quantity


@dataclass(slots=True, frozen=True)
class DeckPrice:
    deck: Deck
    # Most valuable first.
    cards: tuple[CardPrice, ...]

    @property
    def total_cents(self) -> int:
        return sum(card.total_cents for card in self.cards)

    @property
    def cheapest_total_cents(self) -> int:
        return sum(card.cheapest_total_cents for card in self.cards)

    @property
    def unpriced(self) -> tuple[str, ...]:
        return tuple(card.name for card in self.cards if card.source is PriceSource.NONE)

    @property
    def estimated(self) -> tuple[str, ...]:
        return tuple(card.name for card in self.cards if card.source is PriceSource.CHEAPEST)


def price_deck(deck: Deck, resolved: Sequence[ResolvedCard]) -> DeckPrice:
    """Prices for the main deck; resolved must cover the deck's main cards."""
    cards = [card_price(r) for r in resolved if r.card.in_deck]
    cards.sort(key=lambda c: (-c.total_cents, c.name.casefold()))
    return DeckPrice(deck, tuple(cards))


def card_price(resolved: ResolvedCard) -> CardPrice:
    """One deck card's price: its printing's for its finish, else the card's cheapest printing's."""
    card, printing = resolved.card, resolved.printing
    cheapest = resolved.info.cheapest_cents if resolved.info is not None else None
    unit: int | None = None
    if printing is not None:
        unit = printing.eur_cents if card.finish == NONFOIL_FINISH else printing.eur_foil_cents
    if unit is not None:
        return CardPrice(card.name, card.quantity, unit, PriceSource.PRINTING, cheapest)
    if cheapest is not None:
        return CardPrice(card.name, card.quantity, cheapest, PriceSource.CHEAPEST, cheapest)
    return CardPrice(card.name, card.quantity, None, PriceSource.NONE, None)


def format_eur(cents: int) -> str:
    return f"€{cents // 100:,}.{cents % 100:02d}"
