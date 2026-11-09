"""The window the Buy screen reads Cardmarket through (ADR 0067). It is created hidden, frameless and owned by
the app's window, from a background thread after the app's window exists, and shares the app's WebView2
profile. When a page needs the owner (a sign-in, Cloudflare's check) the window is shown exactly over the
Buy screen's empty pane; `CardmarketPane` decides when to place, show and hide it, and `Win32Ops` makes the
calls. A window is always closed with `destroy()`: a killed WebView2 leaves the profile locked and the next
window white.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import weakref
from contextlib import contextmanager
from ctypes import wintypes
from typing import TYPE_CHECKING, Any, Protocol, cast

from commander.buy import BuyEngine, RouteSource
from commander.buypane import PaneArea, screen_rect
from commander.cmsession import (
    BLANK_PAGE,
    CALL_TIMEOUT_S,
    BrowserError,
    Handover,
    LoadTimeout,
    Need,
    WebviewBrowser,
    within,
)
from commander.cmshipping import CardmarketShipping
from commander.credentials import CardmarketSessionStore, SessionStore
from commander.net import Fetcher

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

    import httpx
    from webview import Window

    from commander.deckcache import Clock

# ctypes' user32 and pythonnet's objects are known only at run time.
type _Native = Any

log = logging.getLogger(__name__)

SCAN_WINDOW_TITLE = "Cardmarket scan"

_GWL_EXSTYLE = -20
_WS_EX_APPWINDOW = 0x40000
_SWP_NOZORDER = 0x4
_SWP_NOACTIVATE = 0x10


class WindowOps(Protocol):
    """What the pane does to windows, so its decisions are tested without any."""

    def prepare(self, window: Window, main: Window) -> None:
        """Own `window` by `main` (it stays above the app only) and keep it off the taskbar."""
        ...

    def place(self, window: Window, main: Window, area: PaneArea) -> None:
        """Put `window` over `area` of `main`'s client area without showing it."""
        ...

    def show(self, window: Window) -> None:
        """Show `window` and give its page the keyboard focus."""
        ...

    def hide(self, window: Window) -> None: ...

    def watch_moves(self, main: Window, on_moved: Callable[..., None]) -> None: ...

    def unwatch_moves(self, main: Window, on_moved: Callable[..., None]) -> None: ...


class CardmarketPane:
    """Shows the covered window over the page's reported area, and hides it when the page reports none. The
    page's reports come from the bridge's thread and the cover calls from the scan's, hence the lock."""

    def __init__(self, ops: WindowOps, main_window: Callable[[], Window | None]) -> None:
        self._ops = ops
        self._main_window = main_window
        self._lock = threading.RLock()
        self._area: PaneArea | None = None
        self._covered: Window | None = None
        self._shown = False
        # The window that already has its owner and taskbar style, which are set once.
        self._prepared: Window | None = None

    def set_area(self, area: PaneArea | None) -> None:
        with self._lock:
            self._area = area
            self._apply()

    def cover(self, window: Window) -> None:
        """Show `window` over the area as soon as the page has reported one."""
        with self._lock:
            main = self._main_window()
            if main is None:
                raise BrowserError("the app's window is not available to sign in over")
            if self._prepared is not window:
                self._ops.prepare(window, main)
                self._prepared = window
            self._ops.watch_moves(main, self._on_moved)
            self._covered = window
            self._shown = False
            try:
                self._apply()
            except Exception:
                self._undo_cover(window)
                raise

    def _undo_cover(self, window: Window) -> None:
        """A cover that failed halfway leaves nothing subscribed or marked shown, so a retry starts clean."""
        try:
            self.uncover(window)
        except Exception:
            # The failure being handled is the one to report.
            log.warning("could not take the Cardmarket window back after a failed cover", exc_info=True)

    def uncover(self, window: Window) -> None:
        with self._lock:
            if self._covered is not window:
                return
            self._covered = None
            self._shown = False
            main = self._main_window()
            try:
                if main is not None:
                    self._ops.unwatch_moves(main, self._on_moved)
            finally:
                self._ops.hide(window)

    def _on_moved(self, *_: object) -> None:
        with self._lock:
            self._apply()

    def _apply(self) -> None:
        """Bring the covered window to the area: hidden when there is none, else placed and shown."""
        window, area = self._covered, self._area
        if window is None:
            return
        if area is None:
            if self._shown:
                self._ops.hide(window)
                self._shown = False
            return
        main = self._main_window()
        if main is None:
            return
        self._ops.place(window, main, area)
        if not self._shown:
            self._ops.show(window)
            self._shown = True


class PaneHandover:
    """The scan window's `Handover`: the screen says what the owner must do, and the pane shows the window."""

    def __init__(self, screen: Handover, pane: CardmarketPane, window: Window) -> None:
        self._screen = screen
        self._pane = pane
        self._window = window
        self._covering = False

    def show(self, need: Need) -> None:
        self._screen.show(need)
        if not self._covering:
            self._pane.cover(self._window)
            self._covering = True

    def hide(self) -> None:
        try:
            if self._covering:
                self._covering = False
                self._pane.uncover(self._window)
        finally:
            self._screen.hide()

    def cancelled(self) -> bool:
        return self._screen.cancelled()


def open_scan_browser(store: SessionStore, handover: Handover, pane: CardmarketPane) -> WebviewBrowser:
    """A hidden Cardmarket window with the saved session added, ready to load pages and to be handed over."""
    import webview  # noqa: PLC0415 - loads .NET, so only when a window is wanted

    # Frameless and shadowless so that, shown, it reads as part of the Buy screen; pywebview types its url
    # parameter with an untyped callable.
    window = webview.create_window(  # pyright: ignore[reportUnknownMemberType]
        SCAN_WINDOW_TITLE, html=BLANK_PAGE, hidden=True, frameless=True, shadow=False, easy_drag=False
    )
    if window is None:
        raise BrowserError("pywebview created no scan window")
    try:
        return WebviewBrowser(window, store, handover=PaneHandover(handover, pane, window))
    except BaseException:
        window.destroy()
        raise


def _app_window() -> Window | None:
    """The app's own window: the first one pywebview created, before any scan window."""
    import webview  # noqa: PLC0415 - loads .NET, so only when a window is wanted

    return webview.windows[0] if webview.windows else None


class Win32Ops:
    """`WindowOps` on pywebview's WinForms windows. pywebview calls SetProcessDPIAware, so Win32 coordinates
    are physical pixels. Its own `move` and `resize` show a hidden window, so the placing goes through
    SetWindowPos; WinForms recreates a window's handle when `ShowInTaskbar` changes, so the taskbar style is
    cleared on the handle itself."""

    def __init__(self) -> None:
        self._user32 = _load_user32()
        self._handles: weakref.WeakKeyDictionary[Window, int] = weakref.WeakKeyDictionary()

    def prepare(self, window: Window, main: Window) -> None:
        user32 = self._user32

        def apply() -> None:
            native: _Native = window.native
            native.Owner = cast("_Native", main.native)
            hwnd = self._handle(window)
            style = user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
            user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, style & ~_WS_EX_APPWINDOW)

        _on_ui_thread(main, apply)

    def place(self, window: Window, main: Window, area: PaneArea) -> None:
        origin = wintypes.POINT(0, 0)
        if not self._user32.ClientToScreen(self._handle(main), ctypes.byref(origin)):
            raise BrowserError("the app's window position could not be read")
        x, y, width, height = screen_rect((origin.x, origin.y), area)
        self._user32.SetWindowPos(
            self._handle(window), None, x, y, width, height, _SWP_NOZORDER | _SWP_NOACTIVATE
        )

    def show(self, window: Window) -> None:
        window.show()

        def focus() -> None:
            native: Any = window.native
            native.webview.Focus()

        _on_ui_thread(window, focus)

    def hide(self, window: Window) -> None:
        window.hide()

    def watch_moves(self, main: Window, on_moved: Callable[..., None]) -> None:
        main.events.moved += on_moved

    def unwatch_moves(self, main: Window, on_moved: Callable[..., None]) -> None:
        main.events.moved -= on_moved

    def _handle(self, window: Window) -> int:
        """The window's hwnd, read on the UI thread once and kept."""
        known = self._handles.get(window)
        if known is not None:
            return known
        found: list[int] = []

        def read() -> None:
            native: Any = window.native
            found.append(int(native.Handle.ToInt64()))

        _on_ui_thread(window, read)
        self._handles[window] = found[0]
        return found[0]


def _on_ui_thread(window: Window, work: Callable[[], None]) -> None:
    """Run `work` on the UI thread all pywebview windows share; a UI thread that does not answer fails the
    page instead of hanging the scan beyond the owner's Cancel."""
    # pythonnet's .NET objects are untyped; pywebview has loaded the CLR by the time a window exists.
    from System import Action  # noqa: PLC0415  # pyright: ignore[reportMissingImports, reportUnknownVariableType]

    native: Any = window.native
    try:
        within(CALL_TIMEOUT_S, lambda: native.Invoke(Action(work)))
    except TimeoutError as error:
        raise LoadTimeout("the app's UI thread did not answer") from error


def _load_user32() -> _Native:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.ClientToScreen.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    return user32


def production_engine(client_factory: Callable[[], httpx.Client], cache: Path, now: Clock) -> BuyEngine:
    """The real windows, session store and shipping tables (cached in `cache`) for the Buy screen."""
    sessions = CardmarketSessionStore()
    pane = CardmarketPane(Win32Ops(), _app_window)

    @contextmanager
    def shipping() -> Generator[RouteSource]:
        with client_factory() as http:
            yield CardmarketShipping(Fetcher(http), cache, now=now)

    return BuyEngine(
        open_browser=lambda handover: open_scan_browser(sessions, handover, pane),
        pane=pane,
        shipping=shipping,
    )
