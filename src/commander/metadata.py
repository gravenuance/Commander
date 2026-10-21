"""What other players run with a commander, gathered for `cmdr meta` and for the ideas `cmdr sync` compiles.

Each answer is cached in the cache folder (never the repo: the tournament data is not ours to redistribute),
for a day unless stated, and the sources are fetched in parallel, each at its own service's pace:

- the commander's tournament entries with decklists, from TopDeck.gg, for decks at bracket 4 and 5,
- the most-played commanders and their best-placed tournament lists (bracket 5 only), worked out from the same
  stored events,
- the most-viewed recent Archidekt decks for the commander at the deck's bracket and the one above, other
  players' only (a search answer is refreshed every third day, staggered per commander, paging only to the
  newest deck it already holds), each deck read kept until its owner edits it (ADR 0027),
- the combos inside each top commander's core cards, from Commander Spellbook (a week; bracket 5 only),
- the deck's own combos, from Commander Spellbook (a week, until the list changes; bracket 5 only),
- for `sync`, the decks of the commanders the profile names as alike and of other commanders carrying one of
  the deck's theme tags (ADR 0028), beside the above rather than inside `MetaData`.
"""

import logging
import os
import threading
from collections import Counter
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path

import httpx

from commander.archidekt import (
    MAX_SEARCH_PAGES,
    MIN_DECKS,
    SEARCH_INTERVAL_SECONDS,
    TAG_READ_DIVISOR,
    TAG_SEARCH_PAGES_PER_BRACKET,
    VIEW_FLOORS,
    ArchidektError,
    ArchidektSearch,
    PeerDeck,
    brackets_to_read,
    most_viewed,
)
from commander.background import in_background
from commander.config import MetaSettings
from commander.credentials import KeyringKeyStore, KeyStore
from commander.deckcache import (
    NOT_FETCHED,
    Answer,
    AnswerSource,
    Clock,
    DeckCache,
    NotFetchedError,
    cache_item,
    cached_answer,
    request_key,
)
from commander.deckreads import FOLDER as DECK_READS_FOLDER
from commander.deckreads import DeckRead, DeckReads, decode_multiples, read_of
from commander.jsonshape import (
    ShapeError,
    as_list,
    as_object,
    opt_int,
    req_bool,
    req_float,
    req_int,
    req_list,
    req_object,
    req_str,
    str_list,
)
from commander.models import Deck
from commander.net import Fetcher, HttpStatusError, NetworkError, Pacer, WaitListener
from commander.peerschedule import fresh_for as peer_fresh_for
from commander.peersearch import SearchHits, update_hits
from commander.spellbook import FRESH_FOR as SPELLBOOK_FRESH_FOR
from commander.spellbook import (
    META_REQUEST_INTERVAL_SECONDS,
    ComboDetail,
    ComboPiece,
    SpellbookClient,
    SpellbookError,
    deck_request,
)
from commander.topdeck import REQUEST_INTERVAL_SECONDS, SLOW_QUERY_SECONDS, TopdeckClient, TopdeckError
from commander.topdeckstore import TournamentStore
from commander.tournaments import (
    CommanderStats,
    DecklistCard,
    Entry,
    EntrySort,
    Tournament,
    TournamentSource,
    commander_name,
)

log = logging.getLogger(__name__)

# A deck read answered with one of these means that deck went private or was deleted, not that Archidekt is
# unwell.
_DECK_GONE_STATUSES = frozenset({403, 404, 410})

FRESH_FOR = timedelta(hours=24)
CACHE_VERSION = 2
CACHE_FOLDER = "meta"
# Meta answers of the retired EDHTop16 provider (ADR 0047), which no run reads again.
_RETIRED_PREFIX = "edhtop16"
# The cEDH bracket: the only one the tournaments cover.
CEDH_BRACKET = 5
# Decks at these brackets learn from the commander's tournament lists too: bracket 4 plays near cEDH.
TOURNAMENT_BRACKETS = frozenset({4, CEDH_BRACKET})
# Enough for the busiest commanders' two months.
MAX_OWN_ENTRIES = 400


_ERRORS = (NetworkError, TopdeckError, ArchidektError, SpellbookError, NotFetchedError)
# A card in this share of a commander's lists is part of its core, where its combos are looked for.
COMBO_CORE_SHARE = 0.2

Progress = Callable[[str], None]


@dataclass(slots=True, frozen=True)
class CommanderEntries:
    commander: str
    # False when no event in the window had the commander.
    known: bool
    entries: tuple[Entry, ...]


@dataclass(slots=True, frozen=True)
class MetaCommander:
    stats: CommanderStats
    entries: tuple[Entry, ...]


@dataclass(slots=True, frozen=True)
class PeerList:
    deck: PeerDeck
    cards: tuple[str, ...]
    # The cards its owner files under a category naming the plan, like "Combo Pieces" (ADR 0017).
    planned: tuple[str, ...] = ()
    # Cards played more than once (basic lands mostly), with their count; None in caches from before
    # ADR 0019, which can't say how many lands a deck runs.
    multiples: tuple[tuple[str, int], ...] | None = ()
    # The deck's commanders; empty in caches from before tag searches, which never needed them.
    commanders: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class PeerLists:
    decks: tuple[PeerList, ...]
    # Decks the search listed but whose card list could not be read.
    skipped: int
    # Every deck the search listed in the window, kept so the next refresh pages only through new ones.
    search: SearchHits | None = None


@dataclass(slots=True, frozen=True)
class MetaData:
    commander: str
    since: date
    lists: Answer[CommanderEntries]
    field: Answer[tuple[MetaCommander, ...]]
    peers: Answer[PeerLists]
    combos: Answer[tuple["CommanderCombos", ...]]
    # The combos inside the deck itself, which the meta's answers are checked against.
    own_combos: Answer[tuple[ComboDetail, ...]]


# Reads the meta that needs TopDeck and joins it with what was started earlier.
FinishMeta = Callable[[], MetaData]


def entries_since(entries: Iterable[Entry], since: date) -> tuple[Entry, ...]:
    # A cached answer from an earlier day can reach a little further back than today's window.
    return tuple(e for e in entries if e.tournament.held_on >= since)


def listed_entries(data: MetaData) -> list[Entry]:
    """Every list with a decklist in the window, whatever it placed."""
    return [
        e for e in entries_since(data.lists.data.entries if data.lists.data else (), data.since) if e.cards
    ]


@dataclass(slots=True, frozen=True)
class CommanderCombos:
    commander: str
    combos: tuple[ComboDetail, ...]


class ItemLocks:
    """A lock for each cache item, made on first use and kept for the life of its owner."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    @contextmanager
    def hold(self, item: str) -> Generator[None]:
        with self._guard:
            lock = self._locks.setdefault(item, threading.Lock())
        with lock:
            yield


@dataclass(slots=True, frozen=True)
class MetaSources:
    tournaments: TournamentSource
    archidekt: ArchidektSearch
    spellbook: SpellbookClient
    # Spellbook asks for a few calls per interaction; the core of each top commander is one.
    spellbook_pacer: Pacer
    # The Archidekt user whose own decks never count as other players' lists.
    user: str
    settings: MetaSettings
    # Decks read side by side can want the same Archidekt search; the second waits for the first's answer.
    searches: ItemLocks = field(default_factory=ItemLocks)


def topdeck_client(
    http: httpx.Client,
    pacer_factory: Callable[[float], Pacer],
    cache_root: Path,
    keys: KeyStore | None = None,
    *,
    waits: WaitListener | None = None,
) -> TopdeckClient:
    """The TopDeck client over the cache folder's stored events, with the key from the secret store; `waits`
    hears each wait for a 429."""
    return TopdeckClient(
        Fetcher(http, slow_after=SLOW_QUERY_SECONDS),
        pacer_factory(REQUEST_INTERVAL_SECONDS),
        TournamentStore(cache_root),
        keys or KeyringKeyStore(),
        waits=waits,
    )


def meta_sources(
    http: httpx.Client,
    pacer_factory: Callable[[float], Pacer],
    user: str,
    settings: MetaSettings,
    cache_root: Path,
    *,
    waits: WaitListener | None = None,
) -> MetaSources:
    """TopDeck, Archidekt search and Spellbook clients over one HTTP client, each at its own pace."""
    return MetaSources(
        topdeck_client(http, pacer_factory, cache_root, waits=waits),
        ArchidektSearch(Fetcher(http), pacer_factory(SEARCH_INTERVAL_SECONDS)),
        SpellbookClient(Fetcher(http)),
        pacer_factory(META_REQUEST_INTERVAL_SECONDS),
        user,
        settings,
    )


def cache_stamp(cache_root: Path) -> str:
    """Changes whenever a meta answer is saved, rewritten or removed; cheap enough to poll."""
    count = newest = 0
    try:
        with os.scandir(cache_root / CACHE_FOLDER) as entries:
            for entry in entries:
                if entry.is_file():
                    count += 1
                    newest = max(newest, entry.stat().st_mtime_ns)
    except FileNotFoundError:
        return "-"
    return f"{count}:{newest}"


def utc_today(now: datetime) -> date:
    return now.astimezone(UTC).date()


def window_start(now: datetime, settings: MetaSettings) -> date:
    return utc_today(now) - timedelta(days=settings.window_days)


class _Steps:
    """Tells a caller where a read is: each step as it starts and as it ends, even when it fails."""

    def __init__(self, on_progress: Progress | None, on_done: Progress | None) -> None:
        self._on_progress = on_progress
        self._on_done = on_done

    @contextmanager
    def running(self, step: str) -> Generator[None]:
        if self._on_progress is not None:
            self._on_progress(step)
        try:
            yield
        finally:
            if self._on_done is not None:
                self._on_done(step)


def gather(
    deck: Deck,
    sources: MetaSources,
    cache_root: Path,
    *,
    now: Clock,
    refresh: bool,
    offline: bool = False,
    on_progress: Progress | None = None,
    on_done: Progress | None = None,
) -> MetaData:
    """Everything `cmdr meta` reads about the deck's commander, from cache when it is under a day old.

    offline reads only the cache, whatever its age: an older copy comes back stale, none unavailable.
    """
    return start_gather(
        deck,
        sources,
        cache_root,
        now=now,
        refresh=refresh,
        offline=offline,
        on_progress=on_progress,
        on_done=on_done,
    )()


def start_gather(
    deck: Deck,
    sources: MetaSources,
    cache_root: Path,
    *,
    now: Clock,
    refresh: bool,
    offline: bool = False,
    on_progress: Progress | None = None,
    on_done: Progress | None = None,
) -> FinishMeta:
    """Start `gather`'s reads that need no TopDeck (the Archidekt decks and the deck's own combos) in the
    background and return at once; calling what it returns reads the tournament lists, the meta field and its
    combos, which wait for TopDeck, and joins all of it."""
    if not deck.commanders:
        raise ValueError(f"{deck.name} has no commander")
    name = commander_name(deck.commanders)
    settings = sources.settings
    since = window_start(now(), settings)
    until = utc_today(now())
    folder = cache_root / CACHE_FOLDER

    steps = _Steps(on_progress, on_done)

    def lists() -> Answer[CommanderEntries]:
        with steps.running(f"TopDeck: {name} lists"):
            return _commander_lists(
                name, sources, folder, since, until, now=now, refresh=refresh, offline=offline
            )

    def field() -> Answer[tuple[MetaCommander, ...]]:
        request: Mapping[str, object] = {
            "minEventSize": settings.min_event_size,
            "commanders": settings.meta_commanders,
            "lists": settings.meta_lists,
            "windowDays": settings.window_days,
        }
        with steps.running("TopDeck: the most-played commanders"):
            return cached_answer(
                "tournament-meta",
                request,
                _live(offline, lambda: _meta_field(sources.tournaments, since, until, settings)),
                _cache(folder, "tournament meta", _encode_field, _decode_field),
                now=now,
                refresh=refresh,
                fresh_for=FRESH_FOR,
                errors=_ERRORS,
                stale=sources.tournaments.stale_note,
            )

    def peers() -> Answer[PeerLists]:
        with steps.running(f"Archidekt: recent {name} decks"):
            return _peers(deck, sources, folder, since, now=now, refresh=refresh, offline=offline)

    def own_combos() -> Answer[tuple[ComboDetail, ...]]:
        request = deck_request(deck)
        with steps.running(f"Commander Spellbook: {deck.short_name}'s own combos"):
            return cached_answer(
                cache_item("spellbook-deck", str(deck.id)),
                request,
                _live(offline, partial(_paced_details, sources, request)),
                _cache(folder, "Spellbook deck combos", _encode_combos, _decode_combos),
                now=now,
                refresh=refresh,
                fresh_for=SPELLBOOK_FRESH_FOR,
                errors=_ERRORS,
            )

    def field_and_combos() -> tuple[Answer[tuple[MetaCommander, ...]], Answer[tuple[CommanderCombos, ...]]]:
        answer = field()
        with steps.running("Commander Spellbook: the top commanders' combos"):
            return answer, _meta_combos(answer, sources, folder, now=now, refresh=refresh, offline=offline)

    # Different services, so they run side by side; each client keeps its own pace.
    peer = in_background(peers, name="meta-peers")
    deck_combos = in_background(own_combos, name="meta-combos")

    def finish() -> MetaData:
        own = in_background(lists, name="meta-lists")
        meta = in_background(field_and_combos, name="meta-field")
        field_answer, combos = meta.result()
        data = MetaData(name, since, own.result(), field_answer, peer.result(), combos, deck_combos.result())
        _prune_reads(folder, since, now=now, offline=offline)
        return data

    return finish


def _commander_lists(
    name: str,
    sources: MetaSources,
    folder: Path,
    since: date,
    until: date,
    *,
    now: Clock,
    refresh: bool,
    offline: bool,
) -> Answer[CommanderEntries]:
    settings = sources.settings
    request: Mapping[str, object] = {
        "commander": name,
        "minEventSize": settings.min_event_size,
        "windowDays": settings.window_days,
    }
    return cached_answer(
        cache_item("tournaments", name),
        request,
        _live(offline, lambda: _own_entries(sources.tournaments, name, since, until, settings)),
        _cache(folder, "tournament lists", _encode_entries, _decode_entries),
        now=now,
        refresh=refresh,
        fresh_for=FRESH_FOR,
        errors=_ERRORS,
        stale=sources.tournaments.stale_note,
    )


def gather_peers(
    deck: Deck,
    sources: MetaSources,
    cache_root: Path,
    *,
    now: Clock,
    refresh: bool,
    offline: bool = False,
    on_progress: Progress | None = None,
    on_done: Progress | None = None,
) -> MetaData:
    """The recent Archidekt decks for the commander at the deck's bracket and the one above, for decks below
    bracket 5, plus the commander's tournament lists at bracket 4. The rest of what the tournaments cover (the
    most played commanders and their combos) is left unavailable."""
    return start_gather_peers(
        deck,
        sources,
        cache_root,
        now=now,
        refresh=refresh,
        offline=offline,
        on_progress=on_progress,
        on_done=on_done,
    )()


def start_gather_peers(
    deck: Deck,
    sources: MetaSources,
    cache_root: Path,
    *,
    now: Clock,
    refresh: bool,
    offline: bool = False,
    on_progress: Progress | None = None,
    on_done: Progress | None = None,
) -> FinishMeta:
    """Start `gather_peers`'s Archidekt read in the background and return at once; calling what it returns
    reads the tournament lists (bracket 4), which wait for TopDeck, and joins both."""
    if not deck.commanders:
        raise ValueError(f"{deck.name} has no commander")
    name = commander_name(deck.commanders)
    now_utc = now()
    since = window_start(now_utc, sources.settings)
    until = utc_today(now_utc)
    folder = cache_root / CACHE_FOLDER
    read_lists = deck.bracket in TOURNAMENT_BRACKETS

    steps = _Steps(on_progress, on_done)

    def peers() -> Answer[PeerLists]:
        with steps.running(f"Archidekt: recent {name} decks"):
            return _peers(deck, sources, folder, since, now=now, refresh=refresh, offline=offline)

    def lists() -> Answer[CommanderEntries]:
        with steps.running(f"TopDeck: {name} lists"):
            return _commander_lists(
                name, sources, folder, since, until, now=now, refresh=refresh, offline=offline
            )

    peer = in_background(peers, name="meta-peers")

    def finish() -> MetaData:
        # Different services, so the two run side by side.
        own = in_background(lists, name="meta-lists") if read_lists else None
        why = "not read below bracket 5"
        tournament_lists: Answer[CommanderEntries] = (
            own.result()
            if own is not None
            else Answer(None, AnswerSource.UNAVAILABLE, None, "not read below bracket 4")
        )
        field: Answer[tuple[MetaCommander, ...]] = Answer(None, AnswerSource.UNAVAILABLE, None, why)
        combos: Answer[tuple[CommanderCombos, ...]] = Answer(None, AnswerSource.UNAVAILABLE, None, why)
        own_combos: Answer[tuple[ComboDetail, ...]] = Answer(None, AnswerSource.UNAVAILABLE, None, why)
        peers_answer = peer.result()
        _prune_reads(folder, since, now=now, offline=offline)
        return MetaData(name, since, tournament_lists, field, peers_answer, combos, own_combos)

    return finish


def gather_for(
    deck: Deck,
    sources: MetaSources,
    cache_root: Path,
    *,
    now: Clock,
    refresh: bool,
    offline: bool = False,
    on_progress: Progress | None = None,
    on_done: Progress | None = None,
) -> MetaData:
    """What other players do with the deck's commander: `gather` at bracket 5, else `gather_peers`."""
    return start_gather_for(
        deck,
        sources,
        cache_root,
        now=now,
        refresh=refresh,
        offline=offline,
        on_progress=on_progress,
        on_done=on_done,
    )()


def start_gather_for(
    deck: Deck,
    sources: MetaSources,
    cache_root: Path,
    *,
    now: Clock,
    refresh: bool,
    offline: bool = False,
    on_progress: Progress | None = None,
    on_done: Progress | None = None,
) -> FinishMeta:
    """`gather_for` in two steps, so a caller can read the other sources while TopDeck is still answering:
    the Archidekt and Spellbook reads start now, and calling what comes back waits for TopDeck's."""
    start = start_gather if deck.bracket == CEDH_BRACKET else start_gather_peers
    return start(
        deck,
        sources,
        cache_root,
        now=now,
        refresh=refresh,
        offline=offline,
        on_progress=on_progress,
        on_done=on_done,
    )


TOURNAMENTS_STEP = "TopDeck: recent tournaments"


def start_tournament_refresh(
    decks: Iterable[Deck],
    sources: MetaSources,
    *,
    now: Clock,
    on_progress: Progress | None = None,
    on_done: Progress | None = None,
) -> Future[None] | None:
    """Start fetching the days of the window TopDeck's store lacks, in the background, when any deck reads
    tournament lists; None when none does. The reads that follow find the events stored, and a failure is
    remembered by the client for them. The step is reported started before this returns, so it comes
    first among the steps of the sync."""
    if not any(deck.bracket in TOURNAMENT_BRACKETS for deck in decks):
        return None
    current = now()
    since, until = window_start(current, sources.settings), utc_today(current)
    if on_progress is not None:
        on_progress(TOURNAMENTS_STEP)

    def load() -> None:
        try:
            sources.tournaments.load(since=since, until=until)
        finally:
            if on_done is not None:
                on_done(TOURNAMENTS_STEP)

    def log_unexpected(done: Future[None]) -> None:
        # `load` keeps TopDeck's own failures; anything else would vanish in a future nobody reads.
        error = None if done.cancelled() else done.exception()
        if error is not None:
            log.warning(
                "TopDeck events for %s to %s not fetched: unexpected error", since, until, exc_info=error
            )

    future = in_background(load, name="meta-topdeck")
    future.add_done_callback(log_unexpected)
    return future


@dataclass(slots=True, frozen=True)
class TaggedDecks:
    """The recent Archidekt decks of other commanders that carry one of the deck's theme tags."""

    tag: str
    decks: tuple[PeerList, ...]


@dataclass(slots=True, frozen=True)
class AdjacentDecks:
    """The recent Archidekt decks of the commanders a profile names as playing alike, pooled (ADR 0028), and
    those of other commanders sharing the deck's theme tags, a pool per tag."""

    decks: tuple[PeerList, ...]
    # Each commander group or tag that could not be read, by name, with why.
    errors: tuple[tuple[str, str], ...]
    tagged: tuple[TaggedDecks, ...] = ()
    # Whether a commander group's decks could not be read, so `decks` pools only part of the groups.
    groups_failed: bool = False


def gather_adjacent(
    deck: Deck,
    groups: Sequence[tuple[str, ...]],
    sources: MetaSources,
    cache_root: Path,
    *,
    now: Clock,
    refresh: bool,
    offline: bool = False,
    tags: Sequence[str] = (),
    on_progress: Progress | None = None,
    on_done: Progress | None = None,
) -> AdjacentDecks:
    """Each group's recent Archidekt decks at the deck's bracket, cached as a deck's own peers are; and for
    each deck tag, those of any other commander than the deck's own."""
    since = window_start(now(), sources.settings)
    folder = cache_root / CACHE_FOLDER
    steps = _Steps(on_progress, on_done)
    decks: dict[int, PeerList] = {}
    errors: list[tuple[str, str]] = []
    groups_failed = False
    for group in groups:
        name = commander_name(group)
        with steps.running(f"Archidekt: recent {name} decks"):
            answer = _group_peers(
                group,
                deck.bracket,
                sources,
                folder,
                since,
                own_id=None,
                now=now,
                refresh=refresh,
                offline=offline,
            )
        if answer.data is None:
            errors.append((name, answer.error or "unavailable"))
            groups_failed = True
            continue
        for peer in answer.data.decks:
            decks.setdefault(peer.deck.id, peer)
    own = {name.casefold() for name in deck.commanders}
    tagged: list[TaggedDecks] = []
    for tag in _distinct_tags(tags):
        with steps.running(f"Archidekt: recent decks tagged {tag}"):
            answer = _tag_peers(
                tag, deck.bracket, sources, folder, since, now=now, refresh=refresh, offline=offline
            )
        if answer.data is None:
            errors.append((f"{tag}-tagged", answer.error or "unavailable"))
            continue
        others = tuple(p for p in answer.data.decks if own.isdisjoint(c.casefold() for c in p.commanders))
        tagged.append(TaggedDecks(tag, others))
    _prune_reads(folder, since, now=now, offline=offline)
    return AdjacentDecks(tuple(decks.values()), tuple(errors), tuple(tagged), groups_failed)


def _prune_reads(folder: Path, since: date, *, now: Clock, offline: bool) -> None:
    """Drop cached deck reads no run can use again; a cache-only run leaves the cache as it found it."""
    if offline:
        return
    _drop_retired(folder)
    window_start = datetime(since.year, since.month, since.day, tzinfo=UTC)
    removed = DeckReads(folder / DECK_READS_FOLDER).prune(now=now(), window_start=window_start)
    if removed:
        log.info("dropped %d cached Archidekt decks nothing will read again", removed)


def _drop_retired(folder: Path) -> None:
    """Delete the cached answers of the EDHTop16 provider, which no run reads again."""
    try:
        retired = list(folder.glob(f"{_RETIRED_PREFIX}*.json"))
    except OSError as error:
        log.warning("could not list the retired EDHTop16 answers in %s: %s", folder, error)
        return
    for path in retired:
        try:
            path.unlink()
        except OSError as error:
            log.warning("could not remove the retired EDHTop16 answer %s: %s", path.name, error)


def _peers(
    deck: Deck,
    sources: MetaSources,
    folder: Path,
    since: date,
    *,
    now: Clock,
    refresh: bool,
    offline: bool,
) -> Answer[PeerLists]:
    group = tuple(deck.commanders)
    return _group_peers(
        group, deck.bracket, sources, folder, since, own_id=deck.id, now=now, refresh=refresh, offline=offline
    )


def _group_peers(
    group: tuple[str, ...],
    bracket: int | None,
    sources: MetaSources,
    folder: Path,
    since: date,
    *,
    own_id: int | None,
    now: Clock,
    refresh: bool,
    offline: bool,
) -> Answer[PeerLists]:
    """Recent Archidekt decks led by the commander, or by both partners, at the bracket."""
    settings = sources.settings
    name = commander_name(group)
    request: Mapping[str, object] = {
        "commander": name,
        "brackets": list(brackets_to_read(bracket)),
        "pages": MAX_SEARCH_PAGES,
        "decks": settings.peer_decks,
        "viewFloors": list(VIEW_FLOORS),
        "minDecks": MIN_DECKS,
        "windowDays": settings.window_days,
        "user": sources.user.casefold(),
    }
    item = cache_item("archidekt", name, str(bracket))
    cache = _peer_cache(folder, "Archidekt decks")
    request_fingerprint = request_key(request)
    with sources.searches.hold(item):
        return cached_answer(
            item,
            request,
            _live(
                offline,
                lambda: _peer_lists(
                    sources,
                    DeckReads(folder / DECK_READS_FOLDER),
                    group,
                    bracket,
                    own_id=own_id,
                    since=since,
                    now=now,
                    item=item,
                    previous=_previous_search(cache, item, request_fingerprint),
                ),
            ),
            cache,
            now=now,
            refresh=refresh,
            fresh_for=peer_fresh_for(item, now()),
            errors=_ERRORS,
        )


def _distinct_tags(tags: Sequence[str]) -> list[str]:
    """Each tag once, in order, compared without case: Archidekt matches tags that way."""
    first: dict[str, str] = {}
    for tag in tags:
        first.setdefault(tag.casefold(), tag)
    return list(first.values())


def tag_reads(peer_decks: int) -> int:
    """How many decks a tag search reads: a quarter of a commander's, rounded up."""
    return -(-peer_decks // TAG_READ_DIVISOR)


def _tag_peers(
    tag: str,
    bracket: int | None,
    sources: MetaSources,
    folder: Path,
    since: date,
    *,
    now: Clock,
    refresh: bool,
    offline: bool,
) -> Answer[PeerLists]:
    """Recent Archidekt decks of any commander carrying the deck tag, at the bracket and the one above.

    Cached by tag and bracket alone, so every deck wanting the tag shares it; the caller leaves out its own
    commander's decks.
    """
    settings = sources.settings
    key = tag.casefold()
    request: Mapping[str, object] = {
        "tag": key,
        "brackets": list(brackets_to_read(bracket)),
        "pages": TAG_SEARCH_PAGES_PER_BRACKET,
        "decks": tag_reads(settings.peer_decks),
        "viewFloors": list(VIEW_FLOORS),
        "minDecks": MIN_DECKS,
        "windowDays": settings.window_days,
        "user": sources.user.casefold(),
    }
    item = cache_item("archidekt-tag", key, str(bracket))
    cache = _peer_cache(folder, "Archidekt tagged decks")
    request_fingerprint = request_key(request)
    with sources.searches.hold(item):
        return cached_answer(
            item,
            request,
            _live(
                offline,
                lambda: _tag_peer_lists(
                    sources,
                    DeckReads(folder / DECK_READS_FOLDER),
                    tag,
                    bracket,
                    since=since,
                    now=now,
                    item=item,
                    previous=_previous_search(cache, item, request_fingerprint),
                ),
            ),
            cache,
            now=now,
            refresh=refresh,
            fresh_for=peer_fresh_for(item, now()),
            errors=_ERRORS,
        )


def core_request(commander: MetaCommander) -> dict[str, object] | None:
    """The commander and the cards at least COMBO_CORE_SHARE of its lists play, as a Spellbook request."""
    lists = [e for e in commander.entries if e.cards]
    if not lists:
        return None
    counts = Counter(name for e in lists for name in {card.name for card in e.cards})
    names = commander.stats.name.split(" / ")
    core = sorted(n for n, k in counts.items() if k / len(lists) >= COMBO_CORE_SHARE and n not in names)
    return {
        "commanders": [{"card": n, "quantity": 1} for n in names],
        "main": [{"card": n, "quantity": 1} for n in core],
    }


def _meta_combos(
    field: Answer[tuple[MetaCommander, ...]],
    sources: MetaSources,
    folder: Path,
    *,
    now: Clock,
    refresh: bool,
    offline: bool,
) -> Answer[tuple[CommanderCombos, ...]]:
    if field.data is None:
        return Answer(None, AnswerSource.UNAVAILABLE, None, field.error or "no meta lists")
    cache = _cache(folder, "Spellbook meta combos", _encode_combos, _decode_combos)
    answers: list[tuple[str, Answer[tuple[ComboDetail, ...]]]] = []
    for commander in field.data:
        request = core_request(commander)
        if request is None:
            continue
        answer = cached_answer(
            cache_item("spellbook", commander.stats.name),
            request,
            _live(offline, partial(_paced_details, sources, request)),
            cache,
            now=now,
            refresh=refresh,
            fresh_for=SPELLBOOK_FRESH_FOR,
            errors=_ERRORS,
        )
        answers.append((commander.stats.name, answer))
    return _combined(answers)


def _paced_details(sources: MetaSources, request: Mapping[str, object]) -> tuple[ComboDetail, ...]:
    sources.spellbook_pacer.wait()
    return sources.spellbook.combo_details(request)


def _combined(
    answers: Sequence[tuple[str, Answer[tuple[ComboDetail, ...]]]],
) -> Answer[tuple[CommanderCombos, ...]]:
    """One answer for every commander's combos: the worst source among them, and the oldest time."""
    found = tuple(CommanderCombos(name, a.data) for name, a in answers if a.data is not None)
    error = "; ".join(sorted({a.error for _, a in answers if a.error})) or None
    if not found:
        return Answer(None, AnswerSource.UNAVAILABLE, None, error or "no meta lists to look in")
    times = [a.fetched_at for _, a in answers if a.fetched_at is not None]
    oldest = min(times) if times else None
    seen = {a.source for _, a in answers}
    if seen & {AnswerSource.STALE, AnswerSource.UNAVAILABLE}:
        return Answer(found, AnswerSource.STALE, oldest, error)
    if seen == {AnswerSource.CACHED}:
        return Answer(found, AnswerSource.CACHED, oldest, None)
    return Answer(found, AnswerSource.FETCHED, oldest, None)


def _live[T](offline: bool, fetch: Callable[[], T]) -> Callable[[Mapping[str, object]], T]:  # noqa: FBT001 — mirrors gather's flag
    def run(_: Mapping[str, object]) -> T:
        if offline:
            raise NotFetchedError(NOT_FETCHED)
        return fetch()

    return run


def _own_entries(
    source: TournamentSource, name: str, since: date, until: date, settings: MetaSettings
) -> CommanderEntries:
    found = source.entries(
        name,
        since=since,
        until=until,
        min_event_size=settings.min_event_size,
        sort=EntrySort.NEW,
        limit=MAX_OWN_ENTRIES,
    )
    return CommanderEntries(name, found is not None, found or ())


def _meta_field(
    source: TournamentSource, since: date, until: date, settings: MetaSettings
) -> tuple[MetaCommander, ...]:
    if settings.meta_commanders == 0:
        return ()
    ranked = source.top_commanders(
        since=since, until=until, min_event_size=settings.min_event_size, count=settings.meta_commanders
    )
    field: list[MetaCommander] = []
    for stats in ranked:
        entries = source.entries(
            stats.name,
            since=since,
            until=until,
            min_event_size=settings.min_event_size,
            sort=EntrySort.TOP,
            limit=settings.meta_lists,
        )
        field.append(MetaCommander(stats, entries or ()))
    return tuple(field)


def _peer_lists(
    sources: MetaSources,
    reads: DeckReads,
    group: tuple[str, ...],
    bracket: int | None,
    *,
    own_id: int | None,
    since: date,
    now: Clock,
    item: str,
    previous: SearchHits | None,
) -> PeerLists:
    settings = sources.settings
    if settings.peer_decks == 0:
        return PeerLists((), 0)
    brackets = brackets_to_read(bracket)
    start = _start_of(since)

    def search(stop_at: Mapping[int | None, datetime] | None) -> list[PeerDeck]:
        return sources.archidekt.recent_decks(group[0], brackets=brackets, since=start, stop_at=stop_at)

    hits = update_hits(previous, search, item=item, brackets=brackets, since=start, now=now())
    wanted = {name.casefold() for name in group}
    # The search finds one commander: decks pairing it with a partner or Background are read and dropped, and
    # so are decks led by only one of a pair.
    peers = _read_peers(
        sources,
        reads,
        hits.decks,
        settings.peer_decks,
        own_id=own_id,
        now=now,
        keep=lambda read: wanted == {name.casefold() for name in read.commanders},
    )
    return replace(peers, search=hits)


def _tag_peer_lists(
    sources: MetaSources,
    reads: DeckReads,
    tag: str,
    bracket: int | None,
    *,
    since: date,
    now: Clock,
    item: str,
    previous: SearchHits | None,
) -> PeerLists:
    limit = tag_reads(sources.settings.peer_decks)
    if limit == 0:
        return PeerLists((), 0)
    brackets = brackets_to_read(bracket)
    start = _start_of(since)

    def search(stop_at: Mapping[int | None, datetime] | None) -> list[PeerDeck]:
        found: dict[int, PeerDeck] = {}
        # One search per bracket, so a busy tag at the first cannot use up the pages of the next.
        for each in brackets:
            for peer in sources.archidekt.recent_decks(
                None,
                brackets=(each,),
                since=start,
                tag=tag,
                max_pages=TAG_SEARCH_PAGES_PER_BRACKET,
                stop_at=stop_at,
            ):
                found.setdefault(peer.id, peer)
        return list(found.values())

    hits = update_hits(previous, search, item=item, brackets=brackets, since=start, now=now())
    peers = _read_peers(sources, reads, hits.decks, limit, own_id=None, now=now, keep=lambda _: True)
    return replace(peers, search=hits)


def _start_of(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _read_peers(
    sources: MetaSources,
    reads: DeckReads,
    found: Sequence[PeerDeck],
    limit: int,
    *,
    own_id: int | None,
    now: Clock,
    keep: Callable[[DeckRead], bool],
) -> PeerLists:
    """The most-viewed other players' decks of a search, read or cached, and filtered by keep."""
    user = sources.user.casefold()
    others = [d for d in found if d.owner.casefold() != user and d.id != own_id]
    lists: list[PeerList] = []
    skipped = 0
    for peer in most_viewed(others, limit=limit):
        read = reads.load(peer, now())
        if read is None:
            try:
                read = read_of(sources.archidekt.deck(peer.id))
            except ArchidektError as error:
                # One unreadable deck shouldn't cost the others; the count is shown with the results.
                log.warning("skipping Archidekt deck %d: %s", peer.id, error)
                skipped += 1
                continue
            except HttpStatusError as error:
                if error.status not in _DECK_GONE_STATUSES:
                    raise
                log.warning("skipping Archidekt deck %d: %s", peer.id, error)
                skipped += 1
                continue
            _keep(reads, peer, read, now())
        if keep(read):
            lists.append(PeerList(peer, read.cards, read.planned, read.multiples, read.commanders))
    return PeerLists(tuple(lists), skipped)


def _keep(reads: DeckReads, peer: PeerDeck, read: DeckRead, at: datetime) -> None:
    try:
        reads.save(peer, read, at)
    except OSError as error:
        log.warning("could not cache Archidekt deck %d: %s", peer.id, error)


def _cache[T](
    folder: Path, what: str, encode: Callable[[T], object], decode: Callable[[object], T]
) -> DeckCache[T]:
    return DeckCache(folder, version=CACHE_VERSION, what=what, encode=encode, decode=decode)


def _peer_cache(folder: Path, what: str) -> DeckCache[PeerLists]:
    # An answer from before the search hits has none: it serves as it is, and its refresh searches in full.
    return _cache(folder, what, _encode_peers, _decode_peers)


def _previous_search(cache: DeckCache[PeerLists], item: str, key: str) -> SearchHits | None:
    """The search hits stored for this very request, whatever the age of the answer holding them."""
    stored = cache.load(item, key)
    return stored.data.search if stored is not None else None


def _encode_entry(entry: Entry) -> object:
    t = entry.tournament
    return {
        "standing": entry.standing,
        "tournament": {
            "tid": t.tid,
            "date": t.held_on.isoformat(),
            "size": t.size,
            "topCut": t.top_cut,
        },
        "cards": [[card.name, card.count] for card in entry.cards],
    }


def _decode_entry(raw: object, where: str) -> Entry:
    obj = as_object(raw, where)
    t = req_object(obj, "tournament", where)
    cards: list[DecklistCard] = []
    for index, item in enumerate(req_list(obj, "cards", where)):
        name, count = as_list(item, f"{where}.cards[{index}]")
        if not isinstance(name, str) or not isinstance(count, int):
            raise ShapeError(f"{where}.cards[{index}]: expected [name, count]")
        cards.append(DecklistCard(name, count))
    return Entry(
        standing=req_int(obj, "standing", where),
        tournament=Tournament(
            tid=req_str(t, "tid", where),
            held_on=date.fromisoformat(req_str(t, "date", where)),
            size=req_int(t, "size", where),
            top_cut=req_int(t, "topCut", where),
        ),
        cards=tuple(cards),
    )


def _encode_entries(data: CommanderEntries) -> object:
    return {
        "commander": data.commander,
        "known": data.known,
        "entries": [_encode_entry(e) for e in data.entries],
    }


def _decode_entries(raw: object) -> CommanderEntries:
    obj = as_object(raw, "answer")
    return CommanderEntries(
        commander=req_str(obj, "commander", "answer"),
        known=req_bool(obj, "known", "answer"),
        entries=_decode_entry_list(req_list(obj, "entries", "answer"), "answer.entries"),
    )


def _decode_entry_list(items: Sequence[object], where: str) -> tuple[Entry, ...]:
    return tuple(_decode_entry(item, f"{where}[{index}]") for index, item in enumerate(items))


def _encode_field(field: tuple[MetaCommander, ...]) -> object:
    return [
        {
            "name": c.stats.name,
            "entries": c.stats.entries,
            "topCuts": c.stats.top_cuts,
            "conversion": c.stats.conversion,
            "metaShare": c.stats.meta_share,
            "lists": [_encode_entry(e) for e in c.entries],
        }
        for c in field
    ]


def _decode_field(raw: object) -> tuple[MetaCommander, ...]:
    field: list[MetaCommander] = []
    for index, item in enumerate(as_list(raw, "answer")):
        where = f"answer[{index}]"
        obj = as_object(item, where)
        stats = CommanderStats(
            name=req_str(obj, "name", where),
            entries=req_int(obj, "entries", where),
            top_cuts=req_int(obj, "topCuts", where),
            conversion=req_float(obj, "conversion", where),
            meta_share=req_float(obj, "metaShare", where),
        )
        field.append(
            MetaCommander(stats, _decode_entry_list(req_list(obj, "lists", where), f"{where}.lists"))
        )
    return tuple(field)


def _encode_peer_deck(deck: PeerDeck) -> dict[str, object]:
    return {
        "id": deck.id,
        "name": deck.name,
        "owner": deck.owner,
        "views": deck.views,
        "updatedAt": deck.updated_at.isoformat(),
        "bracket": deck.bracket,
    }


def _decode_peer_deck(d: Mapping[str, object], where: str) -> PeerDeck:
    updated_at = datetime.fromisoformat(req_str(d, "updatedAt", where))
    if updated_at.tzinfo is None:
        raise ShapeError(f"{where}.updatedAt: no time zone")
    return PeerDeck(
        id=req_int(d, "id", where),
        name=req_str(d, "name", where),
        owner=req_str(d, "owner", where),
        views=req_int(d, "views", where),
        updated_at=updated_at,
        bracket=opt_int(d, "bracket", where),
    )


def _encode_peers(peers: PeerLists) -> object:
    encoded: dict[str, object] = {
        "skipped": peers.skipped,
        "decks": [
            {
                **_encode_peer_deck(p.deck),
                "cards": list(p.cards),
                "planned": list(p.planned),
                "multiples": None if p.multiples is None else [[name, count] for name, count in p.multiples],
                "commanders": list(p.commanders),
            }
            for p in peers.decks
        ],
    }
    if peers.search is not None:
        encoded["search"] = {
            "fullAt": peers.search.full_at.isoformat(),
            "decks": [_encode_peer_deck(deck) for deck in peers.search.decks],
        }
    return encoded


def _decode_peers(raw: object) -> PeerLists:
    obj = as_object(raw, "answer")
    decks: list[PeerList] = []
    for index, item in enumerate(req_list(obj, "decks", "answer")):
        where = f"answer.decks[{index}]"
        d = as_object(item, where)
        cards = as_list(d.get("cards"), f"{where}.cards")
        # Caches from before ADR 0017 have no "planned"; they read as none until their next refresh.
        planned = as_list(d.get("planned", []), f"{where}.planned")
        if not all(isinstance(card, str) for card in (*cards, *planned)):
            raise ShapeError(f"{where}.cards: expected names")
        decks.append(
            PeerList(
                _decode_peer_deck(d, where),
                tuple(str(card) for card in cards),
                tuple(str(card) for card in planned),
                _multiples(d, where),
                str_list(d, "commanders", where) if "commanders" in d else (),
            )
        )
    return PeerLists(tuple(decks), req_int(obj, "skipped", "answer"), _decode_search(obj))


def _decode_search(obj: Mapping[str, object]) -> SearchHits | None:
    """The stored search hits; an answer of a search that was never made (no decks to read) has none."""
    if "search" not in obj:
        return None
    where = "answer.search"
    search = req_object(obj, "search", "answer")
    hits = tuple(
        _decode_peer_deck(as_object(item, f"{where}.decks[{index}]"), f"{where}.decks[{index}]")
        for index, item in enumerate(req_list(search, "decks", where))
    )
    full_at = datetime.fromisoformat(req_str(search, "fullAt", where))
    if full_at.tzinfo is None:
        raise ShapeError(f"{where}.fullAt: no time zone")
    return SearchHits(hits, full_at)


def _multiples(d: Mapping[str, object], where: str) -> tuple[tuple[str, int], ...] | None:
    raw = d.get("multiples")
    return None if raw is None else decode_multiples(raw, f"{where}.multiples")


def _encode_combos(combos: tuple[ComboDetail, ...]) -> object:
    return [
        {
            "id": c.id,
            "pieces": [{"name": p.name, "type": p.type_line, "zones": list(p.zones)} for p in c.pieces],
            "requirements": list(c.requirements),
            "results": list(c.results),
            "description": c.description,
        }
        for c in combos
    ]


def _decode_combos(raw: object) -> tuple[ComboDetail, ...]:
    combos: list[ComboDetail] = []
    for index, item in enumerate(as_list(raw, "answer")):
        where = f"answer[{index}]"
        obj = as_object(item, where)
        pieces: list[ComboPiece] = []
        for number, piece in enumerate(req_list(obj, "pieces", where)):
            at = f"{where}.pieces[{number}]"
            fields = as_object(piece, at)
            pieces.append(
                ComboPiece(
                    req_str(fields, "name", at), req_str(fields, "type", at), str_list(fields, "zones", at)
                )
            )
        combos.append(
            ComboDetail(
                id=req_str(obj, "id", where),
                pieces=tuple(pieces),
                requirements=str_list(obj, "requirements", where),
                results=str_list(obj, "results", where),
                description=req_str(obj, "description", where),
            )
        )
    return tuple(combos)
