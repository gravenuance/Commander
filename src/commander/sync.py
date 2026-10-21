"""Bring the local deck folders up to date with Archidekt."""

import logging
from collections.abc import Callable, Collection
from concurrent.futures import Future, as_completed
from dataclasses import dataclass, replace
from enum import Enum
from functools import partial

from commander.archidekt import ArchidektClient, ArchidektError, AuthError, SignInRequiredError
from commander.background import run_all
from commander.decklocks import DeckLock, no_deck_lock
from commander.diff import DeckDiff, diff_decks
from commander.fetchpriority import FetchPriority
from commander.models import Deck, DeckSummary
from commander.net import NetworkError
from commander.store import StoredDeck, Workspace, WorkspaceError, folder_name

log = logging.getLogger(__name__)

MAX_PARALLEL_FETCHES = 4
# Said whenever a sync could not see the decks that only the owner's login reaches (ADR 0040).
PRIVATE_NOT_UPDATED = "private and unlisted decks were not updated"


class SyncStatus(Enum):
    NEW = "new"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    MISSING_UPSTREAM = "missing"
    REMOVED = "removed"
    WOULD_REMOVE = "would remove"
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


def _ignore_ids(_deck_ids: Collection[int]) -> None:
    return


def _ignore_id(_deck_id: int) -> None:
    return


@dataclass(slots=True, frozen=True)
class PullWatch:
    """How a caller that lets others write meanwhile follows and steers the pull (ADR 0042).

    `on_listed` gets the decks the pull will still write, once it knows them; `on_done` gets each of those
    as the pull is done with it (written, or failed). Each is written under `deck_lock`, and the next deck
    fetched is the one `priority` names if it still waits. `on_removed` gets each deck whose folder was
    deleted because Archidekt deleted the deck (ADR 0045), after the removal and outside the lock.
    """

    priority: FetchPriority | None = None
    deck_lock: DeckLock = no_deck_lock
    on_listed: Callable[[Collection[int]], None] = _ignore_ids
    on_done: Callable[[int], None] = _ignore_id
    on_removed: Callable[[int], None] = _ignore_id


def sync_decks(
    client: ArchidektClient,
    workspace: Workspace,
    user: str,
    *,
    force: bool = False,
    dry_run: bool = False,
    on_progress: Callable[[str], None] = lambda _: None,
    warn: Callable[[str], None] = lambda _: None,
    watch: PullWatch = PullWatch(),  # noqa: B008 — frozen, so one shared default is safe
) -> list[SyncResult]:
    """Fetch decks whose Archidekt edit time moved (or all, with force) and save them unless dry_run.

    A signed-in client whose listing Archidekt refuses carries on without the login and says so to warn.
    Decks on disk that the listing lacks come back as MISSING_UPSTREAM, except private and unlisted ones when
    the listing was anonymous, which could not show them (ADR 0040). A signed-in sync removes the folder of
    such a deck whose own page answers 404 (REMOVED, or WOULD_REMOVE on a dry run) and keeps the others,
    reported as missing (ADR 0045). The results are in listing order; each fetched deck is written as soon
    as it arrives.
    """
    try:
        summaries = client.list_commander_decks(user)
    except SignInRequiredError as error:
        log.warning("%s; listing %s's public decks only", error, user)
        warn(f"Archidekt refused the login: {PRIVATE_NOT_UPDATED}")
        client = client.anonymous()
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

    watch.on_listed([plan.summary.id for plan in to_fetch])

    priority = watch.priority or FetchPriority()
    # Daemon threads, so Ctrl-C ends the process without waiting for the fetches still in flight.
    futures = run_all(
        [partial(client.get_deck, plan.summary.id) for plan in to_fetch],
        limit=MAX_PARALLEL_FETCHES,
        name="deck-fetch",
        pick=lambda waiting: priority.pick(waiting, lambda index: to_fetch[index].summary.id),
    )
    plans = dict(zip(futures, to_fetch, strict=True))
    fetched: dict[int, SyncResult] = {}
    try:
        for future in as_completed(futures):
            plan = plans[future]
            try:
                fetched[plan.summary.id] = _write(future, plan, workspace, user, dry_run, on_progress, watch)
            finally:
                watch.on_done(plan.summary.id)
    finally:
        for future in futures:
            future.cancel()
    results += [fetched[plan.summary.id] for plan in to_fetch]

    upstream_ids = {summary.id for summary in summaries}
    results += [
        _settle_missing(client, workspace, user, stored, dry_run, watch)
        for stored in workspace.load_all()
        if stored.deck.id not in upstream_ids and (client.signed_in or not stored.deck.needs_login)
    ]
    return results


def _settle_missing(  # noqa: PLR0917 — the one step of sync_decks' tail
    client: ArchidektClient,
    workspace: Workspace,
    user: str,
    stored: StoredDeck,
    dry_run: bool,  # noqa: FBT001
    watch: PullWatch,
) -> SyncResult:
    """A deck on disk that the listing lacks: removed only once Archidekt itself says it is gone (ADR 0045).

    That needs a signed-in client, because an anonymous listing cannot show private decks and an anonymous
    page read may answer 404 for one; the listing then was complete, as an incomplete one raises or falls
    back to an anonymous client. Anything but a 404 on the deck's own page keeps the folder.
    """
    deck = stored.deck
    missing = SyncResult(deck.id, deck.name, SyncStatus.MISSING_UPSTREAM)
    if not client.signed_in or deck.owner.casefold() != user.casefold():
        return missing
    try:
        gone = client.deck_is_gone(deck.id)
    except (NetworkError, ArchidektError, AuthError) as error:
        log.info(
            "deck %d (%s) is not in the listing; its page was not confirmed gone: %s",
            deck.id,
            deck.name,
            error,
        )
        return replace(
            missing,
            error=f"not in the listing, and Archidekt did not say it was deleted ({error}); not removed",
        )
    if not gone:
        return replace(
            missing, error="not in the listing, but its page still reads on Archidekt; not removed"
        )
    if dry_run:
        return replace(missing, status=SyncStatus.WOULD_REMOVE)
    try:
        with watch.deck_lock(deck.id):
            folder = workspace.remove_deck(deck.id)
    except (OSError, WorkspaceError) as error:
        log.warning("deck %d (%s): could not remove its folder: %s", deck.id, deck.name, error)
        return replace(
            missing,
            status=SyncStatus.FAILED,
            error=f"deleted on Archidekt, but its folder was not removed: {error}",
        )
    log.info("deck %d (%s) was deleted on Archidekt; removed its folder %s", deck.id, deck.name, folder.name)
    watch.on_removed(deck.id)
    return replace(missing, status=SyncStatus.REMOVED)


def _write(  # noqa: PLR0917 — the one step of sync_decks' loop
    future: Future[Deck],
    plan: _Plan,
    workspace: Workspace,
    user: str,
    dry_run: bool,  # noqa: FBT001
    on_progress: Callable[[str], None],
    watch: PullWatch,
) -> SyncResult:
    """The fetched deck written to its folder under its lock, or why it was not."""
    try:
        deck = future.result()
    except (NetworkError, ArchidektError, AuthError) as error:
        return _failed(plan.summary, str(error))
    on_progress(f"fetched {deck.name}")
    if deck.owner.casefold() != user.casefold():
        return _failed(plan.summary, f"belongs to {deck.owner}, not {user}; skipped")
    with watch.deck_lock(deck.id):
        return _apply(workspace, plan.previous, deck, dry_run=dry_run)


def _is_current(workspace: Workspace, previous: Deck, summary: DeckSummary) -> bool:
    folder = workspace.folder_for(summary.id)
    return (
        previous.updated_at == summary.updated_at
        and previous.tags is not None
        and previous.labels_known
        # Archidekt keeps the edit time when only the deck tags change, so the list's tags are compared too.
        and (summary.tags is None or summary.tags == previous.tags)
        and summary.private == previous.private
        and summary.unlisted == previous.unlisted
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
