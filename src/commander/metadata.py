"""What other players run with a commander, gathered for `cmdr meta`.

Three sources, each cached for a day in the cache folder (never the repo, as EDHTop16's terms forbid
redistributing its statistics) and fetched in parallel, each at its own service's pace:

- the commander's EDHTop16 entries with decklists,
- the most-played commanders and their best-placed EDHTop16 lists,
- the most-viewed recent Archidekt decks for the commander, other players' only.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from commander.archidekt import ArchidektError, ArchidektSearch, PeerDeck
from commander.background import in_background
from commander.config import MetaSettings
from commander.deckcache import Answer, Clock, DeckCache, cache_item, cached_answer
from commander.edhtop16 import (
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
)
from commander.models import Deck
from commander.net import NetworkError

log = logging.getLogger(__name__)

FRESH_FOR = timedelta(hours=24)
CACHE_VERSION = 1
CACHE_FOLDER = "meta"
# Enough for the busiest commanders' two months; the client stops at its page cap before this anyway.
MAX_OWN_ENTRIES = 400
_ERRORS = (NetworkError, Edhtop16Error, ArchidektError)

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


@dataclass(slots=True, frozen=True)
class MetaSources:
    edhtop16: Edhtop16Client
    archidekt: ArchidektSearch
    # The Archidekt user whose own decks never count as other players' lists.
    user: str
    settings: MetaSettings


def window_start(now: datetime, settings: MetaSettings) -> date:
    return now.astimezone(UTC).date() - timedelta(days=settings.window_days)


def gather(
    deck: Deck,
    sources: MetaSources,
    cache_root: Path,
    *,
    now: Clock,
    refresh: bool,
    on_progress: Progress | None = None,
) -> MetaData:
    """Everything `cmdr meta` reads about the deck's commander, from cache when it is under a day old."""
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
            lambda _: _own_entries(sources.edhtop16, name, since, settings),
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
            lambda _: _meta_field(sources.edhtop16, since, settings),
            _cache(folder, "EDHTop16 meta", _encode_field, _decode_field),
            now=now,
            refresh=refresh,
            fresh_for=FRESH_FOR,
            errors=_ERRORS,
        )

    def peers() -> Answer[PeerLists]:
        report(f"Archidekt: recent {name} decks")
        request: Mapping[str, object] = {
            "commander": deck.commanders[0],
            "bracket": deck.bracket,
            "decks": settings.peer_decks,
            "user": sources.user.casefold(),
        }
        return cached_answer(
            cache_item("archidekt", deck.commanders[0], str(deck.bracket)),
            request,
            lambda _: _peer_lists(sources, deck, since, settings),
            _cache(folder, "Archidekt decks", _encode_peers, _decode_peers),
            now=now,
            refresh=refresh,
            fresh_for=FRESH_FOR,
            errors=_ERRORS,
        )

    # Different services, so they run side by side; each client keeps its own pace.
    own = in_background(lists, name="meta-lists")
    meta = in_background(field, name="meta-field")
    peer = in_background(peers, name="meta-peers")
    return MetaData(name, since, own.result(), meta.result(), peer.result())


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


def _peer_lists(sources: MetaSources, deck: Deck, since: date, settings: MetaSettings) -> PeerLists:
    if settings.peer_decks == 0:
        return PeerLists((), 0)
    start = datetime(since.year, since.month, since.day, tzinfo=UTC)
    user = sources.user.casefold()
    found = sources.archidekt.recent_decks(deck.commanders[0], bracket=deck.bracket, since=start)
    others = [d for d in found if d.owner.casefold() != user and d.id != deck.id]
    others.sort(key=lambda d: (-d.views, d.id))
    lists: list[PeerList] = []
    skipped = 0
    for peer in others[: settings.peer_decks]:
        try:
            full = sources.archidekt.deck(peer.id)
        except ArchidektError as error:
            # One unreadable deck shouldn't cost the others; the count is shown with the results.
            log.warning("skipping Archidekt deck %d: %s", peer.id, error)
            skipped += 1
            continue
        lists.append(PeerList(peer, tuple(sorted({card.name for card in full.main_cards}))))
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
        if not all(isinstance(card, str) for card in cards):
            raise ShapeError(f"{where}.cards: expected names")
        peer = PeerDeck(
            id=req_int(d, "id", where),
            name=req_str(d, "name", where),
            owner=req_str(d, "owner", where),
            views=req_int(d, "views", where),
            updated_at=datetime.fromisoformat(req_str(d, "updatedAt", where)),
            bracket=opt_int(d, "bracket", where),
        )
        decks.append(PeerList(peer, tuple(str(card) for card in cards)))
    return PeerLists(tuple(decks), req_int(obj, "skipped", "answer"))
