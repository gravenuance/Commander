"""The deck as the app's card grid shows it: commanders, then a group per card type, as Archidekt does, then
each category of cards outside the deck (Sideboard, Maybeboard) as a group of its own."""

from collections.abc import Sequence
from dataclasses import dataclass

from commander.apply import default_category
from commander.carddb import CardInfo, ResolvedCard
from commander.models import COMMANDER_CATEGORY, DONT_HAVE_LABEL
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
# Archidekt's own categories outside the deck come first among those groups; the user's own follow by name.
ASIDE_ORDER = ("Sideboard", "Maybeboard")


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
    # For a card queued on the board to come into the deck: that board card's id; None for a deck card.
    board_id: str | None = None


@dataclass(slots=True, frozen=True)
class GridGroup:
    name: str
    cards: tuple[GridCard, ...]
    # False for a category of cards outside the deck: they count in no total of the deck.
    in_deck: bool = True

    @property
    def count(self) -> int:
        return sum(card.quantity for card in self.cards)

    @property
    def cents(self) -> int:
        return sum(card.cents or 0 for card in self.cards)


@dataclass(slots=True, frozen=True)
class PendingAdd:
    """A card queued on the board to come into the deck, as the grid shows it until it is saved."""

    board_id: str
    info: CardInfo
    copies: int
    # The category it is saved under; None files it by its card type.
    category: str | None
    image: str


def image_url(scryfall_id: str) -> str:
    return IMAGE_URL.format(a=scryfall_id[0], b=scryfall_id[1], id=scryfall_id)


def deck_grid(main: Sequence[ResolvedCard], adds: Sequence[PendingAdd] = ()) -> tuple[GridGroup, ...]:
    """The deck's cards by group in GROUP_ORDER, each by mana value then name; empty groups left out.

    A commander goes under Commander; any other card under its first card type in Archidekt's order, so a
    creature land is a Land and an artifact creature a Creature. Cards the card data lacks are Uncategorized.
    Cards queued to come in join the group of their card type, labelled Don't Have as a save labels them.
    """
    groups: dict[str, list[GridCard]] = {name: [] for name in GROUP_ORDER}
    for r in main:
        if COMMANDER_CATEGORY in r.card.categories:
            group = COMMANDER_CATEGORY
        else:
            group = default_category(r.info) if r.info is not None else "Uncategorized"
        groups.setdefault(group, []).append(_grid_card(r))
    for add in adds:
        groups.setdefault(default_category(add.info), []).append(
            GridCard(
                name=add.info.name,
                quantity=add.copies,
                image=add.image,
                category=add.category or default_category(add.info),
                label=DONT_HAVE_LABEL,
                label_colour="",
                cents=add.info.cheapest_cents * add.copies if add.info.cheapest_cents is not None else None,
                cmc=add.info.cmc,
                board_id=add.board_id,
            )
        )
    return tuple(GridGroup(name, _sorted(cards)) for name, cards in groups.items() if cards)


def aside_grid(outside: Sequence[ResolvedCard]) -> tuple[GridGroup, ...]:
    """The cards outside the deck, one group per category they are filed under: Sideboard, Maybeboard, then
    any other category the deck does not count, by name."""
    groups: dict[str, list[GridCard]] = {}
    for r in outside:
        groups.setdefault(r.card.primary_category, []).append(_grid_card(r))
    order = sorted(groups, key=lambda name: (_aside_rank(name), name.casefold()))
    return tuple(GridGroup(name, _sorted(groups[name]), in_deck=False) for name in order)


def _aside_rank(name: str) -> int:
    folded = [known.casefold() for known in ASIDE_ORDER]
    return folded.index(name.casefold()) if name.casefold() in folded else len(folded)


def _grid_card(r: ResolvedCard) -> GridCard:
    price = card_price(r)
    return GridCard(
        name=r.card.name,
        quantity=r.card.quantity,
        image=image_url(r.card.scryfall_id),
        category=r.card.primary_category,
        label=r.card.label or "",
        label_colour=r.card.label_colour,
        cents=price.total_cents if price.unit_cents is not None else None,
        cmc=r.info.cmc if r.info is not None else 0.0,
    )


def _sorted(cards: list[GridCard]) -> tuple[GridCard, ...]:
    return tuple(sorted(cards, key=lambda c: (c.cmc, c.name.casefold())))
