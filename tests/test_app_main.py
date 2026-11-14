"""The app's entry point and its log: a crash before the window opens must still reach the log file."""

import ctypes
import logging
import shutil
import subprocess
import sys
import threading
import types
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Self, cast

import pytest

from commander import app, app_main
from commander.carddb import CARD_DB_FILE
from commander.config import ConfigError
from commander.desk import Desk, DeskSettings

# The functions the autouse fixture below replaces, kept to test them.
_real_run_app = app.run_app
_real_show_error = app_main._show_error  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001


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
        ("CMDR_CONFIG", "config"),
        ("CMDR_CACHE_DIR", "cache"),
    ):
        monkeypatch.setenv(name, str(tmp_path / folder))
    for name in ("CMDR_USER", "CMDR_WORKSPACE", "CMDR_LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)

    def no_window(
        _desk: Desk,
        *,
        storage: Path,  # noqa: ARG001 - mirrors run_app
        debug: bool = False,  # noqa: ARG001
        gui: app.Backend | None = None,  # noqa: ARG001
    ) -> None:
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


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("win32", {}, None),
        ("win32", {"PYWEBVIEW_GUI": "qt"}, "qt"),
        ("linux", {}, "qt"),
        ("linux", {"PYWEBVIEW_GUI": "gtk"}, "gtk"),
        ("linux", {"PYWEBVIEW_GUI": "GTK"}, "gtk"),
        ("linux", {"PYWEBVIEW_GUI": ""}, "qt"),
        ("linux", {"PYWEBVIEW_GUI": "nonsense"}, "qt"),
        ("darwin", {}, None),
        ("freebsd14", {}, None),
    ],
)
def test_the_window_backend_is_the_one_named_else_qt_on_linux_else_pywebviews_own_pick(
    platform: str, env: dict[str, str], expected: app.Backend | None
) -> None:
    assert app.window_backend(env, platform=platform) == expected


@pytest.mark.parametrize(
    ("backend", "eval_allowed"), [("qt", True), ("gtk", True), ("edgechromium", False), (None, False)]
)
def test_the_page_allows_string_evaluation_for_the_backend_that_needs_it_only(
    backend: app.Backend | None, *, eval_allowed: bool
) -> None:
    assert ("'unsafe-eval'" in app.page_html(backend)) is eval_allowed


class FakeWebview:
    """What `run_app` uses of pywebview: windows, `start` and the error type, recorded."""

    def __init__(self) -> None:
        self.windows: list[object] = []
        self.html = ""
        self.started_with: dict[str, object] = {}
        self.fail_with: Exception | None = None

    def create_window(self, _title: str, *, html: str, **_kwargs: object) -> object:
        self.html = html
        window = types.SimpleNamespace(events=types.SimpleNamespace(closed=_Event()))
        self.windows.append(window)
        return window

    def start(self, **kwargs: object) -> None:
        self.started_with = kwargs
        if self.fail_with is not None:
            raise self.fail_with


class _Event:
    """pywebview's event: `+=` adds a handler."""

    def __iadd__(self, _handler: object) -> Self:
        return self


def install_fake_webview(monkeypatch: pytest.MonkeyPatch, fake: FakeWebview) -> type[Exception]:
    errors = types.ModuleType("webview.errors")
    window_error = type("WebViewException", (Exception,), {})
    vars(errors)["WebViewException"] = window_error
    module = types.ModuleType("webview")
    for name in ("create_window", "start", "windows"):
        vars(module)[name] = getattr(fake, name)
    monkeypatch.setitem(sys.modules, "webview", module)
    monkeypatch.setitem(sys.modules, "webview.errors", errors)
    return window_error


def test_the_window_gets_the_backend_chosen_and_the_page_made_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeWebview()
    install_fake_webview(monkeypatch, fake)

    _real_run_app(cast("Desk", object()), storage=tmp_path / "webview", gui="qt")

    assert fake.started_with["gui"] == "qt"
    assert fake.started_with["storage_path"] == str(tmp_path / "webview")
    assert "'unsafe-eval'" in fake.html


def test_a_window_toolkit_that_will_not_start_becomes_a_window_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeWebview()
    window_error = install_fake_webview(monkeypatch, fake)
    fake.fail_with = window_error("You must have either QT or GTK")

    with pytest.raises(app.WindowError, match="could not open: You must have either QT or GTK"):
        _real_run_app(cast("Desk", object()), storage=tmp_path)


@pytest.mark.parametrize(("platform", "readme"), [("linux", True), ("win32", False), ("darwin", False)])
def test_only_on_linux_does_a_window_failure_point_to_the_system_libraries(
    platform: str, *, readme: bool
) -> None:
    message = str(app.window_failure(RuntimeError("no display"), platform=platform))
    assert message.startswith("The app's window could not open: no display")
    assert ("Qt's system libraries" in message) is readme


def test_a_window_error_is_logged_and_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_window_and_no_real_files: list[str]
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise app.WindowError("The app's window could not open: no display")

    shown = no_window_and_no_real_files
    monkeypatch.setattr(app, "launch", refuse)
    assert app_main.main() == 1
    assert shown == ["The app's window could not open: no display"]
    assert "no display" in (tmp_path / "cache" / "logs" / "commander.log").read_text(encoding="utf-8")


def program_not_found(_name: str) -> None:
    return None


def notify_send_found(_name: str) -> str:
    return "/usr/bin/notify-send"


class RecordedPopen:
    def __init__(self, *, fail: bool = False) -> None:
        self.argv: list[list[str]] = []
        self._fail = fail

    def __call__(self, argv: list[str], **_kwargs: object) -> None:
        if self._fail:
            raise OSError("no such program")
        self.argv.append(argv)


@pytest.fixture
def no_dialog_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """A system without Windows' message box, whatever the tests run on."""
    monkeypatch.setattr(ctypes, "windll", None, raising=False)


@pytest.mark.usefixtures("no_dialog_api")
def test_without_a_dialog_or_notify_send_the_error_goes_to_stderr_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    popen = RecordedPopen()
    monkeypatch.setattr(shutil, "which", program_not_found)
    monkeypatch.setattr(subprocess, "Popen", popen)

    _real_show_error("no display")

    assert capsys.readouterr().err == "Commander: no display\n"
    assert popen.argv == []


@pytest.mark.usefixtures("no_dialog_api")
def test_where_notify_send_exists_the_error_is_also_a_notification_that_is_not_waited_for(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    popen = RecordedPopen()
    monkeypatch.setattr(shutil, "which", notify_send_found)
    monkeypatch.setattr(subprocess, "Popen", popen)

    _real_show_error("no display")

    assert capsys.readouterr().err == "Commander: no display\n"
    assert popen.argv == [
        ["/usr/bin/notify-send", "--app-name=Commander", "--urgency=critical", "Commander", "no display"]
    ]


@pytest.mark.usefixtures("no_dialog_api")
def test_a_notification_that_cannot_start_is_logged_and_the_error_still_reaches_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(shutil, "which", notify_send_found)
    monkeypatch.setattr(subprocess, "Popen", RecordedPopen(fail=True))

    with caplog.at_level(logging.WARNING):
        _real_show_error("no display")

    assert capsys.readouterr().err == "Commander: no display\n"
    assert "could not show the error with notify-send: no such program" in caplog.text


def test_setting_up_the_log_twice_keeps_one_file_handler(tmp_path: Path) -> None:
    settings = DeskSettings(tmp_path / "decks", tmp_path / CARD_DB_FILE, None)
    path = app.setup_logging(tmp_path, {}, settings)
    assert app.setup_logging(tmp_path, {}, settings) == path
    assert len(file_handlers()) == 1
