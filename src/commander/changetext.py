"""How a plan change and a bracket raise read as text: shared by the `cmdr` output and the desktop app."""

from collections.abc import Sequence
from dataclasses import dataclass

from commander.apply import Change, ChangeKind
from commander.pricing import format_eur
from commander.suggest import BracketRaise

_CHANGE_MARKS = {
    ChangeKind.ADD: "+",
    ChangeKind.MORE: "+",
    ChangeKind.FEWER: "-",
    ChangeKind.REMOVE: "-",
    ChangeKind.CLEAR: "-",
}


@dataclass(slots=True, frozen=True)
class ClearGroup:
    """The cards of one board (Maybeboard or Sideboard) a save removes, as the preview words it."""

    board: str
    # Each card's name, with its copies in front when there are several.
    cards: tuple[str, ...]
    count: int

    @property
    def amount(self) -> str:
        return f"{self.count} card{'' if self.count == 1 else 's'}"

    @property
    def text(self) -> str:
        return f"Clears the {self.board}: {self.amount}"


def clear_groups(clears: Sequence[Change]) -> list[ClearGroup]:
    """The cleared entries grouped by board, in the order the boards first appear."""
    boards: dict[str, tuple[str, list[str], int]] = {}
    for change in clears:
        key = change.category.casefold()
        name, cards, count = boards.get(key, (change.category, [], 0))
        cards.append(f"{change.before} {change.name}" if change.before > 1 else change.name)
        boards[key] = (name, cards, count + change.before)
    return [ClearGroup(name, tuple(cards), count) for name, cards, count in boards.values()]


def change_mark(change: Change) -> str:
    """+ for a card coming into the deck or gaining copies, - for one leaving or losing them."""
    return _CHANGE_MARKS[change.kind]


def change_text(change: Change) -> str:
    mark = change_mark(change)
    board = "" if change.in_deck else f" [{change.category}]"
    if change.kind is ChangeKind.ADD:
        price = "no price" if change.price_cents is None else format_eur(change.price_cents)
        printing = f"{change.set_code.upper()} {change.collector_number}, {price}"
        copies = f"{change.after} " if change.after > 1 else ""
        return f"{mark} {copies}{change.name} ({printing}) in {change.category}{raise_text(change.raises)}"
    if change.kind in {ChangeKind.REMOVE, ChangeKind.CLEAR}:
        copies = f"{change.before} " if change.before > 1 else ""
        return f"{mark} {copies}{change.name}{board}"
    return f"{mark} {change.name}{board}: {change.before} -> {change.after} copies"


def raise_text(raises: BracketRaise | None) -> str:
    return f"  ! bracket {raises.bracket}+ ({raises.reason})" if raises else ""
