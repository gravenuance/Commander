"""TopDeck.gg tournaments kept in the cache folder, one file each, so every event is fetched once.

Each file holds an event with its decks stripped to the commander, the finish and the cards: no player's name
or id is ever stored. `coverage.json` records which span of start dates is complete, so a run asks for the
days it has not seen. The folder is cache only: a version it does not know is dropped and fetched again.
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from commander.deckcache import cache_item
from commander.fsutil import write_text_atomic
from commander.jsonshape import ShapeError, as_list, as_object, req_int, req_list, req_str, str_list
from commander.tournaments import DecklistCard, Entry, Tournament

log = logging.getLogger(__name__)

# 2: events of 16+ players; a store filled at 64+ is fetched again, since its coverage misses smaller events.
STORE_VERSION = 2
FOLDER = "topdeck"
_EVENTS = "events"
_COVERAGE = "coverage.json"


@dataclass(slots=True, frozen=True)
class Coverage:
    """Every event that started from `first` to `last` (unix seconds, inclusive) is in the store."""

    first: int
    last: int


@dataclass(slots=True, frozen=True)
class DeckEntry:
    commander: str
    standing: int
    cards: tuple[DecklistCard, ...]


@dataclass(slots=True, frozen=True)
class TournamentRecord:
    tournament: Tournament
    decks: tuple[DeckEntry, ...]

    def entries(self, commander: str) -> list[Entry]:
        """The decks led by the commander (compared without case), as entries of this event."""
        wanted = commander.casefold()
        return [
            Entry(deck.standing, self.tournament, deck.cards)
            for deck in self.decks
            if deck.commander.casefold() == wanted
        ]


class TournamentStore:
    def __init__(self, root: Path) -> None:
        self._folder = root / FOLDER

    def coverage(self) -> Coverage | None:
        path = self._folder / _COVERAGE
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            obj = as_object(raw, "coverage")
            if obj.get("version") != STORE_VERSION:
                return None
            return Coverage(req_int(obj, "first", "coverage"), req_int(obj, "last", "coverage"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ShapeError) as error:
            log.warning("ignoring unreadable TopDeck coverage %s: %s", path, error)
            return None

    def set_coverage(self, coverage: Coverage) -> None:
        document = {"version": STORE_VERSION, "first": coverage.first, "last": coverage.last}
        write_text_atomic(self._folder / _COVERAGE, _dump(document))

    def save(self, record: TournamentRecord) -> None:
        write_text_atomic(self._event_path(record.tournament.tid), _dump(_encode(record)))

    def load_window(self, since: date, until: date) -> list[TournamentRecord]:
        """The stored events held from `since` to `until`, both inclusive; unreadable files are skipped."""
        records: list[TournamentRecord] = []
        for path in self._event_files():
            record = self._read(path)
            if record is not None and since <= record.tournament.held_on <= until:
                records.append(record)
        return records

    def prune(self, before: date) -> int:
        """Delete the events held before the date; how many went."""
        removed = 0
        for path in self._event_files():
            record = self._read(path)
            if record is not None and record.tournament.held_on >= before:
                continue
            try:
                path.unlink()
            except OSError as error:
                log.warning("could not remove the cached TopDeck event %s: %s", path.name, error)
                continue
            removed += 1
        return removed

    def _event_path(self, tid: str) -> Path:
        return self._folder / _EVENTS / f"{cache_item(tid)}.json"

    def _event_files(self) -> list[Path]:
        try:
            return sorted((self._folder / _EVENTS).glob("*.json"))
        except OSError as error:
            log.warning("could not list the cached TopDeck events: %s", error)
            return []

    @staticmethod
    def _read(path: Path) -> TournamentRecord | None:
        try:
            return _decode(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ShapeError, ValueError) as error:
            log.warning("ignoring unreadable cached TopDeck event %s: %s", path.name, error)
            return None


def _dump(document: object) -> str:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"


def _encode(record: TournamentRecord) -> object:
    """Card names once per event, each deck a flat run of name index and count."""
    names: dict[str, int] = {}
    decks: list[object] = []
    for deck in record.decks:
        flat: list[int] = []
        for card in deck.cards:
            flat.extend((names.setdefault(card.name, len(names)), card.count))
        decks.append({"c": deck.commander, "s": deck.standing, "k": flat})
    t = record.tournament
    return {
        "version": STORE_VERSION,
        "tid": t.tid,
        "date": t.held_on.isoformat(),
        "size": t.size,
        "topCut": t.top_cut,
        "names": list(names),
        "decks": decks,
    }


def _decode(raw: object) -> TournamentRecord | None:
    obj = as_object(raw, "event")
    if obj.get("version") != STORE_VERSION:
        return None
    names = str_list(obj, "names", "event")
    decks: list[DeckEntry] = []
    for index, item in enumerate(req_list(obj, "decks", "event")):
        where = f"event.decks[{index}]"
        deck = as_object(item, where)
        decks.append(
            DeckEntry(req_str(deck, "c", where), req_int(deck, "s", where), _cards(deck, names, where))
        )
    tournament = Tournament(
        tid=req_str(obj, "tid", "event"),
        held_on=date.fromisoformat(req_str(obj, "date", "event")),
        size=req_int(obj, "size", "event"),
        top_cut=req_int(obj, "topCut", "event"),
    )
    return TournamentRecord(tournament, tuple(decks))


def _cards(deck: object, names: Sequence[str], where: str) -> tuple[DecklistCard, ...]:
    flat = as_list(as_object(deck, where).get("k"), f"{where}.k")
    if len(flat) % 2:
        raise ShapeError(f"{where}.k: expected pairs")
    cards: list[DecklistCard] = []
    for position in range(0, len(flat), 2):
        index, count = flat[position], flat[position + 1]
        if not isinstance(index, int) or not isinstance(count, int) or not 0 <= index < len(names):
            raise ShapeError(f"{where}.k[{position}]: expected a name index and a count")
        cards.append(DecklistCard(names[index], count))
    return tuple(cards)
