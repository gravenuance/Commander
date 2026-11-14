"""Entry point of `cmdr-mcp`: the server on stdio."""

import logging

from commander.config import cache_dir, environment
from commander.mcp.logfile import LOGGER_NAME, install_log
from commander.mcp.tools import build_server

log = logging.getLogger(LOGGER_NAME)


def main() -> int:
    env = environment()
    install_log(cache_dir(env), env)
    try:
        build_server().run("stdio")
    except KeyboardInterrupt:
        log.info("interrupted")
    except Exception:
        # The last stop before the process ends: the traceback goes to the log, not to a client that is gone.
        log.critical("the server stopped on an unhandled exception", exc_info=True)
        return 1
    log.info("the client closed the connection")
    return 0
