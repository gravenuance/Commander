"""Entry point of Commander.exe: the desktop app with no console, so a startup failure shows in a dialog."""

import ctypes
import logging
import sys
from datetime import UTC, datetime

from commander.app import launch
from commander.board import BoardError
from commander.carddb import CardDataError
from commander.config import ConfigError, environment
from commander.credentials import KeyringTokenStore
from commander.net import make_client

_MB_ICONERROR = 0x10


def main() -> int:
    try:
        launch(
            environment(),
            client_factory=make_client,
            token_store=KeyringTokenStore(),
            now=lambda: datetime.now(UTC),
            debug="--debug" in sys.argv[1:],
        )
    except (ConfigError, CardDataError, BoardError, OSError) as error:
        _show_error(str(error))
        return 1
    except Exception:
        logging.getLogger(__name__).exception("Commander failed to start")
        _show_error("Commander could not start; the details are in the log.")
        return 1
    return 0


def _show_error(message: str) -> None:
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        print(f"Commander: {message}", file=sys.stderr)
        return
    windll.user32.MessageBoxW(None, message, "Commander", _MB_ICONERROR)


if __name__ == "__main__":
    raise SystemExit(main())
