"""Answers from an online service about a deck, cached per deck and reused while the cards are unchanged."""

import hashlib
import json
import logging
import os
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

from commander.fsutil import write_text_atomic
from commander.jsonshape import ShapeError, as_object, req_str

log = logging.getLogger(__name__)

# Cache entries named by text instead of a deck id must be safe as a file name on their own.
_SAFE_ITEM = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")

Clock = Callable[[], datetime]

NOT_FETCHED = "not fetched yet"


class NotFetchedError(Exception):
    """Raised in place of a request when gathering may only read the cache."""


class ServiceKeyError(Exception):
    """A service needs a key that is missing, or that it refused; answers built from it are marked."""


class _FetchFailures:
    """Why the last fetch of each cache entry failed, kept in memory for the run. A cache-only read, which is
    all the app's panels do, then names that reason instead of "not fetched yet"."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reasons: dict[tuple[Path, int | str], str] = {}

    def record(self, entry: tuple[Path, int | str], reason: str) -> None:
        with self._lock:
            self._reasons[entry] = reason

    def forget(self, entry: tuple[Path, int | str]) -> None:
        with self._lock:
            self._reasons.pop(entry, None)

    def reason(self, entry: tuple[Path, int | str]) -> str | None:
        with self._lock:
            return self._reasons.get(entry)


_failures = _FetchFailures()


class AnswerSource(Enum):
    FETCHED = "fetched"
    CACHED = "cached"
    # The service failed, so an older answer for the same cards stands in.
    STALE = "stale"
    UNAVAILABLE = "unavailable"


@dataclass(slots=True, frozen=True)
class StaleNote:
    """A refresh failed and stored data answered instead: why, how far the stored data reaches, and whether
    the failure was a missing or refused key."""

    reason: str
    as_of: datetime
    key_problem: bool


@dataclass(slots=True, frozen=True)
class Answer[T]:
    data: T | None
    source: AnswerSource
    fetched_at: datetime | None
    error: str | None
    # The error is a missing or refused key, which the user fixes once for every deck.
    key_problem: bool = False

    def __post_init__(self) -> None:
        if (self.data is None) != (self.source is AnswerSource.UNAVAILABLE):
            raise ValueError("an answer is missing exactly when the source is unavailable")


@dataclass(slots=True, frozen=True)
class Accepted:
    """Another stored answer that serves a request when none is stored under its own: the item it is kept
    in, and the request it was stored for."""

    item: int | str
    request: Mapping[str, object]


@dataclass(slots=True, frozen=True)
class CachedAnswer[T]:
    data: T
    fetched_at: datetime


class DeckCache[T]:
    """One JSON file per deck (or per named item); unreadable files, other versions and other requests count
    as missing."""

    def __init__(
        self,
        folder: Path,
        *,
        version: int,
        what: str,
        encode: Callable[[T], object],
        decode: Callable[[object], T],
    ) -> None:
        self._folder = folder
        self._version = version
        self.what = what
        self._encode = encode
        self._decode = decode

    def entry(self, item: int | str) -> tuple[Path, int | str]:
        """Names one cached item across cache objects made for the same folder."""
        return self._folder, item

    def load(self, item: int | str, key: str) -> CachedAnswer[T] | None:
        path = self._path(item)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            log.warning("ignoring unreadable cached %s %s: %s", self.what, path, error)
            return None
        try:
            obj = as_object(raw, "cache")
            if obj.get("version") != self._version or obj.get("key") != key:
                return None
            fetched_at = datetime.fromisoformat(req_str(obj, "fetchedAt", "cache"))
            data = self._decode(obj.get("answer"))
        except (ShapeError, ValueError) as error:
            log.warning("ignoring malformed cached %s %s: %s", self.what, path, error)
            return None
        if fetched_at.tzinfo is None:
            log.warning("ignoring cached %s %s: its time has no time zone", self.what, path)
            return None
        return CachedAnswer(data, fetched_at)

    def save(self, item: int | str, key: str, data: T, fetched_at: datetime) -> None:
        document = {
            "version": self._version,
            "key": key,
            "fetchedAt": fetched_at.isoformat(),
            "answer": self._encode(data),
        }
        write_text_atomic(self._path(item), json.dumps(document, ensure_ascii=False, indent=1) + "\n")

    def touch(self, item: int | str, at: datetime) -> None:
        """Set the entry's file time to `at`, which is when it was last used."""
        stamp = at.timestamp()
        try:
            os.utime(self._path(item), (stamp, stamp))
        except OSError as error:
            log.warning("could not mark the cached %s %s as used: %s", self.what, _label(item), error)

    def _path(self, item: int | str) -> Path:
        if isinstance(item, str) and not _SAFE_ITEM.fullmatch(item):
            raise ValueError(f"cache item {item!r} is not a safe file name")
        return self._folder / f"{item}.json"


def request_key(request: Mapping[str, object]) -> str:
    """A fingerprint of the request, so a cached answer is only reused for the same cards."""
    canonical = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cache_item(*parts: str) -> str:
    """A safe cache item name for text such as a commander's name: a readable slug plus a short hash."""
    text = " ".join(parts)
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")[:60] or "item"
    return f"{slug}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}"


def cached_answer[T](
    item: int | str,
    request: Mapping[str, object],
    fetch: Callable[[Mapping[str, object]], T],
    cache: DeckCache[T],
    *,
    now: Clock,
    refresh: bool,
    fresh_for: timedelta,
    errors: tuple[type[Exception], ...],
    stale: Callable[[], StaleNote | None] | None = None,
    also_accept: Sequence[Accepted] = (),
) -> Answer[T]:
    """A recent cached answer, else a fresh one; if fetching fails with one of errors, the last good one.

    `stale` is asked after a fetch that returned: a note means stored data answered because refreshing failed,
    so the answer is stale and not cached as fresh. An answer stored as one of the `also_accept` ones counts
    as an answer to this one too, fresh or as the stale fallback, and the newest of them all stands (this
    request's own on a tie); a fresh answer is stored under its own item and request, so it never replaces
    an accepted one."""
    key = request_key(request)
    found = [cache.load(item, key), *(cache.load(a.item, request_key(a.request)) for a in also_accept)]
    cached = max((c for c in found if c is not None), key=lambda c: c.fetched_at, default=None)
    # A time ahead of the clock (a wrong clock, a hand-edited file) cannot be trusted as recent.
    if cached is not None and not refresh and timedelta(0) <= now() - cached.fetched_at < fresh_for:
        return Answer(cached.data, AnswerSource.CACHED, cached.fetched_at, None)
    entry = cache.entry(item)
    try:
        data = fetch(request)
    except errors as error:
        reason = str(error)
        if isinstance(error, NotFetchedError):
            # A cache miss on a cache-only read is routine and repeats on every panel refresh.
            reason = _failures.reason(entry) or reason
            log.debug("%s for %s not read from the cache: %s", cache.what, _label(item), reason)
        else:
            log.info("%s for %s failed: %s", cache.what, _label(item), reason)
            _failures.record(entry, reason)
        key_problem = isinstance(error, ServiceKeyError)
        if cached is not None:
            return Answer(cached.data, AnswerSource.STALE, cached.fetched_at, reason, key_problem)
        return Answer(None, AnswerSource.UNAVAILABLE, None, reason, key_problem)
    note = stale() if stale is not None else None
    if note is not None:
        log.info("%s for %s answered from stored data: %s", cache.what, _label(item), note.reason)
        return Answer(data, AnswerSource.STALE, note.as_of, note.reason, note.key_problem)
    _failures.forget(entry)
    fetched_at = now()
    try:
        cache.save(item, key, data, fetched_at)
    except OSError as error:
        log.warning("could not cache the %s for %s: %s", cache.what, _label(item), error)
    return Answer(data, AnswerSource.FETCHED, fetched_at, None)


def _label(item: int | str) -> str:
    return f"deck {item}" if isinstance(item, int) else item
