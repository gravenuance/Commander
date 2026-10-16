"""The deck grid: commanders first, then a group per card type, each by mana value."""

from commander.carddb import CardInfo, ResolvedCard
from commander.deckgrid import deck_grid
from commander.models import DeckCard

SID = "0a1b2c3d-0000-0000-0000-000000000000"


def card(
    name: str,
    type_line: str,
    cmc: float = 1.0,
    *,
    categories: tuple[str, ...] = ("Main",),
    cents: int | None = 100,
    quantity: int = 1,
    label: str = "",
    known: bool = True,
) -> ResolvedCard:
    colour = "#f47373" if label else ""
    deck_card = DeckCard(name, quantity, categories, True, SID, 1, 1, "tst", "1", "Normal", label, colour)  # noqa: FBT003
    info = CardInfo(name.lower(), name, "", cmc, type_line, "G", False, True, cents, 1, frozenset())  # noqa: FBT003
    return ResolvedCard(deck_card, info if known else None, None)


def test_commanders_come_first_then_card_types_in_archidekts_order() -> None:
    grid = deck_grid(
        [
            card("Forest", "Basic Land — Forest", 0, quantity=12, cents=10),
            card("Dryad Arbor", "Land Creature — Forest Dryad", 0),
            card("Sol Ring", "Artifact", 1),
            card("Walking Ballista", "Artifact Creature — Construct", 0),
            card(
                "Ellivere of the Wild Court",
                "Legendary Creature — Human Knight",
                4,
                categories=("Commander",),
            ),
            card("Birds of Paradise", "Creature — Bird", 1, label="Don't Have"),
        ]
    )
    assert [(g.name, g.count, g.cents) for g in grid] == [
        ("Commander", 1, 100),
        ("Creature", 2, 200),
        ("Artifact", 1, 100),
        ("Land", 13, 220),
    ]
    creatures = grid[1].cards
    assert [c.name for c in creatures] == ["Walking Ballista", "Birds of Paradise"]
    assert (creatures[1].label, creatures[1].label_colour, creatures[1].category) == (
        "Don't Have",
        "#f47373",
        "Main",
    )
    assert creatures[0].image == f"https://cards.scryfall.io/normal/front/0/a/{SID}.jpg"


def test_unknown_cards_are_uncategorized_and_unpriced_cards_count_nothing() -> None:
    grid = deck_grid([card("Mystery", "", known=False), card("Promo", "Instant", cents=None)])
    assert [(g.name, g.count, g.cents) for g in grid] == [("Instant", 1, 0), ("Uncategorized", 1, 0)]
    assert grid[0].cards[0].cents is None
