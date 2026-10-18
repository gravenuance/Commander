import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from commander.archidekt import PeerDeck
from commander.deckreads import DeckRead, DeckReads

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 8, 16, tzinfo=UTC)
READ = DeckRead(("Ellivere of the Wild Court",), ("Mind Stone",), (), (("Forest", 3),))


def peer(deck_id: int, updated_at: datetime) -> PeerDeck:
    return PeerDeck(deck_id, f"deck {deck_id}", "someone", 1200, updated_at, 3)


def age(path: Path, days: float) -> None:
    """Make the file's last use `days` before NOW."""
    stamp = (NOW - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))


def stored(tmp_path: Path, deck_id: int, updated_at: datetime, *, unused_days: float = 0) -> Path:
    DeckReads(tmp_path).save(peer(deck_id, updated_at), READ, NOW)
    path = tmp_path / f"{deck_id}.json"
    age(path, unused_days)
    return path


def prune(tmp_path: Path) -> int:
    return DeckReads(tmp_path).prune(now=NOW, window_start=WINDOW_START)


def test_a_cached_read_comes_back_for_the_same_updated_at_only(tmp_path: Path) -> None:
    updated = NOW - timedelta(days=3)
    reads = DeckReads(tmp_path)
    reads.save(peer(7, updated), READ, NOW)
    assert reads.load(peer(7, updated), NOW) == READ
    assert reads.load(peer(7, updated + timedelta(hours=1)), NOW) is None


def test_a_deck_unused_for_over_thirty_days_is_pruned(tmp_path: Path) -> None:
    old = stored(tmp_path, 1, NOW - timedelta(days=5), unused_days=31)
    assert prune(tmp_path) == 1
    assert not old.exists()


def test_a_deck_used_within_thirty_days_is_kept(tmp_path: Path) -> None:
    recent = stored(tmp_path, 1, NOW - timedelta(days=5), unused_days=29)
    assert prune(tmp_path) == 0
    assert recent.exists()


def test_a_deck_last_updated_before_the_window_is_pruned_however_recently_it_was_used(tmp_path: Path) -> None:
    out = stored(tmp_path, 1, WINDOW_START - timedelta(seconds=1), unused_days=1)
    edge = stored(tmp_path, 2, WINDOW_START, unused_days=1)
    assert prune(tmp_path) == 1
    assert not out.exists()
    assert edge.exists()


def test_a_hit_counts_as_use_and_keeps_the_file_past_thirty_days(tmp_path: Path) -> None:
    path = stored(tmp_path, 1, NOW - timedelta(days=5), unused_days=40)
    assert DeckReads(tmp_path).load(peer(1, NOW - timedelta(days=5)), NOW) == READ
    assert prune(tmp_path) == 0
    assert path.exists()


def test_a_miss_does_not_count_as_use(tmp_path: Path) -> None:
    path = stored(tmp_path, 1, NOW - timedelta(days=5), unused_days=40)
    assert DeckReads(tmp_path).load(peer(1, NOW - timedelta(days=4)), NOW) is None
    assert prune(tmp_path) == 1
    assert not path.exists()


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        json.dumps({"version": 2, "key": "x", "fetchedAt": NOW.isoformat(), "answer": {}}),
        json.dumps(
            {
                "version": 1,
                "key": "x",
                "fetchedAt": NOW.isoformat(),
                "answer": {"updatedAt": "2026-10-01T00:00:00+00:00"},
            }
        ),
    ],
    ids=["not-json", "not-an-object", "no-updated-at", "other-version"],
)
def test_a_file_that_cannot_be_parsed_is_pruned(tmp_path: Path, text: str) -> None:
    path = tmp_path / "9.json"
    path.write_text(text, encoding="utf-8")
    age(path, 1)
    assert prune(tmp_path) == 1
    assert not path.exists()


def test_other_files_in_the_folder_are_left_alone(tmp_path: Path) -> None:
    other = tmp_path / ".1.json.abc.tmp"
    other.write_text("x", encoding="utf-8")
    age(other, 90)
    assert prune(tmp_path) == 0
    assert other.exists()


def test_pruning_a_missing_folder_does_nothing(tmp_path: Path) -> None:
    assert DeckReads(tmp_path / "missing").prune(now=NOW, window_start=WINDOW_START) == 0
