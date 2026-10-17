"""Output shared by several `cmdr` commands: plan previews and save outcomes, prices and bracket marks."""

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO

from commander.apply import Resolution, SaveResult
from commander.bracketcheck import BracketCheck
from commander.bracketpreview import BracketPreview
from commander.carddb import Role
from commander.changetext import change_text
from commander.deckcache import Answer, AnswerSource
from commander.models import Deck
from commander.pricing import format_eur
from commander.spellbook import BracketTag
from commander.suggest import BracketRaise


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


def raise_json(raises: BracketRaise | None) -> dict[str, object] | None:
    return {"bracket": raises.bracket, "reason": raises.reason} if raises else None


def write_json(document: object, out: TextIO) -> None:
    """A command's `--json` answer: indented UTF-8 JSON and a newline."""
    json.dump(document, out, ensure_ascii=False, indent=2)
    out.write("\n")


def progress_printer(err: TextIO) -> Callable[[str], None]:
    if not err.isatty():
        return lambda _: None
    return lambda message: print(message, file=err)


ROLE_HEADINGS: Mapping[Role, str] = {
    Role.RAMP: "ramp",
    Role.DRAW: "draw",
    Role.REMOVAL: "removal",
    Role.BOARD_WIPE: "wipes",
    Role.TUTOR: "tutors",
    Role.PROTECTION: "protect",
    Role.RECURSION: "recursion",
    Role.COUNTERSPELL: "counters",
}


TAG_ORDER = {tag: index for index, tag in enumerate(BracketTag)}


def warn_answer[T](deck: Deck, answer: Answer[T], what: str, err: TextIO) -> None:
    if answer.source is AnswerSource.UNAVAILABLE:
        print(f"cmdr: {deck.name}: no {what}: {answer.error}", file=err)
    elif answer.source is AnswerSource.STALE and answer.fetched_at is not None:
        print(
            f"cmdr: {deck.name}: showing the {what} from {answer.fetched_at.date().isoformat()}; "
            f"refreshing it failed: {answer.error}",
            file=err,
        )


def count_text(value: int | None) -> str:
    return "-" if value is None else str(value)
