"""What other players run with a commander, gathered for `cmdr meta`.

Three sources, each cached for a day in the cache folder (never the repo, as EDHTop16's terms forbid
redistributing its statistics) and fetched in parallel, each at its own service's pace:

- the commander's EDHTop16 entries with decklists,
- the most-played commanders and their best-placed EDHTop16 lists,
- the most-viewed recent Archidekt decks for the commander, other players' only,
- the combos inside each top commander's core cards, from Commander Spellbook (cached for a week);
- the deck's own combos, from Commander Spellbook (cached for a week, until the list changes).
"""

import logging
import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path

import httpx

from commander.archidekt import SEARCH_INTERVAL_SECONDS, ArchidektError, ArchidektSearch, PeerDeck
from commander.background import in_background
from commander.config import MetaSettings
from commander.deckcache import Answer, AnswerSource, Clock, DeckCache, cache_item, cached_answer
from commander.edhtop16 import (
    REQUEST_INTERVAL_SECONDS,
    SLOW_QUERY_SECONDS,
    CommanderStats,
    DecklistCard,
    Edhtop16Client,
    Edhtop16Error,
    Entry,
    EntrySort,
    Tournament,
    commander_name,
)
from commander.jsonshape import (
    ShapeError,
    as_list,
    as_object,
    opt_int,
    opt_str,
    req_bool,
    req_float,
    req_int,
    req_list,
    req_object,
    req_str,
    str_list,
)
from commander.models import Deck, plan_category
from commander.net import Fetcher, NetworkError, Pacer
from commander.spellbook import FRESH_FOR as SPELLBOOK_FRESH_FOR
from commander.spellbook import (
    META_REQUEST_INTERVAL_SECONDS,
    ComboDetail,
    ComboPiece,
    SpellbookClient,
    SpellbookError,
    deck_request,
)

log = logging.getLogger(__name__)

FRESH_FOR = timedelta(hours=24)
CACHE_VERSION = 1
CACHE_FOLDER = "meta"
# The cEDH bracket: the only one EDHTop16 tournaments cover.
CEDH_BRACKET = 5
# Search hits read per wanted deck when looking for a partner pair, most of which pair another partner.
PARTNER_READS = 3
# Enough for the busiest commanders' two months; the client stops at its page cap before this anyway.
MAX_OWN_ENTRIES = 400


class NotFetchedError(Exception):
    """Raised in place of a request when gathering may only read the cache."""


_ERRORS = (NetworkError, Edhtop16Error, ArchidektError, SpellbookError, NotFetchedError)
# A card in this share of a commander's lists is part of its core, where its combos are looked for.
COMBO_CORE_SHARE = 0.2

Progress = Callable[[str], None]


@dataclass(slots=True, frozen=True)
class CommanderEntries:
    commander: str
    # False when EDHTop16 has no commander by that name, which differs from one with no recent entries.
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


@dataclass(slots=True, frozen=True)
class PeerLists:
    decks: tuple[PeerList, ...]
    # Decks the search listed but whose card list could not be read.
    skipped: int


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


@dataclass(slots=True, frozen=True)
class CommanderCombos:
    commander: str
    combos: tuple[ComboDetail, ...]


@dataclass(slots=True, frozen=True)
class MetaSources:
    edhtop16: Edhtop16Client
    archidekt: ArchidektSearch
    spellbook: SpellbookClient
    # Spellbook asks for a few calls per interaction; the core of each top commander is one.
    spellbook_pacer: Pacer
    # The Archidekt user whose own decks never count as other players' lists.
    user: str
    settings: MetaSettings


def meta_sources(
    http: httpx.Client, pacer_factory: Callable[[float], Pacer], user: str, settings: MetaSettings
) -> MetaSources:
    """EDHTop16, Archidekt search and Spellbook clients over one HTTP client, each at its own pace."""
    return MetaSources(
        Edhtop16Client(Fetcher(http, slow_after=SLOW_QUERY_SECONDS), pacer_factory(REQUEST_INTERVAL_SECONDS)),
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


def window_start(now: datetime, settings: MetaSettings) -> date:
    return now.astimezone(UTC).date() - timedelta(days=settings.window_days)


def gather(
    deck: Deck,
    sources: MetaSources,
    cache_root: Path,
    *,
    now: Clock,
    refresh: bool,
    offline: bool = False,
    on_progress: Progress | None = None,
) -> MetaData:
    """Everything `cmdr meta` reads about the deck's commander, from cache when it is under a day old.

    offline reads only the cache, whatever its age: an older copy comes back stale, none unavailable.
    """
    if not deck.commanders:
        raise ValueError(f"{deck.name} has no commander")
    name = commander_name(deck.commanders)
    settings = sources.settings
    since = window_start(now(), settings)
    folder = cache_root / CACHE_FOLDER

    def report(step: str) -> None:
        if on_progress is not None:
            on_progress(step)

    def lists() -> Answer[CommanderEntries]:
        report(f"EDHTop16: {name} lists")
        request: Mapping[str, object] = {"commander": name, "minEventSize": settings.min_event_size}
        return cached_answer(
            cache_item("edhtop16", name),
            request,
            _live(offline, lambda: _own_entries(sources.edhtop16, name, since, settings)),
            _cache(folder, "EDHTop16 lists", _encode_entries, _decode_entries),
            now=now,
            refresh=refresh,
            fresh_for=FRESH_FOR,
            errors=_ERRORS,
        )

    def field() -> Answer[tuple[MetaCommander, ...]]:
        report("EDHTop16: the most-played commanders")
        request: Mapping[str, object] = {
            "minEventSize": settings.min_event_size,
            "commanders": settings.meta_commanders,
            "lists": settings.meta_lists,
            "windowDays": settings.window_days,
        }
        return cached_answer(
            "edhtop16-meta",
            request,
            _live(offline, lambda: _meta_field(sources.edhtop16, since, settings)),
            _cache(folder, "EDHTop16 meta", _encode_field, _decode_field),
            now=now,
            refresh=refresh,
            fresh_for=FRESH_FOR,
            errors=_ERRORS,
        )

    def peers() -> Answer[PeerLists]:
        report(f"Archidekt: recent {name} decks")
        return _peers(deck, sources, folder, since, now=now, refresh=refresh, offline=offline)

    # Different services, so they run side by side; each client keeps its own pace.
    def own_combos() -> Answer[tuple[ComboDetail, ...]]:
        report(f"Commander Spellbook: {deck.short_name}'s own combos")
        request = deck_request(deck)
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

    def field_and_combos() -> tuple[
        Answer[tuple[MetaCommander, ...]],
        Answer[tuple[CommanderCombos, ...]],
        Answer[tuple[ComboDetail, ...]],
    ]:
        answer = field()
        report("Commander Spellbook: the top commanders' combos")
        combos = _meta_combos(answer, sources, folder, now=now, refresh=refresh, offline=offline)
        # After the meta's: both go through the one Spellbook pacer.
        return answer, combos, own_combos()

    own = in_background(lists, name="meta-lists")
    meta = in_background(field_and_combos, name="meta-field")
    peer = in_background(peers, name="meta-peers")
    field_answer, combos, deck_combos = meta.result()
    return MetaData(name, since, own.result(), field_answer, peer.result(), combos, deck_combos)


def gather_peers(
    deck: Deck,
    sources: MetaSources,
    cache_root: Path,
    *,
    now: Clock,
    refresh: bool,
    offline: bool = False,
    on_progress: Progress | None = None,
) -> MetaData:
    """Only the recent Archidekt decks for the commander at the deck's bracket, for decks below bracket 5:
    EDHTop16 covers cEDH alone, so its parts are left unavailable."""
    if not deck.commanders:
        raise ValueError(f"{deck.name} has no commander")
    if on_progress is not None:
        on_progress(f"Archidekt: recent {commander_name(deck.commanders)} decks")
    since = window_start(now(), sources.settings)
    folder = cache_root / CACHE_FOLDER
    peers = _peers(deck, sources, folder, since, now=now, refresh=refresh, offline=offline)
    why = "not read below bracket 5"
    lists: Answer[CommanderEntries] = Answer(None, AnswerSource.UNAVAILABLE, None, why)
    field: Answer[tuple[MetaCommander, ...]] = Answer(None, AnswerSource.UNAVAILABLE, None, why)
    combos: Answer[tuple[CommanderCombos, ...]] = Answer(None, AnswerSource.UNAVAILABLE, None, why)
    own: Answer[tuple[ComboDetail, ...]] = Answer(None, AnswerSource.UNAVAILABLE, None, why)
    return MetaData(commander_name(deck.commanders), since, lists, field, peers, combos, own)


def gather_for(
    deck: Deck,
    sources: MetaSources,
    cache_root: Path,
    *,
    now: Clock,
    refresh: bool,
    offline: bool = False,
    on_progress: Progress | None = None,
) -> MetaData:
    """What other players do with the deck's commander: `gather` at bracket 5, else `gather_peers`."""
    read = gather if deck.bracket == CEDH_BRACKET else gather_peers
    return read(deck, sources, cache_root, now=now, refresh=refresh, offline=offline, on_progress=on_progress)


@dataclass(slots=True, frozen=True)
class AdjacentDecks:
    """The recent Archidekt decks of the commanders a profile names as playing alike, pooled (ADR 0015)."""

    decks: tuple[PeerList, ...]
    # Each commander group that could not be read, by name, with why.
    errors: tuple[tuple[str, str], ...]


def gather_adjacent(
    deck: Deck,
    groups: Sequence[tuple[str, ...]],
    sources: MetaSources,
    cache_root: Path,
    *,
    now: Clock,
    refresh: bool,
    offline: bool = False,
    on_progress: Progress | None = None,
) -> AdjacentDecks:
    """Each group's recent Archidekt decks at the deck's bracket, cached as a deck's own peers are."""
    since = window_start(now(), sources.settings)
    folder = cache_root / CACHE_FOLDER
    decks: dict[int, PeerList] = {}
    errors: list[tuple[str, str]] = []
    for group in groups:
        name = commander_name(group)
        if on_progress is not None:
            on_progress(f"Archidekt: recent {name} decks")
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
            continue
        for peer in answer.data.decks:
            decks.setdefault(peer.deck.id, peer)
    return AdjacentDecks(tuple(decks.values()), tuple(errors))


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
        "bracket": bracket,
        "decks": settings.peer_decks,
        "user": sources.user.casefold(),
    }
    return cached_answer(
        cache_item("archidekt", name, str(bracket)),
        request,
        _live(
            offline,
            lambda: _peer_lists(sources, group, bracket, own_id=own_id, since=since, settings=settings),
        ),
        _cache(folder, "Archidekt decks", _encode_peers, _decode_peers),
        now=now,
        refresh=refresh,
        fresh_for=FRESH_FOR,
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
            raise NotFetchedError("not fetched yet")
        return fetch()

    return run


def _own_entries(client: Edhtop16Client, name: str, since: date, settings: MetaSettings) -> CommanderEntries:
    found = client.entries(
        name, since=since, min_event_size=settings.min_event_size, sort=EntrySort.NEW, limit=MAX_OWN_ENTRIES
    )
    return CommanderEntries(name, found is not None, found or ())


def _meta_field(client: Edhtop16Client, since: date, settings: MetaSettings) -> tuple[MetaCommander, ...]:
    if settings.meta_commanders == 0:
        return ()
    ranked = client.top_commanders(
        since=since, min_event_size=settings.min_event_size, count=settings.meta_commanders
    )
    field: list[MetaCommander] = []
    for stats in ranked:
        entries = client.entries(
            stats.name,
            since=since,
            min_event_size=settings.min_event_size,
            sort=EntrySort.TOP,
            limit=settings.meta_lists,
        )
        field.append(MetaCommander(stats, entries or ()))
    return tuple(field)


def _peer_lists(
    sources: MetaSources,
    group: tuple[str, ...],
    bracket: int | None,
    *,
    own_id: int | None,
    since: date,
    settings: MetaSettings,
) -> PeerLists:
    if settings.peer_decks == 0:
        return PeerLists((), 0)
    start = datetime(since.year, since.month, since.day, tzinfo=UTC)
    user = sources.user.casefold()
    found = sources.archidekt.recent_decks(group[0], bracket=bracket, since=start)
    others = [d for d in found if d.owner.casefold() != user and d.id != own_id]
    others.sort(key=lambda d: (-d.views, d.id))
    wanted = {name.casefold() for name in group}
    # The search finds one commander; with a partner, decks pairing it with another are read and dropped.
    reads = settings.peer_decks * (PARTNER_READS if len(group) > 1 else 1)
    lists: list[PeerList] = []
    skipped = 0
    for peer in others[:reads]:
        if len(lists) == settings.peer_decks:
            break
        try:
            full = sources.archidekt.deck(peer.id)
        except ArchidektError as error:
            # One unreadable deck shouldn't cost the others; the count is shown with the results.
            log.warning("skipping Archidekt deck %d: %s", peer.id, error)
            skipped += 1
            continue
        if len(group) > 1 and not wanted <= {name.casefold() for name in full.commanders}:
            continue
        names = sorted({card.name for card in full.main_cards})
        planned = sorted({card.name for card in full.main_cards if any(map(plan_category, card.categories))})
        counts: dict[str, int] = {}
        for card in full.main_cards:
            counts[card.name] = counts.get(card.name, 0) + card.quantity
        multiples = tuple(sorted((name, count) for name, count in counts.items() if count > 1))
        lists.append(PeerList(peer, tuple(names), tuple(planned), multiples))
    return PeerLists(tuple(lists), skipped)


def _cache[T](
    folder: Path, what: str, encode: Callable[[T], object], decode: Callable[[object], T]
) -> DeckCache[T]:
    return DeckCache(folder, version=CACHE_VERSION, what=what, encode=encode, decode=decode)


def _encode_entry(entry: Entry) -> object:
    t = entry.tournament
    return {
        "standing": entry.standing,
        "wins": entry.wins,
        "losses": entry.losses,
        "draws": entry.draws,
        "url": entry.decklist_url,
        "tournament": {
            "tid": t.tid,
            "name": t.name,
            "date": t.held_on.isoformat(),
            "size": t.size,
            "topCut": t.top_cut,
        },
        "cards": [[card.name, card.count, card.spice] for card in entry.cards],
    }


def _decode_entry(raw: object, where: str) -> Entry:
    obj = as_object(raw, where)
    t = req_object(obj, "tournament", where)
    cards: list[DecklistCard] = []
    for index, item in enumerate(req_list(obj, "cards", where)):
        name, count, spice = as_list(item, f"{where}.cards[{index}]")
        if not isinstance(name, str) or not isinstance(count, int) or not isinstance(spice, int):
            raise ShapeError(f"{where}.cards[{index}]: expected [name, count, spice]")
        cards.append(DecklistCard(name, count, spice))
    return Entry(
        standing=req_int(obj, "standing", where),
        wins=req_int(obj, "wins", where),
        losses=req_int(obj, "losses", where),
        draws=req_int(obj, "draws", where),
        decklist_url=opt_str(obj, "url", where),
        tournament=Tournament(
            tid=req_str(t, "tid", where),
            name=req_str(t, "name", where),
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


def _encode_peers(peers: PeerLists) -> object:
    return {
        "skipped": peers.skipped,
        "decks": [
            {
                "id": p.deck.id,
                "name": p.deck.name,
                "owner": p.deck.owner,
                "views": p.deck.views,
                "updatedAt": p.deck.updated_at.isoformat(),
                "bracket": p.deck.bracket,
                "cards": list(p.cards),
                "planned": list(p.planned),
                "multiples": None if p.multiples is None else [[name, count] for name, count in p.multiples],
            }
            for p in peers.decks
        ],
    }


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
        peer = PeerDeck(
            id=req_int(d, "id", where),
            name=req_str(d, "name", where),
            owner=req_str(d, "owner", where),
            views=req_int(d, "views", where),
            updated_at=datetime.fromisoformat(req_str(d, "updatedAt", where)),
            bracket=opt_int(d, "bracket", where),
        )
        decks.append(
            PeerList(
                peer,
                tuple(str(card) for card in cards),
                tuple(str(card) for card in planned),
                _multiples(d, where),
            )
        )
    return PeerLists(tuple(decks), req_int(obj, "skipped", "answer"))


def _multiples(d: Mapping[str, object], where: str) -> tuple[tuple[str, int], ...] | None:
    raw = d.get("multiples")
    if raw is None:
        return None
    found: list[tuple[str, int]] = []
    for index, item in enumerate(as_list(raw, f"{where}.multiples")):
        pair = as_list(item, f"{where}.multiples[{index}]")
        if len(pair) != 2 or not isinstance(pair[0], str) or not isinstance(pair[1], int):  # noqa: PLR2004 — a pair
            raise ShapeError(f"{where}.multiples[{index}]: expected [name, count]")
        found.append((pair[0], pair[1]))
    return tuple(found)


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
