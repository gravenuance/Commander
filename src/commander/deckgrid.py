"""The deck as the app's card grid shows it: commanders, then a group per card type, as Archidekt does."""

from collections.abc import Sequence
from dataclasses import dataclass

from commander.apply import default_category
from commander.carddb import ResolvedCard
from commander.models import COMMANDER_CATEGORY
from commander.pricing import card_price

IMAGE_URL = "https://cards.scryfall.io/normal/front/{a}/{b}/{id}.jpg"
GROUP_ORDER = (
    COMMANDER_CATEGORY,
    "Creature",
    "Planeswalker",
    "Battle",
    "Instant",
    "Sorcery",
    "Artifact",
    "Enchantment",
    "Land",
    "Uncategorized",
)


@dataclass(slots=True, frozen=True)
class GridCard:
    name: str
    quantity: int
    image: str
    # The deck's own category for the card, which Archidekt files it under.
    category: str
    label: str
    label_colour: str
    cents: int | None
    cmc: float


@dataclass(slots=True, frozen=True)
class GridGroup:
    name: str
    cards: tuple[GridCard, ...]

    @property
    def count(self) -> int:
        return sum(card.quantity for card in self.cards)

    @property
    def cents(self) -> int:
        return sum(card.cents or 0 for card in self.cards)


def image_url(scryfall_id: str) -> str:
    return IMAGE_URL.format(a=scryfall_id[0], b=scryfall_id[1], id=scryfall_id)


def deck_grid(main: Sequence[ResolvedCard]) -> tuple[GridGroup, ...]:
    """The deck's cards by group in GROUP_ORDER, each by mana value then name; empty groups left out.

    A commander goes under Commander; any other card under its first card type in Archidekt's order, so a
    creature land is a Land and an artifact creature a Creature. Cards the card data lacks are Uncategorized.
    """
    groups: dict[str, list[GridCard]] = {name: [] for name in GROUP_ORDER}
    for r in main:
        if COMMANDER_CATEGORY in r.card.categories:
            group = COMMANDER_CATEGORY
        else:
            group = default_category(r.info) if r.info is not None else "Uncategorized"
        price = card_price(r)
        groups.setdefault(group, []).append(
            GridCard(
                name=r.card.name,
                quantity=r.card.quantity,
                image=image_url(r.card.scryfall_id),
                category=r.card.primary_category,
                label=r.card.label or "",
                label_colour=r.card.label_colour,
                cents=price.total_cents if price.unit_cents is not None else None,
                cmc=r.info.cmc if r.info is not None else 0.0,
            )
        )
    return tuple(
        GridGroup(name, tuple(sorted(cards, key=lambda c: (c.cmc, c.name.casefold()))))
        for name, cards in groups.items()
        if cards
    )
