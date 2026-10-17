"""Output shared by several `cmdr` commands: plan previews and save outcomes, prices and bracket marks."""

from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from commander.apply import Change, ChangeKind, Resolution, SaveResult
from commander.bracketcheck import BracketCheck
from commander.bracketpreview import BracketPreview
from commander.pricing import format_eur
from commander.suggest import BracketRaise

_CHANGE_MARKS = {
    ChangeKind.ADD: "+",
    ChangeKind.MORE: "+",
    ChangeKind.FEWER: "-",
    ChangeKind.REMOVE: "-",
    ChangeKind.MOVE_IN: "+",
    ChangeKind.SET_ASIDE: "-",
    ChangeKind.DROP: "-",
}


def print_resolution(resolution: Resolution, out: TextIO) -> None:
    deck = resolution.deck
    bracket = f"bracket {deck.bracket}" if deck.bracket else "no bracket set"
    print(f"{deck.name} ({deck.id}), {bracket}: {deck.url}", file=out)
    if resolution.changes:
        print(f"  cards in deck: {resolution.main_before} -> {resolution.main_after}", file=out)
    for change in resolution.changes:
        print(f"  {change_text(change)}", file=out)
    for line in resolution.skipped:
        print(f"  skipped, {line}", file=out)
    for line in resolution.warnings:
        print(f"  warning: {line}", file=out)
    for line in resolution.problems:
        print(f"  problem: {line}", file=out)


def print_check(check: BracketCheck, out: TextIO) -> None:
    """The notes of a bracket check, then the cards that lift the deck above its bracket."""
    for note in check.notes:
        print(f"  ! {note}", file=out)
    if check.causes:
        print("  because of:", file=out)
        for cause in check.causes:
            print(f"    {cause.tag.label:<10} {cause.reason.value}: {' + '.join(cause.cards)}", file=out)


def print_bracket_preview(preview: BracketPreview | None, out: TextIO) -> None:
    if preview is None:
        return
    after = preview.after.data
    if after is None:
        print(f"  bracket after these changes: unknown ({preview.after.error})", file=out)
        return
    was = (
        f", was {preview.before.tag.brackets}"
        if preview.before and preview.before.tag is not after.tag
        else ""
    )
    print(f"  bracket after these changes: {after.tag.brackets} ({after.tag.label}){was}", file=out)
    print_check(preview.check, out)


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
    if change.kind in {ChangeKind.REMOVE, ChangeKind.DROP}:
        copies = f"{change.before} " if change.before > 1 else ""
        return f"{mark} {copies}{change.name}{board}"
    if change.kind is ChangeKind.MOVE_IN:
        copies = f"{change.after} " if change.after > 1 else ""
        moved = f"{mark} {copies}{change.name} into the deck from the maybeboard"
        return f"{moved}, in {change.category}{raise_text(change.raises)}"
    if change.kind is ChangeKind.SET_ASIDE:
        return f"{mark} {change.name} to the {change.category}"
    return f"{mark} {change.name}{board}: {change.before} -> {change.after} copies"


def print_apply_outcome(
    resolution: Resolution, result: SaveResult | None, folder: Path | None, out: TextIO, *, save: bool
) -> None:
    if resolution.problems:
        print("nothing was changed: fix the problems above, then run it again", file=out)
    elif not resolution.changes:
        print("nothing to change: the deck already matches the plan", file=out)
    elif not save:
        print("preview only: run it again with --save to make these changes on Archidekt", file=out)
    elif result is not None:
        print(
            f"saved {len(resolution.changes)} change(s); snapshot '{result.snapshot}' is on Archidekt",
            file=out,
        )
        for line in result.mismatches:
            print(f"  not as planned: {line}", file=out)
        if folder is not None:
            print(f"local copy updated: {folder}", file=out)
        else:
            print("the deck is not in your deck folder, so no local copy was written", file=out)


def price_text(cents: int | None) -> str:
    return "-" if cents is None else format_eur(cents)


def raise_text(raises: BracketRaise | None) -> str:
    return f"  ! bracket {raises.bracket}+ ({raises.reason})" if raises else ""


def raise_json(raises: BracketRaise | None) -> dict[str, object] | None:
    return {"bracket": raises.bracket, "reason": raises.reason} if raises else None


def progress_printer(err: TextIO) -> Callable[[str], None]:
    if not err.isatty():
        return lambda _: None
    return lambda message: print(message, file=err)
