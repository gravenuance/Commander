"""How often the user opens each deck, with a 30-day half-life (ADR 0034)."""

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from commander.viewscore import ViewScores

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
STAMP = "2026-10-15T12:00:00Z"


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def __call__(self) -> datetime:
        return self.at


def write(path: Path, decks: dict[str, dict[str, object]], version: int = 1) -> None:
    path.write_text(json.dumps({"version": version, "decks": decks}), encoding="utf-8")


def test_without_a_file_every_deck_scores_zero(tmp_path: Path) -> None:
    assert ViewScores(tmp_path / "views.json", Clock()).scores() == {}


def test_opening_a_deck_adds_one(tmp_path: Path) -> None:
    views = ViewScores(tmp_path / "views.json", Clock())
    views.record_open(7)
    views.record_open(7)
    views.record_open(9)
    assert views.scores() == {7: 2.0, 9: 1.0}


def test_a_score_halves_every_thirty_days(tmp_path: Path) -> None:
    path = tmp_path / "views.json"
    write(
        path,
        {
            "7": {"score": 4.0, "at": "2026-09-15T12:00:00Z"},
            "8": {"score": 4.0, "at": "2026-08-16T12:00:00Z"},
        },
    )
    scores = ViewScores(path, Clock()).scores()
    # 30 days old: 4 * 0.5 = 2; 60 days old: 4 * 0.25 = 1.
    assert scores == {7: pytest.approx(2.0), 8: pytest.approx(1.0)}


def test_a_score_decays_continuously_between_the_half_lives(tmp_path: Path) -> None:
    path = tmp_path / "views.json"
    write(path, {"7": {"score": 4.0, "at": "2026-09-30T12:00:00Z"}})
    # 15 days old: 4 * 0.5 ** 0.5 = 2.8284271.
    assert ViewScores(path, Clock()).scores()[7] == pytest.approx(2.8284271, abs=1e-6)


def test_an_open_decays_the_old_score_before_adding_one(tmp_path: Path) -> None:
    clock = Clock()
    views = ViewScores(tmp_path / "views.json", clock)
    views.record_open(7)
    clock.at = NOW + timedelta(days=30)
    views.record_open(7)
    assert views.scores()[7] == pytest.approx(1.5)
    clock.at = NOW + timedelta(days=90)
    views.record_open(7)
    # 1.5 decayed over 60 days is 0.375, and the open makes it 1.375.
    assert views.scores()[7] == pytest.approx(1.375)


def test_scores_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "views.json"
    ViewScores(path, Clock()).record_open(7)
    assert ViewScores(path, Clock()).scores() == {7: 1.0}


def test_the_file_holds_a_version_and_a_score_with_its_time_per_deck(tmp_path: Path) -> None:
    path = tmp_path / "views.json"
    ViewScores(path, Clock()).record_open(7)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "decks": {"7": {"score": 1.0, "at": STAMP}},
    }


def test_a_clock_that_runs_backwards_does_not_raise_a_score(tmp_path: Path) -> None:
    path = tmp_path / "views.json"
    write(path, {"7": {"score": 2.0, "at": "2026-10-20T12:00:00Z"}})
    assert ViewScores(path, Clock()).scores() == {7: 2.0}


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        '{"version": 1}',
        '{"version": 1, "decks": []}',
        '{"version": 2, "decks": {}}',
        '{"version": 1, "decks": {"7": {"score": "lots", "at": "2026-10-15T12:00:00Z"}}}',
        '{"version": 1, "decks": {"seven": {"score": 1, "at": "2026-10-15T12:00:00Z"}}}',
        '{"version": 1, "decks": {"7": {"score": 1, "at": "yesterday"}}}',
        '{"version": 1, "decks": {"7": {"score": 1, "at": "2026-10-15T12:00:00"}}}',
        '{"version": 1, "decks": {"7": {"score": -1, "at": "2026-10-15T12:00:00Z"}}}',
    ],
)
def test_a_damaged_file_scores_every_deck_zero_and_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, text: str
) -> None:
    path = tmp_path / "views.json"
    path.write_text(text, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="commander.viewscore"):
        assert ViewScores(path, Clock()).scores() == {}
    assert any("views.json" in message for message in caplog.messages)


def test_an_open_after_a_damaged_file_starts_again_from_zero(tmp_path: Path) -> None:
    path = tmp_path / "views.json"
    path.write_bytes(b"\xff\xfe garbage")
    views = ViewScores(path, Clock())
    views.record_open(7)
    assert views.scores() == {7: 1.0}


def test_a_score_that_cannot_be_saved_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "views.json"
    # A folder where the file belongs makes the write fail with an OSError.
    path.mkdir()
    with caplog.at_level(logging.WARNING, logger="commander.viewscore"):
        ViewScores(path, Clock()).record_open(7)
    assert any("could not save" in message for message in caplog.messages)


def test_a_score_decayed_to_nothing_is_dropped_when_the_file_is_next_written(tmp_path: Path) -> None:
    path = tmp_path / "views.json"
    # 365 days is 12.17 half-lives: 1 * 0.5 ** 12.17 = 0.0002, under the 0.01 that counts.
    write(path, {"7": {"score": 1.0, "at": "2025-10-15T12:00:00Z"}, "8": {"score": 1.0, "at": STAMP}})
    ViewScores(path, Clock()).record_open(9)
    assert sorted(json.loads(path.read_text(encoding="utf-8"))["decks"]) == ["8", "9"]
