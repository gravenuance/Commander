"""Preview a deck board's queued cards against Archidekt, and save them: apply, refresh the copy, log it."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from commander.answers import card_key
from commander.apply import (
    ApplyOutcome,
    Resolution,
    SaveError,
    SaveResult,
    apply_to_live,
    change_label,
    fetch_own_deck,
)
from commander.archidekt_edit import ArchidektEditor, LiveDeck
from commander.board import (
    Actor,
    Board,
    BoardCard,
    BoardService,
    NothingQueuedError,
    already_added,
    board_plan,
    change_key,
    counts_met,
    read_board,
)
from commander.carddb import CardDb
from commander.plan import Plan
from commander.store import StoredDeck, Workspace

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class BoardSave:
    resolution: Resolution
    result: SaveResult | None
    # Board cards now in the deck on Archidekt.
    applied: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class BoardPreview:
    resolution: Resolution
    # The queued cards and count changes the deck on Archidekt already holds; a save marks them done.
    done: tuple[str, ...]

    @property
    def nothing_to_send(self) -> str | None:
        """Why a save would send nothing to Archidekt; None when it sends changes or problems say why not."""
        resolution = self.resolution
        if resolution.changes or resolution.problems:
            return None
        if self.done:
            return f"Nothing to send to Archidekt: the deck there already has {', '.join(self.done)}."
        if resolution.skipped:
            return f"Nothing to send to Archidekt: {'; '.join(resolution.skipped)}"
        return "Nothing to send to Archidekt: no changes are queued for this deck."


def preview_board(
    editor: ArchidektEditor,
    stored: StoredDeck,
    db: CardDb,
    *,
    user: str,
    now: Callable[[], datetime],
    estimated: int | None = None,
) -> BoardPreview:
    """The changes the queued cards would make to the deck as it is on Archidekt now; estimated is the lowest
    bracket of the deck's estimate, which an unset deck's bracket raises go by."""
    live = fetch_own_deck(editor, stored.deck.id, user=user)
    board = read_board(stored.folder)
    plan = live_plan(live, board)
    if plan is None:
        resolution = _nothing_to_change(live, board)
    else:
        outcome = apply_to_live(editor, live, plan, db, save_changes=False, now=now, estimated=estimated)
        resolution = _noting_done(outcome, live, board).resolution
    _log_dropped(stored.deck.id, resolution, "preview")
    return BoardPreview(resolution, _done_names(live, board))


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
    estimated: int | None = None,
) -> BoardSave:
    """Save the queued cards and count changes to Archidekt, then update the local deck copy and mark them
    applied (a done count change leaves the board). estimated is as for `preview_board`.

    The plan is made from the deck as it is on Archidekt now, not from the local copy, which may be behind.
    Queued cards that deck already holds, and count changes it already meets, are marked applied too, their
    cuts left alone; when they are all that is queued, nothing is written to Archidekt. A plan with problems
    saves nothing and comes back with them. A failed save is logged to the board's activity before the error
    goes on to the caller.
    """
    board = read_board(stored.folder)
    live = fetch_own_deck(editor, stored.deck.id, user=user)
    plan = live_plan(live, board)
    if plan is None:
        resolution = _nothing_to_change(live, board)
        _log_dropped(stored.deck.id, resolution, "save")
        workspace.save(live.deck)
        service.mark_applied([card.id for card in board.queued], actor, None, counts=board.counts)
        names = (*(card.name for card in board.queued), *(change.name for change in board.counts))
        return BoardSave(resolution, None, names)
    try:
        outcome = apply_to_live(
            editor, live, plan, db, save_changes=True, now=now, on_progress=on_progress, estimated=estimated
        )
    except SaveError as error:
        service.note(actor, "save to Archidekt failed", str(error))
        raise
    outcome = _noting_done(outcome, live, board)
    result = outcome.result
    if result is None:
        return BoardSave(outcome.resolution, None, ())
    workspace.save(result.deck)
    failed = [
        c
        for c in outcome.resolution.changes
        if any(m.startswith(f"{change_label(c)}:") for m in result.mismatches)
    ]
    failed_in_deck = {card_key(c.name) for c in failed if c.in_deck}
    failed_keys = {change_key(c.name, None if c.in_deck else c.category) for c in failed}
    # A swap is done only when both its add and its cut are; a card already in the deck needs nothing more.
    applied = [card for card in board.queued if not failed_in_deck & _swap_keys(card)]
    counts = [change for change in board.counts if change.key not in failed_keys]
    service.mark_applied([card.id for card in applied], actor, result.snapshot, counts=counts)
    if result.mismatches:
        service.note(actor, "Archidekt shows differences", "; ".join(result.mismatches))
    return BoardSave(
        outcome.resolution, result, (*(card.name for card in applied), *(change.name for change in counts))
    )


def _log_dropped(deck_id: int, resolution: Resolution, doing: str) -> None:
    """Log the queued changes the deck on Archidekt leaves nothing to do for; a warning when that is all."""
    if not resolution.skipped:
        return
    left_out = "; ".join(resolution.skipped)
    if resolution.changes or resolution.problems:
        log.info("deck %d: the %s leaves out: %s", deck_id, doing, left_out)
        return
    log.warning("deck %d: the %s sends nothing to Archidekt: %s", deck_id, doing, left_out)


def _done_names(live: LiveDeck, board: Board) -> tuple[str, ...]:
    added = tuple(card.name for card in already_added(live.deck, board))
    return added + tuple(change.name for change in counts_met(live.deck, board))


def _done_lines(live: LiveDeck, board: Board) -> tuple[str, ...]:
    added = tuple(
        f"{card.name}: already in the deck on Archidekt"
        f"{f', so its cut {card.choice} stays' if card.choice else ''}; saving marks it applied"
        for card in already_added(live.deck, board)
    )
    return added + tuple(
        f"{change.name}: no longer on {change.category} on Archidekt; saving clears the change"
        if change.category
        else f"{change.name}: the deck on Archidekt already meets the change to {change.count}; "
        "saving clears the change"
        for change in counts_met(live.deck, board)
    )


def _noting_done(outcome: ApplyOutcome, live: LiveDeck, board: Board) -> ApplyOutcome:
    done = _done_lines(live, board)
    return replace(
        outcome, resolution=replace(outcome.resolution, skipped=(*done, *outcome.resolution.skipped))
    )


def _nothing_to_change(live: LiveDeck, board: Board) -> Resolution:
    return Resolution(live.deck, (), (), (), _done_lines(live, board))


def _swap_keys(card: BoardCard) -> set[str]:
    return {card_key(name) for name in (card.name, card.choice) if name}


def live_plan(live: LiveDeck, board: Board) -> Plan | None:
    """The plan from the live deck; None when the deck already holds every queued change."""
    plan = board_plan(live.deck, board)
    if plan is None and not board.pending:
        raise NothingQueuedError("nothing is queued to change")
    return plan
