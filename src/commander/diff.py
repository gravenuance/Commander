"""What changed in a deck's main 100 between two snapshots."""

from collections import Counter
from dataclasses import dataclass

from commander.models import Deck


@dataclass(slots=True, frozen=True)
class CardChange:
    name: str
    old_quantity: int
    new_quantity: int


@dataclass(slots=True, frozen=True)
class DeckDiff:
    added: tuple[CardChange, ...]
    removed: tuple[CardChange, ...]
    changed: tuple[CardChange, ...]
    old_name: str | None

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed or self.old_name)


def diff_decks(old: Deck | None, new: Deck) -> DeckDiff:
    """Compare main-deck quantities by card name; a printing swap of the same card is not a change."""
    before = _quantities(old) if old is not None else Counter[str]()
    after = _quantities(new)
    added: list[CardChange] = []
    removed: list[CardChange] = []
    changed: list[CardChange] = []
    for name in sorted(before.keys() | after.keys(), key=str.casefold):
        was, now = before[name], after[name]
        if was == now:
            continue
        change = CardChange(name, was, now)
        if was == 0:
            added.append(change)
        elif now == 0:
            removed.append(change)
        else:
            changed.append(change)
    renamed = old is not None and old.name != new.name
    return DeckDiff(
        added=tuple(added),
        removed=tuple(removed),
        changed=tuple(changed),
        old_name=old.name if renamed and old is not None else None,
    )


def _quantities(deck: Deck) -> Counter[str]:
    counts = Counter[str]()
    for card in deck.main_cards:
        counts[card.name] += card.quantity
    return counts
