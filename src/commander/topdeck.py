"""TopDeck.gg's tournament API: Commander events with their decklists, and the lists and shares of the
commanders played, worked out here from the standings.

Its terms (read 2026-10-21) make the API free with a key per user, 100 requests a minute and fewer for bulk
queries, and ask for a visible credit with a link to TopDeck.gg wherever the data is shown. Events are fetched
once, by start date, and kept in the cache folder (`topdeckstore.py`); player names and ids are dropped as the
answer is read and never stored.
"""

import logging
import threading
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

from commander.config import MIN_EVENT_SIZE
from commander.credentials import CredentialError, KeyStore
from commander.deckcache import ServiceKeyError, StaleNote
from commander.jsonshape import (
    ShapeError,
    as_list,
    as_object,
    opt_int,
    req_int,
    req_list,
    req_object,
    req_str,
)
from commander.net import Fetcher, HttpStatusError, NetworkError, Pacer, ResponseTooLargeError, send_paced
from commander.topdeckstore import Coverage, DeckEntry, TournamentRecord, TournamentStore
from commander.tournaments import (
    CommanderStats,
    DecklistCard,
    Entry,
    EntrySort,
    Tournament,
    commander_name,
)

log = logging.getLogger(__name__)

API_URL = "https://topdeck.gg/api/v2/tournaments"
SITE_URL = "https://topdeck.gg"
DEVELOPERS_URL = "https://topdeck.gg/developers"
CREDIT = "Tournament data from TopDeck.gg"
GAME = "Magic: The Gathering"
FORMAT = "EDH"
# Only the decklists are asked for: no player names or ids, and no results the tool does not read.
COLUMNS = ("decklist",)
# Events with fewer players say little about a list, and are not fetched at all.
PARTICIPANT_MIN = MIN_EVENT_SIZE
# The documented 100 requests a minute does not hold for bulk queries: on 2026-10-21 TopDeck answered 429
# after five of them in a row, about five a minute. Requests start twelve seconds apart, start to start,
# as the answers take one to eight seconds to arrive.
REQUEST_INTERVAL_SECONDS = 12.0
# A week of Commander events of 16+ players is about 13.5 MB (2026-10-21); the HTTP client caps an answer
# at 20. A chunk over the cap is asked for again in halves.
CHUNK_DAYS = 7
# Events that started in the last days may still be playing, so those days are asked for again next time.
GRACE_DAYS = 3
SLOW_QUERY_SECONDS = 30.0
# A Commander deck has about a hundred cards; far more is not a deck.
MAX_DECK_CARDS = 400
MAX_TID_LENGTH = 200
KEY_REMEDY = "add one in the app's TopDeck panel or with `cmdr topdeck`"
NO_KEY_MESSAGE = f"no TopDeck key saved; {KEY_REMEDY}"
_DAY = 24 * 60 * 60
# A key check asks for events no real one reaches, so the answer is empty.
_KEY_CHECK_PARTICIPANTS = 100_000


class TopdeckError(Exception):
    """TopDeck answered with something this tool cannot use."""


class TopdeckKeyError(TopdeckError, ServiceKeyError):
    """There is no key, or TopDeck refused it."""


@dataclass(slots=True, frozen=True)
class _Window:
    since: date
    until: date


class TopdeckClient:
    """The tournament source: fetches the events a window lacks, then answers from the stored ones."""

    def __init__(self, fetcher: Fetcher, pacer: Pacer, store: TournamentStore, keys: KeyStore) -> None:
        self._fetcher = fetcher
        self._pacer = pacer
        self._store = store
        self._keys = keys
        # Lists and the meta field read side by side: one fetches, the other waits and reads what it stored.
        self._lock = threading.Lock()
        self._loaded: tuple[_Window, list[TournamentRecord], StaleNote | None] | None = None
        # The first key or network failure: a sync reads once per deck, so later reads give up unsent.
        self._failure: TopdeckKeyError | NetworkError | None = None

    def check_key(self, key: str) -> None:
        """Ask TopDeck for events no one reached; TopdeckKeyError when it refuses the key."""
        body = {"game": GAME, "format": FORMAT, "last": 1, "participantMin": _KEY_CHECK_PARTICIPANTS}
        self._post(body, key)

    def entries(
        self, commander: str, *, since: date, until: date, min_event_size: int, sort: EntrySort, limit: int
    ) -> tuple[Entry, ...] | None:
        found = [
            entry
            for record in self._events(since, until, min_event_size)
            for entry in record.entries(commander)
        ]
        if not found:
            return None
        if sort is EntrySort.TOP:
            found.sort(key=lambda e: (e.placing, -e.tournament.held_on.toordinal(), e.tournament.tid))
        else:
            found.sort(key=lambda e: (-e.tournament.held_on.toordinal(), e.tournament.tid, e.standing))
        return tuple(found[:limit])

    def stale_note(self) -> StaleNote | None:
        """Set when the window last read was answered from the store because fetching the rest failed."""
        with self._lock:
            return None if self._loaded is None else self._loaded[2]

    def load(self, *, since: date, until: date) -> None:
        try:
            self._events(since, until, 0)
        except (TopdeckError, NetworkError) as error:
            log.warning("TopDeck events for %s to %s not fetched: %s", since, until, error)

    def top_commanders(
        self, *, since: date, until: date, min_event_size: int, count: int
    ) -> tuple[CommanderStats, ...]:
        played: Counter[str] = Counter()
        cut: Counter[str] = Counter()
        names: dict[str, str] = {}
        for record in self._events(since, until, min_event_size):
            for deck in record.decks:
                key = deck.commander.casefold()
                names.setdefault(key, deck.commander)
                played[key] += 1
                if 0 < deck.standing <= record.tournament.top_cut:
                    cut[key] += 1
        total = sum(played.values())
        ranked = sorted(played, key=lambda key: (-played[key], names[key]))[:count]
        return tuple(
            CommanderStats(
                name=names[key],
                entries=played[key],
                top_cuts=cut[key],
                conversion=cut[key] / played[key],
                meta_share=played[key] / total,
            )
            for key in ranked
        )

    def _events(self, since: date, until: date, min_event_size: int) -> list[TournamentRecord]:
        """The stored events in the window with enough players, after fetching the days the store lacks."""
        window = _Window(since, until)
        with self._lock:
            if self._loaded is None or self._loaded[0] != window:
                note = self._refresh(window)
                self._loaded = (window, self._store.load_window(since, until), note)
            events = self._loaded[1]
        return [e for e in events if e.tournament.size >= min_event_size]

    def _refresh(self, window: _Window) -> StaleNote | None:
        """Fetch what the store lacks; if that fails and an earlier run stored part of the window, say why the
        stored events stand in. Without stored events the error goes to the caller."""
        before = self._store.coverage()
        try:
            if self._failure is not None:
                raise self._failure
            self._ensure(window)
        except (TopdeckError, NetworkError) as error:
            if isinstance(error, TopdeckKeyError | NetworkError):
                self._failure = error
            if before is None or not _overlaps(before, window):
                raise
            log.warning("TopDeck refresh failed, answering from the stored events: %s", error)
            reached = self._store.coverage() or before
            return StaleNote(
                str(error), datetime.fromtimestamp(reached.last, UTC), isinstance(error, ServiceKeyError)
            )
        return None

    def _ensure(self, window: _Window) -> None:
        """Fetch the days of the window the store has not seen, oldest first, saving as each week arrives."""
        need_first = _start_of(window.since)
        need_last = _start_of(window.until) + _DAY - 1
        settled = need_last - GRACE_DAYS * _DAY
        coverage = self._store.coverage()
        if coverage is not None and coverage.first <= need_first <= coverage.last + 1:
            first, covered = max(coverage.first, need_first), coverage.last
        else:
            first, covered = need_first, need_first - 1
        cursor = covered + 1
        if cursor > settled:
            return
        while cursor <= need_last:
            end = min(cursor + CHUNK_DAYS * _DAY - 1, need_last)
            for record in self._fetch_fitting(cursor, end):
                self._store.save(record)
            self._loaded = None
            covered = max(covered, min(end, settled))
            self._store.set_coverage(Coverage(first, covered))
            cursor = end + 1
        removed = self._store.prune(window.since)
        if removed:
            log.info("dropped %d cached TopDeck events older than %s", removed, window.since)

    def _fetch_fitting(self, start: int, end: int) -> list[TournamentRecord]:
        """The events started from `start` to `end`. An answer over the size cap is asked for again in two
        halves, down to one day; a day that is still too big is skipped, so it cannot block the coverage."""
        try:
            return self._fetch(start, end)
        except ResponseTooLargeError:
            days = -(-(end - start + 1) // _DAY)
            if days <= 1:
                log.warning(
                    "TopDeck's answer for %s is over the size cap even for one day; skipping it", _day(start)
                )
                return []
            log.info(
                "TopDeck's answer for %s to %s is over the size cap; asking in halves", _day(start), _day(end)
            )
            middle = start + days // 2 * _DAY
            return [*self._fetch_fitting(start, middle - 1), *self._fetch_fitting(middle, end)]

    def _fetch(self, start: int, end: int) -> list[TournamentRecord]:
        body: dict[str, object] = {
            "game": GAME,
            "format": FORMAT,
            "start": start,
            "end": end,
            "participantMin": PARTICIPANT_MIN,
            "columns": list(COLUMNS),
        }
        try:
            key = self._keys.get()
        except CredentialError as error:
            raise TopdeckKeyError(str(error)) from error
        if not key:
            raise TopdeckKeyError(NO_KEY_MESSAGE)
        raw = self._post(body, key)
        try:
            records = parse_tournaments(raw)
        except ShapeError as error:
            raise TopdeckError(f"unexpected answer from TopDeck: {error}") from error
        log.info("TopDeck: %d events from %s to %s", len(records), _day(start), _day(end))
        return records

    def _post(self, body: Mapping[str, object], key: str) -> object:
        try:
            return send_paced(
                self._pacer,
                lambda: self._fetcher.post_json(
                    API_URL, body, headers={"Authorization": key}, surface_rate_limit=True
                ),
                what="TopDeck",
            )
        except HttpStatusError as error:
            if error.status in (401, 403):
                raise TopdeckKeyError(
                    f"TopDeck refused the key (HTTP {error.status}); check it in the TopDeck panel"
                ) from error
            raise


def _overlaps(coverage: Coverage, window: _Window) -> bool:
    return coverage.first < _start_of(window.until) + _DAY and coverage.last >= _start_of(window.since)


def _start_of(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


def _day(stamp: int) -> date:
    return datetime.fromtimestamp(stamp, UTC).date()


def parse_tournaments(raw: object) -> list[TournamentRecord]:
    """The events of an answer, as stored; a malformed event is skipped and logged."""
    records: list[TournamentRecord] = []
    for index, item in enumerate(as_list(raw, "answer")):
        where = f"answer[{index}]"
        try:
            records.append(parse_tournament(as_object(item, where), where))
        except (ValueError, OverflowError, OSError) as error:  # ShapeError, or a date out of range
            log.warning("Skipping malformed TopDeck event #%d: %s", index, error)
    return records


def parse_tournament(obj: Mapping[str, object], where: str) -> TournamentRecord:
    tid = req_str(obj, "TID", where)
    if not tid or len(tid) > MAX_TID_LENGTH:
        raise ShapeError(f"{where}.TID: expected 1 to {MAX_TID_LENGTH} characters")
    held_on = datetime.fromtimestamp(req_int(obj, "startDate", where), UTC).date()
    standings = req_list(obj, "standings", where)
    decks: list[DeckEntry] = []
    malformed = 0
    for place, item in enumerate(standings, start=1):
        try:
            deck = _deck(item, place, f"{where}.standings[{place - 1}]")
        except ShapeError:
            malformed += 1
            continue
        if deck is not None:
            decks.append(deck)
    if malformed:
        log.warning("Skipped %d malformed TopDeck decks in event %s", malformed, tid[:80])
    tournament = Tournament(tid, held_on, len(standings), opt_int(obj, "topCut", where) or 0)
    return TournamentRecord(tournament, tuple(decks))


def _deck(item: object, place: int, where: str) -> DeckEntry | None:
    """The deck of a standing, with its place in the order; None when the player gave no structured list.

    Only the commander and the cards are read: the player's name and id never leave this function.
    """
    standing = as_object(item, where)
    structured = standing.get("deckObj")
    if structured is None:
        return None
    deck = as_object(structured, f"{where}.deckObj")
    commanders = sorted(req_object(deck, "Commanders", f"{where}.deckObj"))
    if not commanders:
        return None
    main = req_object(deck, "Mainboard", f"{where}.deckObj")
    if len(main) > MAX_DECK_CARDS:
        raise ShapeError(f"{where}.deckObj.Mainboard: {len(main)} cards")
    cards: list[DecklistCard] = []
    for name, entry in main.items():
        count = req_int(as_object(entry, f"{where}.deckObj.Mainboard"), "count", f"{where}.deckObj.Mainboard")
        if count >= 1:
            cards.append(DecklistCard(name, count))
    return DeckEntry(commander_name(commanders), place, tuple(cards))
