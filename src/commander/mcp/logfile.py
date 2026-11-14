"""The server's log file: stdout carries the protocol, so everything else goes to a file."""

import logging
import platform
import sys
from collections.abc import Mapping
from importlib import metadata
from logging.handlers import RotatingFileHandler
from pathlib import Path

from commander import __version__
from commander.applog import ENV_LOG_LEVEL, LOG_BYTES, LOG_FILES
from commander.config import config_path

LOGGER_NAME = "commander.mcp"
LOG_FILE = "commander-mcp.log"


def install_log(folder: Path, env: Mapping[str, str]) -> Path | None:
    """Log the server's own events to folder/logs/commander-mcp.log, rotating, and say what is running.

    It hangs on the `commander.mcp` logger and not on the root one, which every `cmdr` command resets
    (`cmdr.main` calls `basicConfig(force=True)`). The path is None when the file cannot be opened; the log
    then goes to stderr, which the client keeps apart from the protocol.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.propagate = False
    level = env.get(ENV_LOG_LEVEL, "INFO").upper()
    logger.setLevel(level if level in logging.getLevelNamesMapping() else logging.INFO)
    path = folder / "logs" / LOG_FILE
    opened: Path | None = path
    handler: logging.Handler
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=LOG_BYTES, backupCount=LOG_FILES, encoding="utf-8")
    except OSError as error:
        handler = logging.StreamHandler(sys.stderr)
        opened = None
        print(f"cmdr-mcp: cannot open the log file {path}: {error}", file=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.info(
        "cmdr-mcp %s (mcp %s) on %s, Python %s; config %s, cache %s",
        __version__,
        _mcp_version(),
        platform.platform(),
        platform.python_version(),
        config_path(None, env),
        folder,
    )
    return opened


def _mcp_version() -> str:
    try:
        return metadata.version("mcp")
    except metadata.PackageNotFoundError:
        return "unknown"
