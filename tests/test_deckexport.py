"""The deck as TopDeck.gg decklist text."""

from commander.deckexport import topdeck_text
from commander.models import Deck, DeckCard


def card(name: str, quantity: int = 1, *categories: str, in_deck: bool = True) -> DeckCard:
    return DeckCard(name, quantity, categories or ("Ramp",), in_deck, "sid", 1, 1, "tst", "1", "Normal")


def test_commanders_come_first_then_the_rest_by_name_with_front_faces_only() -> None:
    deck = Deck(
        1,
        "test",
        "me",
        5,
        "2026-10-16",
        (
            card("Mountain", 6, "Land"),
            card("Sundering Eruption // Volcanic Fissure"),
            card("Magda, Brazen Outlaw", 1, "Commander"),
            card("Arcane Signet"),
            card("High-Speed Hoverbike", 1, "Maybeboard", in_deck=False),
        ),
    )
    assert topdeck_text(deck).splitlines() == [
        "~~Commanders~~",
        "1 Magda, Brazen Outlaw",
        "",
        "~~Mainboard~~",
        "1 Arcane Signet",
        "6 Mountain",
        "1 Sundering Eruption",
    ]
