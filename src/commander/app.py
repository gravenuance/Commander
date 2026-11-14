"""The desktop app: one window over the deck boards, drawn by WebView2 (Qt WebEngine on Linux) through
pywebview (ADR 0006).

The page is the HTML, CSS and JavaScript files in commander/web, inlined into one document, so no web server
runs; the page calls Desk's methods directly through pywebview's bridge.
"""

import logging
import platform
import re
import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from commander import __version__
from commander.applog import install_file_log
from commander.carddb import CARD_DB_FILE
from commander.config import (
    ConfigError,
    cache_dir,
    config_path,
    read_config,
    resolve_user,
    resolve_workspace,
    webview_dir,
)
from commander.credentials import TokenStore
from commander.desk import Desk, DeskSettings, warm_thread
from commander.updates import remove_old_exe, running_exe

if TYPE_CHECKING:
    import httpx

log = logging.getLogger(__name__)

WINDOW_TITLE = "Commander"
_CSS_SLOT = "/*APP_CSS*/"
_CSP_SCRIPTS = "script-src 'unsafe-inline'"
# index.html lists the page scripts in the order they load; each tag is replaced by the file's text.
_SCRIPT_TAG = re.compile(r'<script src="([a-z]+\.js)"></script>')


def page_html(platform: str | None = None) -> str:
    """The app page with its stylesheet and scripts inlined, for this system's window (`platform` is a
    `sys.platform` value)."""
    web = resources.files("commander") / "web"
    html = (web / "index.html").read_text(encoding="utf-8")
    css = (web / "app.css").read_text(encoding="utf-8")
    if html.count(_CSP_SCRIPTS) != 1:
        raise ValueError("index.html must hold one script policy")
    if (sys.platform if platform is None else platform) != "win32":
        # pywebview's Qt backend builds the bridge's functions with `new Function`, which the policy refuses
        # without this; the page loads nothing from outside (default-src 'none') and writes text nodes only.
        html = html.replace(_CSP_SCRIPTS, f"{_CSP_SCRIPTS} 'unsafe-eval'")
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
    return DeskSettings(folder, cache_dir(env) / CARD_DB_FILE, name, file.meta, path, documents, file.targets)


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


def window_backend(platform: str, env: Mapping[str, str]) -> Literal["qt"] | None:
    """The pywebview backend to ask for: None lets it pick (WinForms and WebView2 on Windows, or the one
    PYWEBVIEW_GUI names); on Linux it is Qt, which the dependencies bring, so GTK is not tried first."""
    if platform == "win32" or env.get("PYWEBVIEW_GUI"):
        return None
    return "qt"


def run_app(desk: Desk, *, storage: Path, debug: bool = False, gui: Literal["qt"] | None = None) -> None:
    """Open the window and block until it closes; the window keeps its profile (HTTP cache, page storage) in
    `storage`, so card images load from disk after their first view."""
    # pywebview loads .NET through pythonnet, which takes a moment; only the app pays for it.
    import webview  # noqa: PLC0415

    # pywebview types its url parameter with an untyped callable.
    window = webview.create_window(  # pyright: ignore[reportUnknownMemberType]
        WINDOW_TITLE,
        html=page_html(),
        js_api=desk,
        width=1440,
        height=900,
        min_size=(900, 600),
        text_select=True,
    )

    if window is None:
        raise RuntimeError("pywebview created no window")

    def on_closed() -> None:
        # The Cardmarket windows belong to a scan; the loop ends only when none is left. The scan is asked to
        # cancel before they are destroyed, so a failure on a destroyed window is the cancel's.
        desk.stop_buying()
        for other in list(webview.windows):
            if other is not window:
                other.destroy()

    window.events.closed += on_closed
    log.info("window opened")
    # A packed exe unpacks its data to a folder, so the icon has a real path for the toolkit to load.
    with resources.as_file(resources.files("commander") / "web" / "commander.ico") as icon:
        webview.start(gui=gui, debug=debug, private_mode=False, storage_path=str(storage), icon=str(icon))
    log.info("window closed")


def launch(
    env: Mapping[str, str],
    *,
    client_factory: Callable[[], "httpx.Client"],
    token_store: TokenStore,
    now: Callable[[], datetime],
    config: str | None = None,
    workspace: str | None = None,
    user: str | None = None,
    debug: bool = False,
) -> None:
    settings = desk_settings(env, config=config, workspace=workspace, user=user)
    setup_logging(cache_dir(env), env, settings)
    remove_old_exe(running_exe())
    desk = Desk(
        settings,
        client_factory=client_factory,
        token_store=token_store,
        now=now,
        quit_app=close_windows,
        warm_up=warm_thread,
    )
    try:
        run_app(desk, storage=webview_dir(env), debug=debug, gui=window_backend(sys.platform, env))
    finally:
        desk.close()


def close_windows() -> None:
    """Close the app's window, which ends `run_app`; an update calls it once the new exe has started."""
    import webview  # noqa: PLC0415

    for window in list(webview.windows):
        window.destroy()
