"""A full sync: the decks, the card data, the account's labels, the ideas and the re-judging of the open ones
(ADR 0023).

Every write to a deck folder, the pull's, the [grace] tables' and the ideas merge's, holds that deck's
`deck_lock`; the pull also tells its caller which decks it will still write, so the app refuses a write only
to those (ADR 0042). The ideas phase fetches with no lock held.

`cmdr sync` and the app's Sync button both run it; neither prints from in here, so
each failure comes back as a warning to show the way the caller shows things.
"""

import logging
from collections.abc import Callable, Generator
from concurrent.futures import FIRST_COMPLETED, Future, wait
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from commander.archidekt import ArchidektClient, ArchidektError, AuthError
from commander.archidekt_edit import ArchidektAuth, ArchidektEditor
from commander.background import in_background, run_all
from commander.board import BoardCard, BoardError, BoardService, Origin, count_text
from commander.bracket import CACHE_FOLDER as SPELLBOOK_CACHE_FOLDER
from commander.bracket import estimate_cache, estimated_decks
from commander.carddb import CARD_DB_FILE, CardDataError, CardDb
from commander.config import MetaSettings
from commander.credentials import CredentialError, TokenStore
from commander.deckcache import Clock
from commander.decklocks import DeckLock
from commander.deckprofile import ProfileError, profile_path, write_profile_template
from commander.fetchpriority import FetchPriority, fetching_deck
from commander.ideas import compile_ideas, gather_idea_pools, rejudge_ideas, relabel_legacy_ideas
from commander.metadata import (
    TOURNAMENT_BRACKETS,
    TOURNAMENTS_STEP,
    AdjacentDecks,
    MetaData,
    MetaSources,
    Progress,
    meta_sources,
    prune_deck_reads,
    start_gather_for,
    start_tournament_refresh,
)
from commander.net import Fetcher, NetworkError, Pacer
from commander.phases import ActivePhases
from commander.profileedit import ensure_grace_table
from commander.scryfall import CardRefresh, RefreshStatus, ScryfallClient, ScryfallError, refresh_card_db
from commander.spellbook import SpellbookClient
from commander.store import StoredDeck, Workspace
from commander.sync import PRIVATE_NOT_UPDATED, PullWatch, SyncResult, SyncStatus, sync_decks
from commander.topdeck import KEY_REMEDY
from commander.viewscore import VIEWS_FILE, ViewScores

if TYPE_CHECKING:
    import httpx

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class SyncServices:
    """What a sync reaches outside the deck folder, injected so tests run it offline."""

    client_factory: Callable[[], "httpx.Client"]
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
    # Why the card data was rebuilt, and the day it is next rebuilt for its age; None when they do not apply.
    reason: str | None = None
    next_refresh: date | None = None


@dataclass(slots=True, frozen=True)
class SyncOutcome:
    results: list[SyncResult]
    cards: CardStatus
    # The account's card labels, None when not signed in, on a dry run or when the read failed.
    labels: list[tuple[str, str]] | None
    # Per deck, by short name: the ideas turned down for no longer passing the idea test, those compiled and
    # the open cards closed because the main deck holds them (ADR 0046).
    turned_down: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    ideas: dict[str, dict[Origin, list[BoardCard]]] = field(
        default_factory=dict[str, dict[Origin, list[BoardCard]]]
    )
    closed: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    # Per deck, by short name: the queued count changes ("Mountain 4 → 6") cleared as the deck meets them.
    counts_cleared: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
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
    pull: PullWatch | None = None,
    on_cards: Callable[[], None] | None = None,
) -> SyncOutcome:
    """The whole sync. `pull` follows and steers the deck pull, and its `deck_lock` covers every write to a
    deck folder; `on_cards` is told once the card data is in place, or will not change (ADR 0042)."""
    warnings: list[str] = []
    pull = pull or PullWatch(priority=priority)
    deck_lock = pull.deck_lock
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
        if on_cards is not None:
            if cards_job is None:
                on_cards()
            else:
                cards_job.add_done_callback(lambda _: on_cards())
        try:
            login = _sign_in(fetcher, user, services)
            if login.problem is not None:
                log.warning("Archidekt login not usable: %s", login.problem)
                warnings.append(f"Archidekt login not usable ({login.problem}): {PRIVATE_NOT_UPDATED}")
            results = sync_decks(
                ArchidektClient(fetcher, token=login.token, pacer=services.pacer_factory(0.0)),
                workspace,
                user,
                force=options.force,
                dry_run=options.dry_run,
                on_progress=on_progress,
                warn=warnings.append,
                watch=pull,
            )
            if not login.attempted and any(stored.deck.needs_login for stored in workspace.load_all()):
                warnings.append(f"Not signed in to Archidekt: {PRIVATE_NOT_UPDATED}")
            labels = None if options.dry_run else _sync_labels(fetcher, workspace, login, warnings)
        except Exception:
            # The download shares this client, which closes on leaving the block: wait for it, and log
            # its status because there is no outcome to carry it.
            card_status = _card_status(cards_job, options)
            log.info("card data: %s, %s", card_status.state.value, card_status.detail)
            raise
        cards = _card_status(cards_job, options)
    if options.dry_run:
        return SyncOutcome(results, cards, labels, warnings=warnings)
    _add_grace_tables(workspace, warnings, deck_lock)
    phase = (
        _compile_ideas(
            workspace,
            user,
            settings,
            services,
            warnings=warnings,
            progress=on_progress,
            priority=priority,
            on_deck=on_deck or _ignore_deck,
            deck_lock=deck_lock,
        )
        if options.ideas
        else _IdeasPhase()
    )
    return SyncOutcome(
        results,
        cards,
        labels,
        phase.turned_down,
        phase.ideas,
        phase.closed,
        phase.counts_cleared,
        warnings,
    )


def _add_grace_tables(workspace: Workspace, warnings: list[str], deck_lock: DeckLock) -> None:
    """Give each deck's profile the empty [grace] table (ADR 0039), writing the template for a deck with no
    profile yet, under the deck's lock; a profile that cannot be written is a warning."""
    for deck_id, folder in workspace.deck_folders():
        try:
            with deck_lock(deck_id):
                _add_grace_table(workspace, folder)
        except (OSError, ProfileError) as error:
            log.warning("deck %s: could not add the [grace] table: %s", deck_id, error)
            warnings.append(f"deck {deck_id}: [grace] table not added: {error}")


def _add_grace_table(workspace: Workspace, folder: Path) -> None:
    # A save that held the lock may have renamed the folder after a rename on Archidekt; the next sync has it.
    if not folder.is_dir():
        return
    if not profile_path(folder).exists():
        deck = workspace.load_folder(folder)
        if deck is not None:
            write_profile_template(folder, deck)
            log.info("%s: wrote profile.toml from the template", folder.name)
    elif ensure_grace_table(folder):
        log.info("%s: added an empty [grace] table to profile.toml", folder.name)


@dataclass(slots=True, frozen=True)
class _Login:
    """What the saved Archidekt login gave this sync."""

    # The access token; None without a login or when it could not be used.
    token: str | None = None
    # Why a saved login could not be used; None when there is none or it worked.
    problem: str | None = None

    @property
    def attempted(self) -> bool:
        """Whether there was a saved login to try."""
        return self.token is not None or self.problem is not None


def _sign_in(fetcher: Fetcher, user: str, services: SyncServices) -> _Login:
    """An access token for the saved login, which lets the sync list and read the private and unlisted decks
    too (ADR 0040); a login that cannot be used is a problem for the caller to warn about, not an error."""
    auth = ArchidektAuth(fetcher, services.token_store)
    try:
        if not auth.has_login(user):
            return _Login()
        return _Login(token=auth.access_token(user))
    except (AuthError, CredentialError, NetworkError, ArchidektError) as error:
        return _Login(problem=str(error))


def _sync_labels(
    fetcher: Fetcher, workspace: Workspace, login: _Login, warnings: list[str]
) -> list[tuple[str, str]] | None:
    """Keep the account's card labels for the app (ADR 0012); None when not signed in or the read fails."""
    if login.problem is not None:
        warnings.append(f"card labels not refreshed: {login.problem}")
        return None
    if login.token is None:
        return None
    try:
        labels = ArchidektEditor(fetcher, login.token).account_labels()
    except (AuthError, NetworkError, ArchidektError) as error:
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
        due = f", next refresh {refresh.next_due} or with the next new set" if refresh.next_due else ""
        return CardStatus(CardState.CURRENT, source + due, next_refresh=refresh.next_due)
    why = f"{refresh.reason}, " if refresh.reason else ""
    detail = f"{why}{source}, {refresh.printings or 0:,} printings"
    return CardStatus(CardState.UPDATED, detail, reason=refresh.reason or None)


@dataclass(slots=True)
class _IdeasPhase:
    """What the ideas phase did, per deck by short name; the fields of the same names in `SyncOutcome`."""

    ideas: dict[str, dict[Origin, list[BoardCard]]] = field(
        default_factory=dict[str, dict[Origin, list[BoardCard]]]
    )
    turned_down: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    closed: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    counts_cleared: dict[str, list[str]] = field(default_factory=dict[str, list[str]])


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
    deck_lock: DeckLock,
) -> _IdeasPhase:
    """Each deck's new ideas from the meta and recent Archidekt decks (ADR 0028), by short name, the open
    ideas turned down on the way for no longer passing the idea test, the open cards closed first
    because the main deck holds them (ADR 0046) and the queued count changes it already meets, cleared
    (ADR 0043).

    Answers under a day old come from the cache. A deck whose sources fail keeps its board as it was.
    Every deck's reads start at once, each on its own thread, and the sync thread merges them as they finish.
    The reads' Archidekt requests go out in the order of how often the user opens the decks, the one the
    priority names ahead of those (ADR 0034, ADR 0049), and so does the merge when several are ready. The
    reads hold no lock; the merge holds the deck's lock and works from the deck and board as they are then,
    so a change the user made meanwhile stays (ADR 0042).
    """
    try:
        db = CardDb(services.cache / CARD_DB_FILE)
    except CardDataError as error:
        warnings.append(f"ideas not compiled: {error}")
        return _IdeasPhase()
    phase = _IdeasPhase()
    # Reads run side by side, so the line names each one that is under way.
    phases = ActivePhases(progress, idle=_COMPILING)
    waiting = _most_viewed_first(workspace.load_all(), services)
    order = [s.deck.id for s in waiting]
    next_up = priority or FetchPriority()
    with db, services.client_factory() as http:
        sources = meta_sources(
            http,
            services.pacer_factory,
            user,
            settings,
            services.cache,
            waits=_TopdeckWaits(phases),
            rank=lambda deck_id: next_up.rank(deck_id, order),
            rank_changes=next_up.watch,
        )
        waiting = _with_estimated_brackets(waiting, http, services, phases)
        # TopDeck's window is fetched once, on its own thread, while every deck's Archidekt and Spellbook
        # reads go ahead; only a deck at a bracket that reads tournament lists waits for it to finish.
        tournaments = start_tournament_refresh(
            (s.deck for s in waiting),
            sources,
            now=services.now,
            on_progress=phases.start,
            on_done=phases.done,
        )
        for queued in waiting:
            on_deck(queued.deck.id, FetchState.QUEUED)
        with _settled(tournaments):
            reading = _start_reading(
                waiting, sources, services, tournaments=tournaments, phases=phases, on_deck=on_deck
            )
            while reading:
                read = next_up.pick(_finished(reading), lambda r: r.stored.deck.id)
                reading.remove(read)
                _merge_deck(
                    read,
                    workspace,
                    db,
                    services,
                    phase,
                    warnings=warnings,
                    on_deck=on_deck,
                    deck_lock=deck_lock,
                )
            if tournaments is not None:
                # The client closes with this block, so TopDeck's thread must be over first.
                _wait_for(tournaments)
                # `start_tournament_refresh` has logged any error; the reads have its failure to report.
                if tournaments.exception() is not None:
                    warnings.append("TopDeck's tournament lists were not fetched: unexpected error")
        # Every deck's reads are over, so none is using a cached deck read the prune could remove.
        prune_deck_reads(services.cache, settings, now=services.now)
    return phase


_ESTIMATES_STEP = "Spellbook: bracket estimates"


def _with_estimated_brackets(
    waiting: list[StoredDeck], http: "httpx.Client", services: SyncServices, phases: ActivePhases
) -> list[StoredDeck]:
    """The decks as their reads and ideas see them (ADR 0051): one with no bracket set has its estimate's
    lowest. The estimate comes from the cache while fresh and is otherwise asked of Spellbook; a deck whose
    estimate fails stays unset, and the failure is logged."""
    if all(s.deck.bracket is not None for s in waiting):
        return waiting
    phases.start(_ESTIMATES_STEP)
    try:
        decks = estimated_decks(
            [s.deck for s in waiting],
            SpellbookClient(Fetcher(http)),
            estimate_cache(services.cache / SPELLBOOK_CACHE_FOLDER),
            now=services.now,
        )
    finally:
        phases.done(_ESTIMATES_STEP)
    return [StoredDeck(s.folder, deck) for s, deck in zip(waiting, decks, strict=True)]


# How long an error in the ideas phase waits for TopDeck's thread before the client closes under it.
_TOPDECK_GRACE_SECONDS = 10.0
# A wait in slices, because on Windows Ctrl-C does not interrupt a wait without a timeout.
_POLL_SECONDS = 0.5


@contextmanager
def _settled(tournaments: Future[None] | None) -> Generator[None]:
    """After an error, gives TopDeck's thread a moment to end before the client closes under it; it logs its
    own failure. Ctrl-C is not an Exception, so it ends the run at once."""
    try:
        yield
    except Exception:
        if tournaments is not None:
            _wait_for(tournaments, up_to=_TOPDECK_GRACE_SECONDS)
        raise


def _wait_for(future: Future[None], *, up_to: float | None = None) -> None:
    """Wait until the future is done, or for `up_to` seconds at most."""
    waited = 0.0
    while not future.done() and (up_to is None or waited < up_to):
        wait([future], timeout=_POLL_SECONDS)
        waited += _POLL_SECONDS


# The name of the threads that read the decks, followed by a number.
READ_THREAD = "deck-read"
# Shown when no read is under way, as the ideas are merged.
_COMPILING = "Compiling ideas"


class _TopdeckWaits:
    """Shows a wait for TopDeck's rate limit in the place of the step that is waiting."""

    def __init__(self, phases: ActivePhases) -> None:
        self._phases = phases
        self._text = ""
        self._replaced = False

    def waiting(self, seconds: float) -> None:
        self._text = f"TopDeck: waiting {seconds:.0f} s (rate limit)"
        self._replaced = self._phases.replace(TOURNAMENTS_STEP, self._text)
        if not self._replaced:
            self._phases.start(self._text)

    def resumed(self) -> None:
        if self._replaced:
            self._phases.replace(self._text, TOURNAMENTS_STEP)
        else:
            self._phases.done(self._text)


@dataclass(slots=True, frozen=True)
class _DeckRead:
    """What a deck's reads found: the decks of its alike commanders and tags, and the meta."""

    pools: AdjacentDecks
    data: MetaData


@dataclass(slots=True, eq=False)
class _ReadingDeck:
    """A deck whose reads run on a thread of their own."""

    stored: StoredDeck
    read: Future[_DeckRead]
    # What went wrong reading it, one sentence each; complete once `read` is.
    warnings: list[str]


def _start_reading(
    waiting: list[StoredDeck],
    sources: MetaSources,
    services: SyncServices,
    *,
    tournaments: Future[None] | None,
    phases: ActivePhases,
    on_deck: DeckProgress,
) -> list[_ReadingDeck]:
    """Start every deck's reads at once, each on its own thread; the decks come back in their order."""
    warnings: list[list[str]] = [[] for _ in waiting]

    def job(stored: StoredDeck, read_warnings: list[str]) -> Callable[[], _DeckRead]:
        return lambda: _read_deck(
            stored,
            sources,
            services,
            tournaments=tournaments,
            phases=phases,
            on_deck=on_deck,
            warnings=read_warnings,
        )

    jobs = [job(stored, found) for stored, found in zip(waiting, warnings, strict=True)]
    futures = run_all(jobs, limit=max(len(jobs), 1), name=READ_THREAD)
    return [
        _ReadingDeck(stored, future, found)
        for stored, future, found in zip(waiting, futures, warnings, strict=True)
    ]


def _read_deck(
    stored: StoredDeck,
    sources: MetaSources,
    services: SyncServices,
    *,
    tournaments: Future[None] | None,
    phases: ActivePhases,
    on_deck: DeckProgress,
    warnings: list[str],
) -> _DeckRead:
    """All of one deck's reads, on the thread that runs it. Its Archidekt requests name the deck, which is how
    the pacer ranks them against the other decks'; the thread keeps its own read of the card data, as a
    sqlite connection belongs to the thread that opened it."""
    fetching_deck.set(stored.deck.id)
    on_deck(stored.deck.id, FetchState.FETCHING)
    finish = start_gather_for(
        stored.deck,
        sources,
        services.cache,
        now=services.now,
        refresh=False,
        on_progress=phases.start,
        on_done=phases.done,
        prune=False,
    )
    with CardDb(services.cache / CARD_DB_FILE) as db:
        pools = gather_idea_pools(
            stored,
            db,
            sources,
            services.cache,
            now=services.now,
            refresh=False,
            warn=warnings.append,
            on_progress=phases.start,
            on_done=phases.done,
            prune=False,
        )
    if tournaments is not None and stored.deck.bracket in TOURNAMENT_BRACKETS:
        # Only TopDeck's own thread asks it for events; the lists are read from what it stored.
        wait([tournaments])
    return _DeckRead(pools, finish())


def _finished(reading: list[_ReadingDeck]) -> list[_ReadingDeck]:
    """The reads that are over, once there is one, in the decks' order."""
    while True:
        done = [r for r in reading if r.read.done()]
        if done:
            return done
        wait([r.read for r in reading], timeout=_POLL_SECONDS, return_when=FIRST_COMPLETED)


def _merge_deck(
    read: _ReadingDeck,
    workspace: Workspace,
    db: CardDb,
    services: SyncServices,
    phase: _IdeasPhase,
    *,
    warnings: list[str],
    on_deck: DeckProgress,
    deck_lock: DeckLock,
) -> None:
    """Merge the deck's finished read into its board, under its lock."""
    deck = read.stored.deck
    try:
        finished = read.read.result()
    except (BoardError, ProfileError) as error:
        _not_compiled(read, str(error), warnings, on_deck)
        return
    except Exception as error:  # one deck's failed read must not stop the others' merges
        log.warning("deck %d: its reads failed unexpectedly", deck.id, exc_info=error)
        _not_compiled(read, f"unexpected {type(error).__name__}: {error}", warnings, on_deck)
        return
    pools, data = finished.pools, finished.data
    try:
        with deck_lock(deck.id):
            current = _current(workspace, read.stored)
            if current is None:
                warnings.extend(read.warnings)
                warnings.append(f"{deck.short_name} ideas not merged: {_GONE}")
                on_deck(deck.id, FetchState.INCOMPLETE)
                return
            # Before the re-judge, so a held card is closed and not turned down for its share.
            board_service = BoardService(current.folder, current.deck, db, now=services.now)
            held = board_service.close_held()
            if held:
                phase.closed[deck.short_name] = [card.name for card in held]
            met = board_service.clear_met_counts()
            if met:
                phase.counts_cleared[deck.short_name] = [count_text(c) for c in met]
            dropped = rejudge_ideas(
                current,
                data,
                db,
                now=services.now,
                adjacent=None if pools.groups_failed else pools.decks,
                tagged=pools.tagged,
            )
            if dropped:
                phase.turned_down[deck.short_name] = [card.name for card in dropped]
            relabel_legacy_ideas(current, data, db, now=services.now)
            phase.ideas[deck.short_name] = compile_ideas(
                current, data, db, now=services.now, adjacent=pools.decks, tagged=pools.tagged
            )
    except (BoardError, ProfileError) as error:
        _not_compiled(read, str(error), warnings, on_deck)
        return
    if data.lists.data is None and data.peers.data is None:
        why = data.peers.error or data.lists.error
        read.warnings.append(f"{deck.short_name}: no meta or Archidekt decks to learn from ({why})")
    warnings.extend(read.warnings)
    key_problem = _note_key_problem(data.lists.key_problem, warnings)
    incomplete = bool(read.warnings) or data.peers.data is None or key_problem
    on_deck(deck.id, FetchState.INCOMPLETE if incomplete else FetchState.DONE)


def _not_compiled(read: _ReadingDeck, why: str, warnings: list[str], on_deck: DeckProgress) -> None:
    warnings.extend(read.warnings)
    warnings.append(f"{read.stored.deck.short_name} ideas not compiled: {why}")
    on_deck(read.stored.deck.id, FetchState.INCOMPLETE)


_GONE = "the deck left its folder or changed its commander while its ideas were fetched"
_KEY_WARNING = f"tournament lists skipped: missing or refused TopDeck key; {KEY_REMEDY}"


def _note_key_problem(key_problem: bool, warnings: list[str]) -> bool:  # noqa: FBT001 — an answer's flag
    """Whether the tournament lists lack a working TopDeck key; warns once per sync."""
    if not key_problem:
        return False
    if _KEY_WARNING not in warnings:
        warnings.append(_KEY_WARNING)
    return True


def _current(workspace: Workspace, fetched: StoredDeck) -> StoredDeck | None:
    """The deck as saved now, which a save from the app may have changed since its ideas were fetched; None
    when its folder is gone or renamed, or it no longer has the commander the ideas were fetched for."""
    deck = workspace.load_folder(fetched.folder)
    if deck is None or deck.commanders != fetched.deck.commanders:
        log.info("deck %d: ideas not merged: %s", fetched.deck.id, _GONE)
        return None
    # The ideas are judged at the bracket the reads used, unless the user has set one since.
    bracket = deck.bracket if deck.bracket is not None else fetched.deck.bracket
    return StoredDeck(fetched.folder, replace(deck, bracket=bracket))


def _most_viewed_first(decks: list[StoredDeck], services: SyncServices) -> list[StoredDeck]:
    """The decks with a commander, by view score and then in the workspace's order."""
    scores = ViewScores(services.cache / VIEWS_FILE, services.now).scores()
    return sorted((s for s in decks if s.deck.commanders), key=lambda s: -scores.get(s.deck.id, 0.0))


def _ignore_deck(_deck_id: int, _state: FetchState) -> None:
    return
