"""Decks as text other sites import: TopDeck.gg's tournament decklist and Cardmarket's wants list."""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from commander.carddb import front_name
from commander.models import COMMANDER_CATEGORY, DONT_HAVE_LABEL, Deck


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
    return f"{quantity} {front_name(name)}"


@dataclass(slots=True, frozen=True)
class WantsList:
    """Cardmarket wants-list text, the copies in it, and the decks left out for want of known labels."""

    text: str
    cards: int
    skipped: tuple[str, ...]


def cardmarket_wants_text(decks: Iterable[Deck]) -> WantsList:
    """Every main-deck card labelled Don't Have, summed over the decks, as "<quantity> <name>" lines.

    Cardmarket lists a two-faced card under both faces, so the name stays whole ("Front // Back"), unlike
    TopDeck's. Decks that never kept their labels are skipped and named, by name order.
    """
    wanted: Counter[str] = Counter()
    skipped: list[str] = []
    for deck in decks:
        if not deck.labels_known:
            skipped.append(deck.short_name)
            continue
        for card in deck.main_cards:
            if card.label == DONT_HAVE_LABEL:
                wanted[card.name] += card.quantity
    lines = [f"{quantity} {name}" for name, quantity in sorted(wanted.items(), key=_by_name)]
    text = "".join(f"{line}\n" for line in lines)
    return WantsList(text, wanted.total(), tuple(sorted(skipped, key=str.casefold)))


def _by_name(item: tuple[str, int]) -> tuple[str, str]:
    return item[0].casefold(), item[0]
