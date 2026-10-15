"""Deck data shared by every command."""

from dataclasses import dataclass

COMMANDER_CATEGORY = "Commander"
ARCHIDEKT_DECK_URL = "https://archidekt.com/decks/{id}"


@dataclass(slots=True, frozen=True)
class DeckCard:
    name: str
    quantity: int
    categories: tuple[str, ...]
    # Archidekt counts a card in the deck when its first category is one marked "included in deck".
    in_deck: bool
    scryfall_id: str
    archidekt_card_id: int
    entry_id: int
    set_code: str
    collector_number: str
    finish: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("card name is empty")
        if self.quantity < 1:
            raise ValueError(f"{self.name}: quantity must be at least 1, got {self.quantity}")

    @property
    def primary_category(self) -> str:
        return self.categories[0] if self.categories else "Uncategorized"


@dataclass(slots=True, frozen=True)
class Deck:
    id: int
    name: str
    owner: str
    bracket: int | None
    updated_at: str
    cards: tuple[DeckCard, ...]

    def __post_init__(self) -> None:
        if self.id < 1:
            raise ValueError(f"deck id must be positive, got {self.id}")
        if self.bracket is not None and not 1 <= self.bracket <= 5:  # noqa: PLR2004 — brackets run 1..5
            raise ValueError(f"bracket must be 1-5, got {self.bracket}")

    @property
    def url(self) -> str:
        return ARCHIDEKT_DECK_URL.format(id=self.id)

    @property
    def main_cards(self) -> tuple[DeckCard, ...]:
        return tuple(card for card in self.cards if card.in_deck)

    @property
    def main_count(self) -> int:
        return sum(card.quantity for card in self.main_cards)

    @property
    def commanders(self) -> tuple[str, ...]:
        return tuple(card.name for card in self.main_cards if COMMANDER_CATEGORY in card.categories)


@dataclass(slots=True, frozen=True)
class DeckSummary:
    id: int
    name: str
    updated_at: str
