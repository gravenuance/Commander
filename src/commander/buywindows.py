"""The windows the Buy screen opens on Cardmarket (ADR 0067): the hidden one the scan reads through and the
visible one the owner signs in with. They are created from a background thread after the app's window exists
and share the app's WebView2 profile. A window is always closed with `destroy()`: a killed WebView2 leaves
the profile locked and the next window white.
"""

import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from commander.buy import BuyEngine, RouteSource
from commander.cmsession import BLANK_PAGE, BrowserError, SignInWindow, WebviewBrowser, sign_in_window
from commander.cmshipping import CardmarketShipping
from commander.credentials import CardmarketSessionStore, SessionStore
from commander.deckcache import Clock
from commander.net import Fetcher

if TYPE_CHECKING:
    import httpx

log = logging.getLogger(__name__)

SCAN_WINDOW_TITLE = "Cardmarket scan"
SIGN_IN_WINDOW_TITLE = "Sign in to Cardmarket"


class SignInHandle:
    """The sign-in window, closed once; a window the owner closed already is not closed again."""

    def __init__(self, handle: SignInWindow) -> None:
        self._handle = handle
        self.gone = False

    def close(self) -> None:
        if self.gone:
            return
        self.gone = True
        self._handle.close()


def open_scan_browser(store: SessionStore) -> WebviewBrowser:
    """A hidden Cardmarket window with the saved session added, ready to load pages."""
    import webview  # noqa: PLC0415 - loads .NET, so only when a window is wanted

    # pywebview types its url parameter with an untyped callable.
    window = webview.create_window(SCAN_WINDOW_TITLE, html=BLANK_PAGE, hidden=True)  # pyright: ignore[reportUnknownMemberType]
    if window is None:
        raise BrowserError("pywebview created no scan window")
    try:
        return WebviewBrowser(window, store)
    except BaseException:
        window.destroy()
        raise


def open_sign_in(
    store: SessionStore, on_signed_in: Callable[[], None], on_closed: Callable[[], None]
) -> SignInHandle:
    """The visible sign-in window. `on_signed_in` runs when a signed-in Cardmarket page has loaded and the
    session is saved; `on_closed` when the owner closes the window."""
    handle = sign_in_window(store, on_signed_in, title=SIGN_IN_WINDOW_TITLE)
    window = SignInHandle(handle)

    def closed() -> None:
        window.gone = True
        on_closed()

    handle.window.events.closed += closed
    return window


def production_engine(client_factory: Callable[[], "httpx.Client"], cache: Path, now: Clock) -> BuyEngine:
    """The real windows, session store and shipping tables (cached in `cache`) for the Buy screen."""
    sessions = CardmarketSessionStore()

    @contextmanager
    def shipping() -> Generator[RouteSource]:
        with client_factory() as http:
            yield CardmarketShipping(Fetcher(http), cache, now=now)

    return BuyEngine(
        open_browser=lambda: open_scan_browser(sessions),
        open_sign_in=lambda signed_in, closed: open_sign_in(sessions, signed_in, closed),
        sessions=sessions,
        shipping=shipping,
    )
