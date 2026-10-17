"""The app's entry point and its log: a crash before the window opens must still reach the log file."""

import logging
import sys
import threading
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from commander import app, app_main
from commander.carddb import CARD_DB_FILE
from commander.config import ConfigError
from commander.desk import Desk, DeskSettings


@pytest.fixture(autouse=True)
def restored_logging() -> Iterator[None]:
    """Give back the root logger and the crash hooks the code under test installs."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    hooks = sys.excepthook, threading.excepthook
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)
    sys.excepthook, threading.excepthook = hooks


@pytest.fixture(autouse=True)
def no_window_and_no_real_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Nothing here may open a window or a dialog, or read or write the user's real config and data."""
    for name, folder in (
        ("APPDATA", "appdata"),
        ("LOCALAPPDATA", "localappdata"),
        ("CMDR_CACHE_DIR", "cache"),
    ):
        monkeypatch.setenv(name, str(tmp_path / folder))
    for name in ("CMDR_CONFIG", "CMDR_USER", "CMDR_WORKSPACE", "CMDR_LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)

    def no_window(_desk: Desk, *, debug: bool = False) -> None:  # noqa: ARG001 - mirrors run_app
        raise AssertionError("a test tried to open the app window")

    shown: list[str] = []
    monkeypatch.setattr(app, "run_app", no_window)
    monkeypatch.setattr(app_main, "_show_error", shown.append)
    return shown


def file_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]


def test_a_crash_before_the_window_opens_reaches_the_log_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_window_and_no_real_files: list[str]
) -> None:
    def crash(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("import-time crash")

    shown = no_window_and_no_real_files
    monkeypatch.setattr(app, "launch", crash)
    assert app_main.main() == 1
    log = (tmp_path / "cache" / "logs" / "commander.log").read_text(encoding="utf-8")
    assert "Commander failed to start" in log
    assert "RuntimeError: import-time crash" in log
    assert shown == ["Commander could not start; the details are in the log."]


def test_a_startup_error_with_a_message_is_logged_and_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_window_and_no_real_files: list[str]
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise ConfigError("no deck folder")

    shown = no_window_and_no_real_files
    monkeypatch.setattr(app, "launch", refuse)
    assert app_main.main() == 1
    assert shown == ["no deck folder"]
    assert "no deck folder" in (tmp_path / "cache" / "logs" / "commander.log").read_text(encoding="utf-8")


def test_setting_up_the_log_twice_keeps_one_file_handler(tmp_path: Path) -> None:
    settings = DeskSettings(tmp_path / "decks", tmp_path / CARD_DB_FILE, None)
    path = app.setup_logging(tmp_path, {}, settings)
    assert app.setup_logging(tmp_path, {}, settings) == path
    assert len(file_handlers()) == 1
