"""File helpers: atomic writes (a reader holding the target open for a moment must not fail the write) and
owner-only folders."""

import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from commander import fsutil
from commander.fsutil import ensure_private_folder, write_text_atomic

on_posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX folder modes")


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def common_umask() -> Iterator[None]:
    """The umask most Linux logins have, which gives a new folder 0o755."""
    previous = os.umask(0o022)
    yield
    os.umask(previous)


@on_posix_only
@pytest.mark.usefixtures("common_umask")
def test_a_new_private_folder_is_owner_only_even_under_a_permissive_umask(tmp_path: Path) -> None:
    folder = tmp_path / "cache" / "webview"
    ensure_private_folder(folder)
    assert mode_of(folder) == 0o700


@on_posix_only
@pytest.mark.usefixtures("common_umask")
def test_an_existing_wider_folder_is_tightened_and_its_contents_are_kept(tmp_path: Path) -> None:
    folder = tmp_path / "webview"
    folder.mkdir()
    (folder / "Cookies").write_text("session", encoding="utf-8")
    assert mode_of(folder) == 0o755
    ensure_private_folder(folder)
    assert mode_of(folder) == 0o700
    assert (folder / "Cookies").read_text(encoding="utf-8") == "session"


@on_posix_only
def test_a_private_folder_is_left_as_it_is_when_asked_again(tmp_path: Path) -> None:
    folder = tmp_path / "webview"
    ensure_private_folder(folder)
    ensure_private_folder(folder)
    assert mode_of(folder) == 0o700


@on_posix_only
def test_a_folder_that_lost_its_owner_bits_is_made_usable_again(tmp_path: Path) -> None:
    folder = tmp_path / "webview"
    folder.mkdir()
    folder.chmod(0o600)
    ensure_private_folder(folder)
    assert mode_of(folder) == 0o700


@on_posix_only
@pytest.mark.usefixtures("common_umask")
def test_a_folder_that_refuses_the_mode_change_is_logged_and_the_app_carries_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    folder = tmp_path / "webview"

    def refuse(_path: Path, _mode: int) -> None:
        raise PermissionError("not yours")

    monkeypatch.setattr(Path, "chmod", refuse)
    with caplog.at_level("WARNING", logger="commander.fsutil"):
        ensure_private_folder(folder)
    assert folder.is_dir()
    assert f"could not make {folder} owner-only: not yours" in caplog.text


def test_a_private_folder_exists_afterwards_on_every_system(tmp_path: Path) -> None:
    folder = tmp_path / "a" / "b"
    ensure_private_folder(folder)
    assert folder.is_dir()


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
