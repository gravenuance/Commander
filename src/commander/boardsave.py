"""Preview a deck board's queued cards against Archidekt, and save them: apply, refresh the copy, log it."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from commander.answers import card_key
from commander.apply import ApplyOutcome, Resolution, SaveError, SaveResult, apply_to_live, fetch_own_deck
from commander.archidekt_edit import ArchidektEditor, LiveDeck
from commander.board import (
    Actor,
    Board,
    BoardCard,
    BoardError,
    BoardService,
    board_plan,
    import_aside,
    read_board,
)
from commander.carddb import CardDb
from commander.plan import Plan
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
    live = fetch_own_deck(editor, stored.deck.id, user=user)
    plan = _live_plan(live, read_board(stored.folder))
    return apply_to_live(editor, live, plan, db, save_changes=False, now=now)


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

    The plan is made from the deck as it is on Archidekt now, not from the local copy, which may be behind.
    A plan with problems saves nothing and comes back with them. A failed save is logged to the board's
    activity before the error goes on to the caller.
    """
    board = read_board(stored.folder)
    live = fetch_own_deck(editor, stored.deck.id, user=user)
    plan = _live_plan(live, board)
    try:
        outcome = apply_to_live(editor, live, plan, db, save_changes=True, now=now, on_progress=on_progress)
    except SaveError as error:
        service.note(actor, "save to Archidekt failed", str(error))
        raise
    result = outcome.result
    if result is None:
        return BoardSave(outcome.resolution, None, ())
    folder = workspace.save(result.deck)
    pick_up_aside(StoredDeck(folder, result.deck), db, now=now)
    failed = {
        card_key(c.name)
        for c in outcome.resolution.changes
        if any(m.startswith(f"{c.name}:") for m in result.mismatches)
    }
    # A swap is done only when both its add and its cut are.
    applied = [card for card in board.queued if not failed & _swap_keys(card)]
    service.mark_applied([card.id for card in applied], actor, result.snapshot)
    if result.mismatches:
        service.note(actor, "Archidekt shows differences", "; ".join(result.mismatches))
    return BoardSave(outcome.resolution, result, tuple(card.name for card in applied))


def _swap_keys(card: BoardCard) -> set[str]:
    return {card_key(name) for name in (card.name, card.choice) if name}


def _live_plan(live: LiveDeck, board: Board) -> Plan:
    plan = board_plan(live.deck, board)
    if plan is not None:
        return plan
    if board.queued:
        raise BoardError("the queued cards are already in the deck on Archidekt; take them off the board")
    raise BoardError("nothing is queued to add")


def pick_up_aside(stored: StoredDeck, db: CardDb, *, now: Callable[[], datetime]) -> list[BoardCard]:
    """Put the deck's Sideboard and Maybeboard cards on its board as considered; returns the new ones."""
    service = BoardService(stored.folder, stored.deck, db, now=now)
    return import_aside(service, stored.deck, read_board(stored.folder))
