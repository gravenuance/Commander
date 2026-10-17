"""The desktop app: one window over the deck boards, drawn by WebView2 through pywebview (ADR 0006).

The page is the HTML, CSS and JavaScript files in commander/web, inlined into one document, so no web server
runs; the page calls Desk's methods directly through pywebview's bridge.
"""

import logging
import platform
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from importlib import resources
from pathlib import Path

import httpx

from commander import __version__
from commander.applog import install_file_log
from commander.carddb import CARD_DB_FILE
from commander.config import ConfigError, cache_dir, config_path, read_config, resolve_user, resolve_workspace
from commander.credentials import TokenStore
from commander.desk import Desk, DeskSettings

log = logging.getLogger(__name__)

WINDOW_TITLE = "Commander"
_CSS_SLOT = "/*APP_CSS*/"
# index.html lists the page scripts in the order they load; each tag is replaced by the file's text.
_SCRIPT_TAG = re.compile(r'<script src="([a-z]+\.js)"></script>')


def page_html() -> str:
    """The app page with its stylesheet and scripts inlined."""
    web = resources.files("commander") / "web"
    html = (web / "index.html").read_text(encoding="utf-8")
    css = (web / "app.css").read_text(encoding="utf-8")
    # Inlined text must not close its own element early.
    if "</style" in css.casefold():
        raise ValueError("app.css contains a closing tag of its own element")
    if html.count(_CSS_SLOT) != 1:
        raise ValueError("index.html must hold one stylesheet slot")

    def inline(tag: re.Match[str]) -> str:
        script = (web / tag[1]).read_text(encoding="utf-8")
        if "</script" in script.casefold():
            raise ValueError(f"{tag[1]} contains a closing tag of its own element")
        return f"<script>{script}</script>"

    html, scripts = _SCRIPT_TAG.subn(inline, html.replace(_CSS_SLOT, css))
    if scripts == 0:
        raise ValueError("index.html must list the page scripts")
    return html


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
    """The app's rotating log file, opened already by the entry point, headed by what produced it."""
    path = install_file_log(folder, env)
    log.info(
        "Commander %s on Python %s, %s; decks in %s, card data %s, user %s",
        __version__,
        platform.python_version(),
        platform.platform(),
        settings.workspace,
        settings.card_db,
        settings.user or "not set",
    )
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
    # A packed exe unpacks its data to a folder, so the icon has a real path for WinForms to load.
    with resources.as_file(resources.files("commander") / "web" / "commander.ico") as icon:
        webview.start(debug=debug, private_mode=True, icon=str(icon))
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
