"""A deck as text other sites import: TopDeck.gg's tournament decklist."""

from commander.models import COMMANDER_CATEGORY, Deck


def topdeck_text(deck: Deck) -> str:
    """The deck as TopDeck.gg writes decklists: its commanders, then the rest, one "1 Name" a line.

    TopDeck names a two-faced card by its front face, so "Sundering Eruption // Volcanic Fissure" is written
    "Sundering Eruption". The deck is the saved one: changes still queued on the board are not in it.
    """
    commanders = [card for card in deck.main_cards if COMMANDER_CATEGORY in card.categories]
    rest = sorted(
        (card for card in deck.main_cards if card not in commanders), key=lambda c: c.name.casefold()
    )
    lines = ["~~Commanders~~", *(_line(card.quantity, card.name) for card in commanders), "", "~~Mainboard~~"]
    lines += [_line(card.quantity, card.name) for card in rest]
    return "\n".join(lines) + "\n"


def _line(quantity: int, name: str) -> str:
    return f"{quantity} {name.split(' // ', maxsplit=1)[0]}"
