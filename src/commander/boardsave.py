"""Preview a deck board's queued cards against Archidekt, and save them: apply, refresh the copy, log it."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from commander.apply import ApplyOutcome, Resolution, SaveError, SaveResult, apply_plan
from commander.archidekt_edit import ArchidektEditor
from commander.board import Actor, BoardError, BoardService, board_plan, read_board
from commander.carddb import CardDb
from commander.store import StoredDeck, Workspace


@dataclass(slots=True, frozen=True)
class BoardSave:
    resolution: Resolution
    result: SaveResult | None
    # Board cards now in the deck on Archidekt.
    applied: tuple[str, ...]


def preview_board(
    editor: ArchidektEditor, stored: StoredDeck, db: CardDb, *, user: str, now: Callable[[], datetime]
) -> ApplyOutcome:
    """The changes the queued cards would make to the deck as it is on Archidekt now."""
    plan = board_plan(stored.deck, read_board(stored.folder))
    if plan is None:
        raise BoardError("nothing is queued to add")
    return apply_plan(editor, stored.deck.id, plan, db, user=user, save_changes=False, now=now)


def save_board(
    editor: ArchidektEditor,
    workspace: Workspace,
    stored: StoredDeck,
    db: CardDb,
    service: BoardService,
    *,
    user: str,
    actor: Actor,
    now: Callable[[], datetime],
    on_progress: Callable[[str], None] = lambda _: None,
) -> BoardSave:
    """Save the queued cards to Archidekt, then update the local deck copy and mark them applied.

    A plan with problems saves nothing and comes back with them. A failed save is logged to the board's
    activity before the error goes on to the caller.
    """
    board = read_board(stored.folder)
    plan = board_plan(stored.deck, board)
    if plan is None:
        raise BoardError("nothing is queued to add")
    try:
        outcome = apply_plan(
            editor, stored.deck.id, plan, db, user=user, save_changes=True, now=now, on_progress=on_progress
        )
    except SaveError as error:
        service.note(actor, "save to Archidekt failed", str(error))
        raise
    result = outcome.result
    if result is None:
        return BoardSave(outcome.resolution, None, ())
    workspace.save(result.deck)
    failed = {
        c.name
        for c in outcome.resolution.changes
        if any(m.startswith(f"{c.name}:") for m in result.mismatches)
    }
    applied = [card for card in board.queued if card.name not in failed]
    service.mark_applied([card.id for card in applied], actor, result.snapshot)
    if result.mismatches:
        service.note(actor, "Archidekt shows differences", "; ".join(result.mismatches))
    return BoardSave(outcome.resolution, result, tuple(card.name for card in applied))
