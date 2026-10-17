"""Atomic file writes: a reader holding the target open for a moment must not fail the write."""

from pathlib import Path

import pytest

from commander import fsutil
from commander.fsutil import write_text_atomic


def refuse_replacing(times: int, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Make Path.replace raise PermissionError (Windows: the target is open elsewhere) `times` times."""
    real = Path.replace
    attempts: list[int] = []

    def replace(self: Path, target: Path) -> Path:
        attempts.append(1)
        if len(attempts) <= times:
            raise PermissionError(13, "in use")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(fsutil, "REPLACE_FIRST_DELAY_SECONDS", 0.0)
    return attempts


def test_a_write_waits_out_a_target_another_process_holds_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "board.json"
    target.write_text("old\n", encoding="utf-8")
    attempts = refuse_replacing(2, monkeypatch)
    write_text_atomic(target, "new\n")
    assert len(attempts) == 3
    assert target.read_text(encoding="utf-8") == "new\n"
    assert [p.name for p in tmp_path.iterdir()] == ["board.json"]


def test_a_target_that_stays_locked_fails_and_leaves_the_old_file_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "board.json"
    target.write_text("old\n", encoding="utf-8")
    refuse_replacing(fsutil.REPLACE_ATTEMPTS, monkeypatch)
    with pytest.raises(PermissionError):
        write_text_atomic(target, "new\n")
    assert target.read_text(encoding="utf-8") == "old\n"
    assert [p.name for p in tmp_path.iterdir()] == ["board.json"]
