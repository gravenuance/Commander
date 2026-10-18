"""A full sync: the decks, the card data, the account's labels, the boards and the ideas (ADR 0023).

`cmdr sync` and the app's Sync button both run it; neither prints from in here, so
each failure comes back as a warning to show the way the caller shows things.
"""

import logging
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import httpx

from commander.archidekt import ArchidektClient, ArchidektError
from commander.archidekt_edit import ArchidektAuth, ArchidektEditor, AuthError
from commander.background import in_background
from commander.board import BoardCard, BoardError, Origin
from commander.carddb import CARD_DB_FILE, CardDataError, CardDb
from commander.config import MetaSettings
from commander.credentials import CredentialError, TokenStore
from commander.deckcache import Clock
from commander.deckprofile import ProfileError
from commander.fetchpriority import FetchPriority
from commander.ideas import compile_ideas, gather_idea_pools, rejudge_ideas
from commander.metadata import Progress, gather_for, meta_sources
from commander.net import Fetcher, NetworkError, Pacer
from commander.scryfall import CardRefresh, RefreshStatus, ScryfallClient, ScryfallError, refresh_card_db
from commander.store import StoredDeck, Workspace
from commander.sync import SyncResult, SyncStatus, sync_decks
from commander.viewscore import VIEWS_FILE, ViewScores

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class SyncServices:
    """What a sync reaches outside the deck folder, injected so tests run it offline."""

    client_factory: Callable[[], httpx.Client]
    token_store: TokenStore
    # The card database and the meta answers live here.
    cache: Path
    now: Clock
    pacer_factory: Callable[[float], Pacer] = Pacer


@dataclass(slots=True, frozen=True)
class SyncOptions:
    force: bool = False
    dry_run: bool = False
    cards: bool = True
    ideas: bool = True


class FetchState(Enum):
    """Where a deck stands in the ideas phase; the app shows it beside the deck."""

    QUEUED = "queued"
    FETCHING = "fetching"
    DONE = "done"
    # Finished with a warning, or without the sources it needed.
    INCOMPLETE = "incomplete"


# Told as each deck's state changes: the deck id and its new state.
DeckProgress = Callable[[int, FetchState], None]


class CardState(Enum):
    SKIPPED = "skipped"
    CURRENT = "current"
    UPDATED = "updated"
    FAILED = "failed"


@dataclass(slots=True, frozen=True)
class CardStatus:
    state: CardState
    detail: str


@dataclass(slots=True, frozen=True)
class SyncOutcome:
    results: list[SyncResult]
    cards: CardStatus
    # The account's card labels, None when not signed in, on a dry run or when the read failed.
    labels: list[tuple[str, str]] | None
    # Per deck, by short name: the ideas turned down for no longer passing the idea test, and those compiled.
    turned_down: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    ideas: dict[str, dict[Origin, list[BoardCard]]] = field(
        default_factory=dict[str, dict[Origin, list[BoardCard]]]
    )
    # What went wrong without stopping the sync, one sentence each.
    warnings: list[str] = field(default_factory=list[str])

    @property
    def failed(self) -> bool:
        return (
            any(r.status is SyncStatus.FAILED for r in self.results) or self.cards.state is CardState.FAILED
        )


def run_sync(
    services: SyncServices,
    workspace: Workspace,
    user: str,
    settings: MetaSettings,
    options: SyncOptions = SyncOptions(),  # noqa: B008 — frozen, so one shared default is safe
    *,
    priority: FetchPriority | None = None,
    on_deck: DeckProgress | None = None,
    on_progress: Progress,
) -> SyncOutcome:
    warnings: list[str] = []
    with services.client_factory() as http:
        fetcher = Fetcher(http)
        # The card download is independent of the decks, so it runs while they sync.
        cards_job: Future[CardRefresh] | None = None
        if not options.dry_run and options.cards:
            scryfall = ScryfallClient(fetcher)
            cards_job = in_background(
                lambda: refresh_card_db(
                    scryfall, services.cache, now=services.now, force=options.force, on_progress=on_progress
                ),
                name="card-data",
            )
        try:
            results = sync_decks(
                ArchidektClient(fetcher),
                workspace,
                user,
                force=options.force,
                dry_run=options.dry_run,
                on_progress=on_progress,
            )
            labels = None if options.dry_run else _sync_labels(fetcher, workspace, user, services, warnings)
        except Exception:
            # The download shares this client, which closes on leaving the block: wait for it, and log
            # its status because there is no outcome to carry it.
            card_status = _card_status(cards_job, options)
            log.info("card data: %s, %s", card_status.state.value, card_status.detail)
            raise
        cards = _card_status(cards_job, options)
    if options.dry_run:
        return SyncOutcome(results, cards, labels, warnings=warnings)
    ideas, turned_down = (
        _compile_ideas(
            workspace,
            user,
            settings,
            services,
            warnings=warnings,
            progress=on_progress,
            priority=priority,
            on_deck=on_deck or _ignore_deck,
        )
        if options.ideas
        else ({}, {})
    )
    return SyncOutcome(results, cards, labels, turned_down, ideas, warnings)


def _sync_labels(
    fetcher: Fetcher, workspace: Workspace, user: str, services: SyncServices, warnings: list[str]
) -> list[tuple[str, str]] | None:
    """Keep the account's card labels for the app (ADR 0012); None when not signed in or the read fails."""
    auth = ArchidektAuth(fetcher, services.token_store)
    if not auth.has_login(user):
        return None
    try:
        labels = ArchidektEditor(fetcher, auth.access_token(user)).account_labels()
    except (AuthError, CredentialError, NetworkError, ArchidektError) as error:
        warnings.append(f"card labels not refreshed: {error}")
        return None
    try:
        workspace.save_account_labels(labels)
    except OSError as error:
        log.warning("could not save the card labels: %s", error)
        warnings.append(f"card labels not saved: {error}")
    return labels


def _card_status(job: Future[CardRefresh] | None, options: SyncOptions) -> CardStatus:
    if job is None:
        return CardStatus(CardState.SKIPPED, "dry run" if options.dry_run else "--no-cards")
    try:
        refresh = job.result()
    except (NetworkError, ScryfallError, CardDataError, OSError) as error:
        log.info("card data refresh failed: %s", error)
        return CardStatus(CardState.FAILED, f"{error}; the previous card data is kept")
    source = f"Scryfall data of {refresh.meta.cards_updated_at[:10]}"
    if refresh.status is RefreshStatus.CURRENT:
        return CardStatus(CardState.CURRENT, source)
    return CardStatus(CardState.UPDATED, f"{source}, {refresh.printings or 0:,} printings")


def _compile_ideas(
    workspace: Workspace,
    user: str,
    settings: MetaSettings,
    services: SyncServices,
    *,
    warnings: list[str],
    progress: Progress,
    priority: FetchPriority | None,
    on_deck: DeckProgress,
) -> tuple[dict[str, dict[Origin, list[BoardCard]]], dict[str, list[str]]]:
    """Each deck's new ideas from the meta and recent Archidekt decks (ADR 0028), by short name, and the open
    ideas turned down on the way for no longer passing the idea test.

    Answers under a day old come from the cache. A deck whose sources fail keeps its board as it was.
    The decks go in the order of how often the user opens them, and the one the priority names goes
    ahead of those (ADR 0034).
    """
    try:
        db = CardDb(services.cache / CARD_DB_FILE)
    except CardDataError as error:
        warnings.append(f"ideas not compiled: {error}")
        return {}, {}
    found: dict[str, dict[Origin, list[BoardCard]]] = {}
    turned_down: dict[str, list[str]] = {}
    with db, services.client_factory() as http:
        sources = meta_sources(http, services.pacer_factory, user, settings)
        waiting = _most_viewed_first(workspace.load_all(), services)
        next_up = priority or FetchPriority()
        for queued in waiting:
            on_deck(queued.deck.id, FetchState.QUEUED)
        while waiting:
            stored = next_up.pick(waiting, lambda s: s.deck.id)
            waiting.remove(stored)
            deck = stored.deck
            warned = len(warnings)
            on_deck(deck.id, FetchState.FETCHING)
            try:
                data = gather_for(
                    deck, sources, services.cache, now=services.now, refresh=False, on_progress=progress
                )
                pools = gather_idea_pools(
                    stored,
                    db,
                    sources,
                    services.cache,
                    now=services.now,
                    refresh=False,
                    warn=warnings.append,
                    on_progress=progress,
                )
                dropped = rejudge_ideas(
                    stored,
                    data,
                    db,
                    now=services.now,
                    adjacent=None if pools.groups_failed else pools.decks,
                    tagged=pools.tagged,
                )
                if dropped:
                    turned_down[deck.short_name] = [card.name for card in dropped]
                found[deck.short_name] = compile_ideas(
                    stored, data, db, now=services.now, adjacent=pools.decks, tagged=pools.tagged
                )
            except (BoardError, ProfileError) as error:
                warnings.append(f"{deck.short_name} ideas not compiled: {error}")
                on_deck(deck.id, FetchState.INCOMPLETE)
                continue
            if data.lists.data is None and data.peers.data is None:
                why = data.peers.error or data.lists.error
                warnings.append(f"{deck.short_name}: no meta or Archidekt decks to learn from ({why})")
            incomplete = len(warnings) > warned or data.peers.data is None
            on_deck(deck.id, FetchState.INCOMPLETE if incomplete else FetchState.DONE)
    return found, turned_down


def _most_viewed_first(decks: list[StoredDeck], services: SyncServices) -> list[StoredDeck]:
    """The decks with a commander, by view score and then in the workspace's order."""
    scores = ViewScores(services.cache / VIEWS_FILE, services.now).scores()
    return sorted((s for s in decks if s.deck.commanders), key=lambda s: -scores.get(s.deck.id, 0.0))


def _ignore_deck(_deck_id: int, _state: FetchState) -> None:
    return
