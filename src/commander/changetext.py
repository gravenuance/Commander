"""How a plan change and a bracket raise read as text: shared by the `cmdr` output and the desktop app."""

from commander.apply import Change, ChangeKind
from commander.pricing import format_eur
from commander.suggest import BracketRaise

_CHANGE_MARKS = {
    ChangeKind.ADD: "+",
    ChangeKind.MORE: "+",
    ChangeKind.FEWER: "-",
    ChangeKind.REMOVE: "-",
}


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
    if change.kind is ChangeKind.REMOVE:
        copies = f"{change.before} " if change.before > 1 else ""
        return f"{mark} {copies}{change.name}{board}"
    return f"{mark} {change.name}{board}: {change.before} -> {change.after} copies"


def raise_text(raises: BracketRaise | None) -> str:
    return f"  ! bracket {raises.bracket}+ ({raises.reason})" if raises else ""
