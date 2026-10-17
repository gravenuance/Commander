"""Bring the local deck folders up to date with Archidekt."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from functools import partial

from commander.archidekt import ArchidektClient, ArchidektError
from commander.background import run_all
from commander.diff import DeckDiff, diff_decks
from commander.models import Deck, DeckSummary
from commander.net import NetworkError
from commander.store import Workspace, WorkspaceError, folder_name

log = logging.getLogger(__name__)

MAX_PARALLEL_FETCHES = 4


class SyncStatus(Enum):
    NEW = "new"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    MISSING_UPSTREAM = "missing"
    FAILED = "failed"


@dataclass(slots=True, frozen=True)
class SyncResult:
    deck_id: int
    name: str
    status: SyncStatus
    diff: DeckDiff | None = None
    error: str | None = None


@dataclass(slots=True, frozen=True)
class _Plan:
    summary: DeckSummary
    previous: Deck | None


def sync_decks(
    client: ArchidektClient,
    workspace: Workspace,
    user: str,
    *,
    force: bool = False,
    dry_run: bool = False,
    on_progress: Callable[[str], None] = lambda _: None,
) -> list[SyncResult]:
    """Fetch decks whose Archidekt edit time moved (or all, with force) and save them unless dry_run."""
    summaries = client.list_commander_decks(user)
    on_progress(f"found {len(summaries)} Commander decks for {user}")
    results: list[SyncResult] = []
    to_fetch: list[_Plan] = []
    for summary in summaries:
        try:
            previous = workspace.load(summary.id)
        except WorkspaceError as error:
            results.append(_failed(summary, str(error)))
            continue
        if not force and previous is not None and _is_current(workspace, previous, summary):
            results.append(SyncResult(summary.id, summary.name, SyncStatus.UNCHANGED))
        else:
            to_fetch.append(_Plan(summary, previous))

    # Daemon threads, so Ctrl-C ends the process without waiting for the fetches still in flight.
    futures = run_all(
        [partial(client.get_deck, plan.summary.id) for plan in to_fetch],
        limit=MAX_PARALLEL_FETCHES,
        name="deck-fetch",
    )
    try:
        for plan, future in zip(to_fetch, futures, strict=True):
            try:
                deck = future.result()
            except (NetworkError, ArchidektError) as error:
                results.append(_failed(plan.summary, str(error)))
                continue
            on_progress(f"fetched {deck.name}")
            if deck.owner.casefold() != user.casefold():
                results.append(_failed(plan.summary, f"belongs to {deck.owner}, not {user}; skipped"))
                continue
            results.append(_apply(workspace, plan.previous, deck, dry_run=dry_run))
    finally:
        for future in futures:
            future.cancel()

    upstream_ids = {summary.id for summary in summaries}
    results += [
        SyncResult(stored.deck.id, stored.deck.name, SyncStatus.MISSING_UPSTREAM)
        for stored in workspace.load_all()
        if stored.deck.id not in upstream_ids
    ]
    return results


def _is_current(workspace: Workspace, previous: Deck, summary: DeckSummary) -> bool:
    folder = workspace.folder_for(summary.id)
    return (
        previous.updated_at == summary.updated_at
        and previous.tags is not None
        and previous.labels_known
        # Archidekt keeps the edit time when only the deck tags change, so the list's tags are compared too.
        and (summary.tags is None or summary.tags == previous.tags)
        and folder is not None
        and folder.name == folder_name(previous)
    )


def _apply(workspace: Workspace, previous: Deck | None, deck: Deck, *, dry_run: bool) -> SyncResult:
    diff = diff_decks(previous, deck)
    status = SyncStatus.NEW if previous is None else SyncStatus.UPDATED
    if previous is not None and diff.is_empty:
        # Edited on Archidekt without touching the main deck (maybeboard, description, printings).
        status = SyncStatus.UNCHANGED
    if not dry_run:
        try:
            workspace.save(deck)
        except (OSError, WorkspaceError) as error:
            return SyncResult(deck.id, deck.name, SyncStatus.FAILED, error=f"could not save: {error}")
    return SyncResult(deck.id, deck.name, status, diff=diff)


def _failed(summary: DeckSummary, message: str) -> SyncResult:
    log.warning("deck %s failed: %s", summary.id, message)
    return SyncResult(summary.id, summary.name, SyncStatus.FAILED, error=message)
