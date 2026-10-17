"""The app's log file and crash hooks, light enough to install before anything else starts."""

import logging
import sys
import threading
from collections.abc import Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

log = logging.getLogger(__name__)

LOG_FILE = "commander.log"
LOG_BYTES = 5 * 1024 * 1024
LOG_FILES = 5
ENV_LOG_LEVEL = "CMDR_LOG_LEVEL"


def install_file_log(folder: Path, env: Mapping[str, str]) -> Path:
    """Log to folder/logs/commander.log (rotating) and record unhandled exceptions; safe to call twice."""
    logs = folder / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / LOG_FILE
    root = logging.getLogger()
    if not any(
        isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == path.absolute() for h in root.handlers
    ):
        handler = RotatingFileHandler(path, maxBytes=LOG_BYTES, backupCount=LOG_FILES, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        # Handlers already there (the CLI's stderr) keep the level they had; only the file gets more.
        for existing in root.handlers:
            if existing.level == logging.NOTSET:
                existing.setLevel(root.level)
        root.addHandler(handler)
    level = env.get(ENV_LOG_LEVEL, "INFO").upper()
    root.setLevel(level if level in logging.getLevelNamesMapping() else logging.INFO)
    for noisy in ("httpx", "httpcore", "pywebview"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    sys.excepthook = _log_crash
    threading.excepthook = _log_thread_crash
    return path


def _log_crash(kind: type[BaseException], error: BaseException, trace: TracebackType | None) -> None:
    log.critical("unhandled exception", exc_info=(kind, error, trace))
    sys.__excepthook__(kind, error, trace)


def _log_thread_crash(args: threading.ExceptHookArgs) -> None:
    if args.exc_value is not None:
        log.critical(
            "unhandled exception in thread %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
