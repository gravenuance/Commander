"""Other players' Archidekt decks, read once and kept for as long as they are unchanged.

A deck is cached under its id and the `updatedAt` the search listed it with, so the next run reads again
only the decks their owners have edited since. Entries live in the meta cache folder, which is not the repo.
A file's time is when it was last used (written or hit), and `prune` drops what no run can use any more.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from commander.archidekt import PeerDeck
from commander.deckcache import DeckCache, request_key
from commander.jsonshape import ShapeError, as_list, as_object, req_list, req_str, str_list
from commander.models import Deck, plan_category

log = logging.getLogger(__name__)

CACHE_VERSION = 2
FOLDER = "peer-decks"
# A deck neither read nor hit for this long is dropped: no recent search lists it.
UNUSED_DAYS = 30


@dataclass(slots=True, frozen=True)
class DeckRead:
    """What the meta keeps of another player's deck: its commanders and its main deck's cards."""

    commanders: tuple[str, ...]
    cards: tuple[str, ...]
    # The cards its owner files under a category naming the plan, like "Combo Pieces" (ADR 0017).
    planned: tuple[str, ...]
    # Cards played more than once (basic lands mostly), with their count (ADR 0019).
    multiples: tuple[tuple[str, int], ...]


def read_of(deck: Deck) -> DeckRead:
    counts: dict[str, int] = {}
    for card in deck.main_cards:
        counts[card.name] = counts.get(card.name, 0) + card.quantity
    return DeckRead(
        commanders=deck.commanders,
        cards=tuple(sorted({card.name for card in deck.main_cards})),
        planned=tuple(
            sorted({card.name for card in deck.main_cards if any(map(plan_category, card.categories))})
        ),
        multiples=tuple(sorted((name, count) for name, count in counts.items() if count > 1)),
    )


@dataclass(slots=True, frozen=True)
class _Stored:
    """A read with the `updatedAt` it was listed under, which `prune` needs to tell it is out of reach."""

    read: DeckRead
    updated_at: datetime


class DeckReads:
    """The cache of `DeckRead`s in a folder; a missing, unreadable or outdated entry is a miss."""

    def __init__(self, folder: Path) -> None:
        self._folder = folder
        self._cache = DeckCache(
            folder, version=CACHE_VERSION, what="Archidekt deck", encode=_encode, decode=_decode
        )

    def load(self, peer: PeerDeck, at: datetime) -> DeckRead | None:
        """The cached read, which counts as used at `at`."""
        cached = self._cache.load(peer.id, _key(peer))
        if cached is None:
            return None
        self._cache.touch(peer.id, at)
        return cached.data.read

    def save(self, peer: PeerDeck, read: DeckRead, at: datetime) -> None:
        self._cache.save(peer.id, _key(peer), _Stored(read, peer.updated_at), at)
        self._cache.touch(peer.id, at)

    def prune(self, *, now: datetime, window_start: datetime) -> int:
        """Delete the files unused for UNUSED_DAYS, those of decks last updated before window_start (no search
        lists them again) and those that cannot be parsed; how many went."""
        unused_before = (now - timedelta(days=UNUSED_DAYS)).timestamp()
        removed = 0
        for path in self._folder.glob("*.json"):
            if not _prunable(path, unused_before, window_start):
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as error:
                log.warning("could not remove the cached Archidekt deck %s: %s", path, error)
                continue
            removed += 1
        return removed


def _prunable(path: Path, unused_before: float, window_start: datetime) -> bool:
    try:
        if path.stat().st_mtime < unused_before:
            return True
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except OSError as error:
        # Locked or unreadable for now: not proof that it is useless.
        log.warning("could not read the cached Archidekt deck %s: %s", path, error)
        return False
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        log.info("dropping unparseable cached Archidekt deck %s: %s", path, error)
        return True
    updated_at = _updated_at(raw, path)
    return updated_at is None or updated_at < window_start


def _updated_at(raw: object, path: Path) -> datetime | None:
    """The `updatedAt` a current-version cache file holds; None when it is another version or malformed."""
    try:
        obj = as_object(raw, "cache")
        if obj.get("version") != CACHE_VERSION:
            return None
        updated_at = datetime.fromisoformat(
            req_str(as_object(obj.get("answer"), "answer"), "updatedAt", "answer")
        )
    except (ShapeError, ValueError) as error:
        log.info("dropping unparseable cached Archidekt deck %s: %s", path, error)
        return None
    return updated_at if updated_at.tzinfo is not None else None


def _key(peer: PeerDeck) -> str:
    return request_key({"updatedAt": peer.updated_at.isoformat()})


def _encode(stored: _Stored) -> object:
    read = stored.read
    return {
        "updatedAt": stored.updated_at.isoformat(),
        "commanders": list(read.commanders),
        "cards": list(read.cards),
        "planned": list(read.planned),
        "multiples": [[name, count] for name, count in read.multiples],
    }


def _decode(raw: object) -> _Stored:
    obj = as_object(raw, "answer")
    read = DeckRead(
        commanders=str_list(obj, "commanders", "answer"),
        cards=str_list(obj, "cards", "answer"),
        planned=str_list(obj, "planned", "answer"),
        multiples=decode_multiples(req_list(obj, "multiples", "answer"), "answer.multiples"),
    )
    return _Stored(read, datetime.fromisoformat(req_str(obj, "updatedAt", "answer")))


def decode_multiples(raw: object, where: str) -> tuple[tuple[str, int], ...]:
    """[[name, count], ...] as a deck's cards played more than once are stored."""
    found: list[tuple[str, int]] = []
    for index, item in enumerate(as_list(raw, where)):
        pair = as_list(item, f"{where}[{index}]")
        if len(pair) != 2 or not isinstance(pair[0], str) or not isinstance(pair[1], int):  # noqa: PLR2004 — a pair
            raise ShapeError(f"{where}[{index}]: expected [name, count]")
        found.append((pair[0], pair[1]))
    return tuple(found)
