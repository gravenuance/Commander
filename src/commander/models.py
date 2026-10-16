"""Deck data shared by every command."""

from dataclasses import dataclass

COMMANDER_CATEGORY = "Commander"
MAYBEBOARD_CATEGORY = "Maybeboard"
# Casefolded names of the categories that hold cards under consideration, outside the deck.
ASIDE_CATEGORIES = frozenset({"sideboard", "maybeboard"})
# Words in an Archidekt category that file its cards as part of the deck's plan: "Combo Pieces", "WinCons".
PLAN_WORDS = ("combo", "wincon", "finisher", "payoff", "keycard", "keypiece")
ARCHIDEKT_DECK_URL = "https://archidekt.com/decks/{id}"
# The card labels of the buying steps (ADR 0010): a card saved into a deck is still to buy, then ordered.
DONT_HAVE_LABEL = "Don't Have"
GETTING_LABEL = "Getting"
TO_GET_LABELS = (DONT_HAVE_LABEL, GETTING_LABEL)


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
    # The card's Archidekt label name ("Proxy", "Getting"), "" for none; None when not kept (old snapshot).
    label: str | None = ""
    # The label's colour as Archidekt keeps it ("#f445f4"); "" for no label or an unknown colour.
    label_colour: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("card name is empty")
        if self.quantity < 1:
            raise ValueError(f"{self.name}: quantity must be at least 1, got {self.quantity}")

    @property
    def primary_category(self) -> str:
        return self.categories[0] if self.categories else "Uncategorized"

    @property
    def aside(self) -> bool:
        """On the Sideboard or Maybeboard: kept with the deck, under consideration, not played."""
        return not self.in_deck and self.primary_category.casefold() in ASIDE_CATEGORIES


@dataclass(slots=True, frozen=True)
class Deck:
    id: int
    name: str
    owner: str
    bracket: int | None
    updated_at: str
    cards: tuple[DeckCard, ...]
    # Archidekt's deck tags (Stax, Dwarves, cEDH...); None for a snapshot saved before they were kept.
    tags: tuple[str, ...] | None = ()

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
    def aside_cards(self) -> tuple[DeckCard, ...]:
        return tuple(card for card in self.cards if card.aside)

    @property
    def labels_known(self) -> bool:
        return all(card.label is not None for card in self.cards)

    @property
    def to_get(self) -> tuple[DeckCard, ...]:
        """Deck cards still to be bought or on their way, by their label."""
        return tuple(card for card in self.main_cards if card.label in TO_GET_LABELS)

    @property
    def main_count(self) -> int:
        return sum(card.quantity for card in self.main_cards)

    @property
    def commanders(self) -> tuple[str, ...]:
        return tuple(card.name for card in self.main_cards if COMMANDER_CATEGORY in card.categories)

    @property
    def short_name(self) -> str:
        """The deck by its commander's short name, as players say it: "Magda", "Ellivere", "Etali"."""
        if not self.commanders:
            return self.name
        return " & ".join(_short_commander(name) for name in self.commanders)


def short_commander_name(name: str) -> str:
    """A commander as players say it; partners filed as "A / B" become "A/B", each by its short name."""
    return "/".join(_short_commander(part) for part in name.split(" / "))


def _short_commander(name: str) -> str:
    # The first word: "Magda, Brazen Outlaw", "Tevesh Szat, Doom of Fools", "Ellivere of the Wild Court";
    # after a leading "The": "The Gitrog Monster" -> Gitrog.
    front = name.split(" // ", maxsplit=1)[0].removeprefix("The ")
    return front.split(",")[0].split()[0]


@dataclass(slots=True, frozen=True)
class DeckSummary:
    id: int
    name: str
    updated_at: str
    # The deck tags the list shows; Archidekt keeps updated_at when only these change. None when not sent.
    tags: tuple[str, ...] | None = None


def plan_category(name: str) -> bool:
    """Whether an Archidekt category files its cards as the deck's plan, like "Combo Pieces" or "Win Con"."""
    squeezed = "".join(ch for ch in name.casefold() if ch.isalnum())
    return any(word in squeezed for word in PLAN_WORDS)
