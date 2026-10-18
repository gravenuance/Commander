"""How often the user opens each deck, kept on this machine to order the sync (ADR 0034).

A deck's score gains 1 each time it is opened and halves every 30 days, so what was opened lately
outweighs what was opened often long ago. The file holds one score and the time it was last updated per
deck, and the score at any later moment follows from those two.
"""

import json
import logging
import math
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from commander.deckcache import Clock
from commander.fsutil import write_text_atomic
from commander.jsonshape import ShapeError, as_object, req_float, req_int, req_str

log = logging.getLogger(__name__)

VIEWS_FILE = "views.json"
FORMAT_VERSION = 1
HALF_LIFE_DAYS = 30.0
# A score this low has no say in the order; it is dropped when the file is next written.
NEGLIGIBLE_SCORE = 0.01
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_SECONDS_PER_DAY = 86_400


@dataclass(slots=True, frozen=True)
class _Entry:
    score: float
    at: datetime

    def score_at(self, now: datetime) -> float:
        """The score `now`; a clock set back counts as no time passed, so a score never grows by itself."""
        age_days = max(0.0, (now - self.at).total_seconds() / _SECONDS_PER_DAY)
        return self.score * 0.5 ** (age_days / HALF_LIFE_DAYS)


class ViewScores:
    """Every deck's view score, read from and written to one small JSON file."""

    def __init__(self, path: Path, now: Clock) -> None:
        self._path = path
        self._now = now
        # The app's bridge thread writes while the sync thread reads.
        self._lock = threading.Lock()

    def scores(self) -> dict[int, float]:
        """Each viewed deck's score now; a deck never opened is absent, which counts as zero."""
        with self._lock:
            now = self._now()
            return {deck_id: entry.score_at(now) for deck_id, entry in self._read().items()}

    def record_open(self, deck_id: int) -> None:
        """Decay the deck's score to now, then add 1. A file that cannot be saved is logged, not raised."""
        with self._lock:
            now = self._now().astimezone(UTC)
            entries = self._read()
            previous = entries.get(deck_id)
            entries[deck_id] = _Entry((previous.score_at(now) if previous else 0.0) + 1.0, now)
            kept = {i: e for i, e in entries.items() if i == deck_id or e.score_at(now) >= NEGLIGIBLE_SCORE}
            try:
                write_text_atomic(self._path, _dump(kept))
            except OSError as error:
                log.warning("could not save the view scores to %s: %s", self._path, error)

    def _read(self) -> dict[int, _Entry]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError) as error:
            log.warning(
                "could not read the view scores in %s: %s; every deck counts as unviewed", self._path, error
            )
            return {}
        try:
            return _parse(text)
        except (json.JSONDecodeError, ShapeError, ValueError) as error:
            log.warning(
                "the view scores in %s are damaged (%s); every deck counts as unviewed", self._path, error
            )
            return {}


def _parse(text: str) -> dict[int, _Entry]:
    root = as_object(json.loads(text), VIEWS_FILE)
    version = req_int(root, "version", VIEWS_FILE)
    if version != FORMAT_VERSION:
        raise ShapeError(f"format version {version}, this build reads {FORMAT_VERSION}")
    entries: dict[int, _Entry] = {}
    for key, raw in as_object(root.get("decks"), "decks").items():
        where = f"decks.{key}"
        fields = as_object(raw, where)
        score = req_float(fields, "score", where)
        if not math.isfinite(score) or score < 0:
            raise ShapeError(f"{where}.score: expected zero or more")
        stamp = datetime.fromisoformat(req_str(fields, "at", where))
        if stamp.tzinfo is None:
            raise ShapeError(f"{where}.at: expected a time with a zone")
        entries[int(key)] = _Entry(score, stamp.astimezone(UTC))
    return entries


def _dump(entries: dict[int, _Entry]) -> str:
    decks = {
        str(deck_id): {"score": entry.score, "at": entry.at.strftime(_STAMP_FORMAT)}
        for deck_id, entry in sorted(entries.items())
    }
    return json.dumps({"version": FORMAT_VERSION, "decks": decks}, indent=1) + "\n"
