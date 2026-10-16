"""The desktop app: one window over the deck boards, drawn by WebView2 through pywebview (ADR 0006).

The page is the HTML, CSS and JavaScript in commander/web, inlined into one document, so no web server
runs; the page calls Desk's methods directly through pywebview's bridge.
"""

import logging
import platform
import sys
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from importlib import resources
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

import httpx

from commander import __version__
from commander.carddb import CARD_DB_FILE
from commander.config import ConfigError, cache_dir, config_path, read_config, resolve_user, resolve_workspace
from commander.credentials import TokenStore
from commander.desk import Desk, DeskSettings

log = logging.getLogger(__name__)

WINDOW_TITLE = "Commander"
LOG_FILE = "commander.log"
LOG_BYTES = 5 * 1024 * 1024
LOG_FILES = 5
ENV_LOG_LEVEL = "CMDR_LOG_LEVEL"
_CSS_SLOT = "/*APP_CSS*/"
_JS_SLOT = "/*APP_JS*/"


def page_html() -> str:
    """The app page with its stylesheet and script inlined."""
    web = resources.files("commander") / "web"
    html = (web / "index.html").read_text(encoding="utf-8")
    css = (web / "app.css").read_text(encoding="utf-8")
    script = (web / "app.js").read_text(encoding="utf-8")
    # Inlined text must not close its own element early.
    if "</style" in css.casefold() or "</script" in script.casefold():
        raise ValueError("app.css or app.js contains a closing tag of its own element")
    if html.count(_CSS_SLOT) != 1 or html.count(_JS_SLOT) != 1:
        raise ValueError("index.html must hold one stylesheet slot and one script slot")
    return html.replace(_CSS_SLOT, css).replace(_JS_SLOT, script)


def desk_settings(
    env: Mapping[str, str], *, config: str | None, workspace: str | None, user: str | None
) -> DeskSettings:
    """Where the app finds the decks and card data; without an account it opens on the sign-in screen."""
    path = config_path(config, env)
    file = read_config(path)
    documents = Path.home() / "Documents"
    try:
        folder = resolve_workspace(workspace, env, file)
    except ConfigError:
        # No deck folder yet: signing in gives the account its own (ADR 0023); until then there are no decks.
        folder = documents / "Commander" / "decks"
    try:
        name: str | None = resolve_user(user, env, file)
    except ConfigError:
        name = None
    return DeskSettings(folder, cache_dir(env) / CARD_DB_FILE, name, file.meta, path, documents)


def setup_logging(folder: Path, env: Mapping[str, str], settings: DeskSettings) -> Path:
    """A rotating log file for the app, opened with what produced it, and hooks for crashes."""
    logs = folder / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / LOG_FILE
    handler = RotatingFileHandler(path, maxBytes=LOG_BYTES, backupCount=LOG_FILES, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    # Handlers already there (the CLI's stderr) keep the level they had; only the file gets more.
    for existing in root.handlers:
        if existing.level == logging.NOTSET:
            existing.setLevel(root.level)
    root.addHandler(handler)
    level = env.get(ENV_LOG_LEVEL, "INFO").upper()
    root.setLevel(level if level in logging.getLevelNamesMapping() else logging.INFO)
    for noisy in ("httpx", "httpcore", "pywebview"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log.info(
        "Commander %s on Python %s, %s; decks in %s, card data %s, user %s",
        __version__,
        platform.python_version(),
        platform.platform(),
        settings.workspace,
        settings.card_db,
        settings.user or "not set",
    )
    sys.excepthook = _log_crash
    threading.excepthook = _log_thread_crash
    return path


def run_app(desk: Desk, *, debug: bool = False) -> None:
    """Open the window and block until it closes."""
    # pywebview loads .NET through pythonnet, which takes a moment; only the app pays for it.
    import webview  # noqa: PLC0415

    # pywebview types its url parameter with an untyped callable.
    webview.create_window(  # pyright: ignore[reportUnknownMemberType]
        WINDOW_TITLE,
        html=page_html(),
        js_api=desk,
        width=1440,
        height=900,
        min_size=(900, 600),
        text_select=True,
    )
    log.info("window opened")
    webview.start(debug=debug, private_mode=True)
    log.info("window closed")


def launch(
    env: Mapping[str, str],
    *,
    client_factory: Callable[[], httpx.Client],
    token_store: TokenStore,
    now: Callable[[], datetime],
    config: str | None = None,
    workspace: str | None = None,
    user: str | None = None,
    debug: bool = False,
) -> None:
    settings = desk_settings(env, config=config, workspace=workspace, user=user)
    setup_logging(cache_dir(env), env, settings)
    run_app(Desk(settings, client_factory=client_factory, token_store=token_store, now=now), debug=debug)


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
