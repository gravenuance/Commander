"""Entry point of Commander.exe: the desktop app with no console, so a startup failure shows in a dialog."""

import ctypes
import logging
import shutil
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime

from commander.applog import install_file_log
from commander.config import cache_dir, environment

_MB_ICONERROR = 0x10

log = logging.getLogger(__name__)


def main() -> int:
    env = environment()
    # First, before the rest of the app is imported, so a crash while it loads is in the log too.
    try:
        install_file_log(cache_dir(env), env)
    except OSError as error:
        print(f"Commander: cannot open the log file: {error}", file=sys.stderr)
    try:
        return _start(env)
    except Exception:
        log.exception("Commander failed to start")
        _show_error("Commander could not start; the details are in the log.")
        return 1


def _start(env: Mapping[str, str]) -> int:
    # Imported here, after the log is open: the app's own imports are what may fail.
    from commander.app import WindowError, launch  # noqa: PLC0415
    from commander.board import BoardError  # noqa: PLC0415
    from commander.carddb import CardDataError  # noqa: PLC0415
    from commander.config import ConfigError  # noqa: PLC0415
    from commander.credentials import KeyringTokenStore  # noqa: PLC0415
    from commander.net import make_client  # noqa: PLC0415

    try:
        launch(
            env,
            client_factory=make_client,
            token_store=KeyringTokenStore(),
            now=lambda: datetime.now(UTC),
            debug="--debug" in sys.argv[1:],
        )
    except (ConfigError, CardDataError, BoardError, WindowError, OSError) as error:
        log.exception("Commander could not start")
        _show_error(str(error))
        return 1
    return 0


def _show_error(message: str) -> None:
    """A dialog on Windows. Elsewhere stderr, and a desktop notification where `notify-send` exists, since an
    app started from a launcher has no terminal to print to."""
    windll = getattr(ctypes, "windll", None)
    if windll is not None:
        windll.user32.MessageBoxW(None, message, "Commander", _MB_ICONERROR)
        return
    print(f"Commander: {message}", file=sys.stderr)
    notify = shutil.which("notify-send")
    if notify is None:
        return
    try:
        # Not waited for: the notification must not hold the exit.
        subprocess.Popen(  # noqa: S603 - the program is the one `which` found, the arguments are ours
            [notify, "--app-name=Commander", "--urgency=critical", "Commander", message],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        log.warning("could not show the error with notify-send: %s", error)


if __name__ == "__main__":
    raise SystemExit(main())
