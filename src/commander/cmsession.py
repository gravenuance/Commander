"""The browser session the Cardmarket scan reads pages through (ADR 0067).

`Browser` is all the scan engine sees: load an address, get the page's HTML. `SessionBrowser` adds what a real
window needs around that: wait for the load event, wait out Cloudflare's check, refuse a page that asks to
sign in, and keep the session cookie in the secret store while signed in. It drives a `PageDriver`; the one
implementation, `WebviewDriver`, holds every pywebview and .NET call so the rest runs without a window.

The owner signs in by hand in `sign_in_window`; the app never sees the password. The session is the
`PHPSESSID` cookie, which WebView2 drops on close, so it is saved to Credential Manager while signed in and
added back before the first page loads.

A page-load or script call can hang for good while Cloudflare swaps its check for the page, so every call into
the window has a time limit, and a window is always closed with `destroy()`: killing the process leaves
WebView2's browser process holding the profile, and the next start shows a white page.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import urlsplit

from commander.cmpages import PageState, page_state
from commander.credentials import (
    CARDMARKET_HOSTS,
    CARDMARKET_SESSION_COOKIE,
    CredentialError,
    SessionCookie,
    SessionStore,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from http.cookies import SimpleCookie

    from webview import Window

# A pythonnet object: its members are known only at run time.
type _DotNet = Any

log = logging.getLogger(__name__)

START_URL = "https://www.cardmarket.com/en/Magic"
DEFAULT_COOKIE_DOMAIN = "www.cardmarket.com"
# The load event may never come; the scan fails the page rather than wait on.
LOAD_TIMEOUT_S = 45.0
# Cloudflare's check passes by itself in about 5 s.
CHALLENGE_WAIT_S = 30.0
CHALLENGE_POLL_S = 1.0
# One script call or UI-thread call; a longer wait is a window that has stopped answering.
CALL_TIMEOUT_S = 10.0
READY_TIMEOUT_S = 45.0
BLANK_PAGE = "<p style='font-family:sans-serif'>Starting…</p>"
_OUTER_HTML = "document.documentElement.outerHTML"
# What a script that clicked a submit button answers.
SUBMITTED = "submitted"


class BrowserError(Exception):
    """The browser could not give the page; the message says why."""


class ChallengeError(BrowserError):
    """Cloudflare's check was still up after the wait."""


class LoadTimeout(BrowserError):  # noqa: N818 - a condition the scan reports, not an error in the usual sense
    """The page did not finish loading, or the window stopped answering."""


class SignedOut(BrowserError):  # noqa: N818 - a condition the scan reports, not an error in the usual sense
    """The page asks to sign in: the session has ended."""


class Browser(Protocol):
    def load(self, url: str) -> str:
        """Open `url` and return the page's HTML once it has loaded and any challenge has passed."""
        ...

    def close(self) -> None: ...


class FormBrowser(Browser, Protocol):
    """A `Browser` that also fills in and submits the forms of the page it holds (the wants list)."""

    def act(self, script: str) -> str:
        """Run `script` on the current page and return the string it answers. The script must not navigate."""
        ...

    def submit(self, script: str) -> str:
        """Run `script`, which clicks a submit button and answers `SUBMITTED`, wait for the page that loads
        in answer and return its HTML, checked like `load`'s."""
        ...


class PageDriver(Protocol):
    """One window, as narrowly as `SessionBrowser` needs it."""

    def navigate(self, url: str, timeout_s: float) -> bool:
        """Start loading `url`; False when the load event did not come within `timeout_s`."""
        ...

    def read_html(self, timeout_s: float) -> str | None:
        """The current page's HTML; None when the window did not answer within `timeout_s`."""
        ...

    def run_script(self, script: str, timeout_s: float) -> str | None:
        """Run `script` in the page; the string it answers, or None when it answered no string in time."""
        ...

    def begin_navigation(self) -> None:
        """Forget any load already seen, so `wait_loaded` waits for the next one. Call before the click."""
        ...

    def wait_loaded(self, timeout_s: float) -> bool:
        """True once a page has loaded since `begin_navigation` (or `navigate`)."""
        ...

    def read_session_cookie(self) -> SessionCookie | None: ...

    def add_session_cookie(self, cookie: SessionCookie) -> None: ...

    def close(self) -> None: ...


def on_cardmarket(url: str | None) -> bool:
    """True for an https page on Cardmarket itself, matched on the parsed host name."""
    parts = urlsplit(url or "")
    return parts.scheme == "https" and (parts.hostname or "").rstrip(".") in CARDMARKET_HOSTS


class SessionKeeper:
    """Restores the saved session into a window and saves the window's session while it is signed in."""

    def __init__(self, driver: PageDriver, store: SessionStore) -> None:
        self._driver = driver
        self._store = store
        # What the store already holds, so an unchanged cookie is not written again.
        self._saved: SessionCookie | None = None

    def restore(self) -> bool:
        """Put the saved cookie into the window; False when none is saved or the store cannot be read."""
        try:
            saved = self._store.get()
        except CredentialError as error:
            log.warning("no Cardmarket session restored: %s", error)
            return False
        if saved is None:
            return False
        self._driver.add_session_cookie(saved)
        self._saved = saved
        return True

    def save(self) -> None:
        """Store the window's session cookie if it differs from the stored one. A failure is logged: the
        scan itself does not need the stored copy."""
        cookie = self._driver.read_session_cookie()
        if cookie is None or cookie == self._saved:
            return
        try:
            self._store.put(cookie)
        except CredentialError as error:
            log.warning("the Cardmarket session was not saved: %s", error)
            return
        self._saved = cookie
        log.info("saved the Cardmarket session cookie")


class SessionBrowser:
    """A `Browser` over a `PageDriver`, signed in with the owner's saved session."""

    def __init__(
        self,
        driver: PageDriver,
        store: SessionStore,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = time.sleep,
    ) -> None:
        self._driver = driver
        self._keeper = SessionKeeper(driver, store)
        self._clock = clock
        self._sleep = sleep

    def restore_session(self) -> bool:
        return self._keeper.restore()

    def load(self, url: str) -> str:
        if not on_cardmarket(url):
            raise ValueError(f"not a Cardmarket address: {url}")
        if not self._driver.navigate(url, LOAD_TIMEOUT_S):
            raise LoadTimeout(f"{url} did not finish loading within {LOAD_TIMEOUT_S:.0f} s")
        return self._settled_page(url)

    def act(self, script: str) -> str:
        answer = self._driver.run_script(script, CALL_TIMEOUT_S)
        if answer is None:
            raise LoadTimeout("the window did not answer the script")
        return answer

    def submit(self, script: str) -> str:
        # The load event is cleared before the click: a click leaves a stale one set, and the page that
        # follows would then be read before it exists.
        self._driver.begin_navigation()
        answer = self._driver.run_script(script, CALL_TIMEOUT_S)
        if answer is not None and answer != SUBMITTED:
            raise BrowserError(f"the page did not take the step: {answer}")
        if answer is None:
            log.warning("the window gave no answer to a submit script; waiting for the page anyway")
        if not self._driver.wait_loaded(LOAD_TIMEOUT_S):
            raise LoadTimeout(f"no page loaded within {LOAD_TIMEOUT_S:.0f} s of the submit")
        return self._settled_page("the page after the submit")

    def _settled_page(self, where: str) -> str:
        """The loaded page's HTML once any challenge has passed; refuses a sign-in page."""
        html = self._wait_out_challenge(where)
        state = page_state(html)
        if state is PageState.SIGNED_OUT:
            raise SignedOut(f"{where} asks to sign in; the Cardmarket session has ended")
        if state is PageState.SIGNED_IN:
            self._keeper.save()
        return html

    def _wait_out_challenge(self, url: str) -> str:
        """The page's HTML once it is not Cloudflare's check, polling each second for up to 30 s."""
        deadline = self._clock() + CHALLENGE_WAIT_S
        html = self._driver.read_html(CALL_TIMEOUT_S)
        while html is None or page_state(html) is PageState.CHALLENGE:
            if self._clock() >= deadline:
                if html is None:
                    raise LoadTimeout(f"{url}: the window stopped answering")
                raise ChallengeError(f"{url}: Cloudflare's check was still up after {CHALLENGE_WAIT_S:.0f} s")
            log.info("waiting for %s: %s", url, "no answer yet" if html is None else "Cloudflare's check")
            self._sleep(CHALLENGE_POLL_S)
            html = self._driver.read_html(CALL_TIMEOUT_S)
        return html

    def close(self) -> None:
        self._driver.close()


def _within[T](timeout_s: float, call: Callable[[], T]) -> T:
    """Run `call` on a worker thread; TimeoutError when it has not finished within `timeout_s`. A call that
    hangs for good is left behind on its daemon thread."""
    outcome: list[T] = []
    failure: list[Exception] = []

    def work() -> None:
        try:
            outcome.append(call())
        except Exception as error:  # noqa: BLE001 - handed to the caller below
            failure.append(error)

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        raise TimeoutError(f"no answer within {timeout_s:.0f} s")
    if failure:
        raise failure[0]
    return outcome[0]


class WebviewDriver:
    """A pywebview window on Cardmarket. WebView2 only takes calls on the window's UI thread, which
    pythonnet's `Invoke` reaches; the other calls pywebview marshals itself."""

    def __init__(self, window: Window) -> None:
        self._window = window

    def wait_ready(self, timeout_s: float = READY_TIMEOUT_S) -> bool:
        """True once the window has loaded its first page, so WebView2 is up."""
        return self._window.events.loaded.wait(timeout_s)

    def configure(self) -> None:
        """Let WebView2 offer to save the password, as Edge does (encrypted in the profile): signing in again
        after the server ends the session is then one click."""

        def apply(core: _DotNet) -> None:
            core.Settings.IsPasswordAutosaveEnabled = True
            core.Settings.IsGeneralAutofillEnabled = True

        self._on_ui_thread(apply)

    def add_session_cookie(self, cookie: SessionCookie) -> None:
        def apply(core: _DotNet) -> None:
            made: Any = core.CookieManager.CreateCookie(cookie.name, cookie.value, cookie.domain, cookie.path)
            made.IsSecure = True
            made.IsHttpOnly = True
            core.CookieManager.AddOrUpdateCookie(made)

        self._on_ui_thread(apply)

    def navigate(self, url: str, timeout_s: float) -> bool:
        loaded = self._window.events.loaded
        loaded.clear()
        # pywebview's load_url sets the control's Source, which WebView2 ignores when it already holds that
        # address: reloading the wants list after a form posted back to it never loaded (live, 2026-11-09).
        # CoreWebView2.Navigate always loads.
        try:
            self._on_ui_thread(lambda core: core.Navigate(url))
        except LoadTimeout:
            log.warning("the window did not start loading %s", url)
            return False
        return loaded.wait(timeout_s)

    def read_html(self, timeout_s: float) -> str | None:
        try:
            html = _within(timeout_s, lambda: self._window.evaluate_js(_OUTER_HTML))
        except TimeoutError:
            return None
        return html if isinstance(html, str) else None

    def run_script(self, script: str, timeout_s: float) -> str | None:
        try:
            answer = _within(timeout_s, lambda: self._window.evaluate_js(script))
        except TimeoutError:
            return None
        return answer if isinstance(answer, str) else None

    def begin_navigation(self) -> None:
        # Only load_url or this clears the event; a click that navigates leaves it set.
        self._window.events.loaded.clear()

    def wait_loaded(self, timeout_s: float) -> bool:
        return self._window.events.loaded.wait(timeout_s)

    def read_session_cookie(self) -> SessionCookie | None:
        try:
            # pywebview leaves get_cookies untyped; on WebView2 it returns one SimpleCookie per cookie.
            jars = cast(
                "list[SimpleCookie]",
                _within(CALL_TIMEOUT_S, self._window.get_cookies),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            )
        except TimeoutError:
            log.warning("the window did not give its cookies")
            return None
        for jar in jars:
            morsel = jar.get(CARDMARKET_SESSION_COOKIE)
            if morsel is None or not morsel.value:
                continue
            try:
                return SessionCookie(
                    morsel.key, morsel.value, morsel["domain"] or DEFAULT_COOKIE_DOMAIN, morsel["path"] or "/"
                )
            except CredentialError as error:
                log.warning("not keeping the session cookie: %s", error)
        return None

    def current_url(self) -> str | None:
        return self._window.get_current_url()

    def close(self) -> None:
        self._window.destroy()

    def _on_ui_thread(self, work: Callable[[_DotNet], None]) -> None:
        # pythonnet's .NET objects are untyped; pywebview has loaded the CLR by the time a window exists.
        from System import Action  # noqa: PLC0415  # pyright: ignore[reportMissingImports, reportUnknownVariableType]

        native: Any = cast("Any", self._window.native)

        def on_ui_thread() -> None:
            core: Any = native.webview.CoreWebView2
            if core is None:
                raise BrowserError("WebView2 is not ready")
            work(core)

        try:
            _within(CALL_TIMEOUT_S, lambda: native.Invoke(Action(on_ui_thread)))
        except TimeoutError as error:
            raise LoadTimeout("the window's UI thread did not answer") from error


class WebviewBrowser(SessionBrowser):
    """A `SessionBrowser` on a pywebview window that already exists (the hidden scan window)."""

    def __init__(
        self,
        window: Window,
        store: SessionStore,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = time.sleep,
    ) -> None:
        driver = WebviewDriver(window)
        super().__init__(driver, store, clock=clock, sleep=sleep)
        if not driver.wait_ready():
            raise LoadTimeout("the Cardmarket window did not start")
        driver.configure()
        self.restore_session()


@dataclass(slots=True)
class SignInWindow:
    window: Window

    def close(self) -> None:
        self.window.destroy()


def sign_in_window(
    store: SessionStore,
    on_signed_in: Callable[[], None],
    *,
    title: str = "Sign in to Cardmarket",
) -> SignInWindow:
    """Open a visible window on Cardmarket for the owner to sign in by hand (password, 2FA, any check).

    `on_signed_in` runs on a pywebview thread each time a page that loads shows a signed-in session (a
    logout link), after the session cookie has been saved. Call from the thread pywebview started, not
    from the GUI loop itself; close the result with `close()`.
    """
    import webview  # noqa: PLC0415 - loads .NET, so only when a window is wanted

    # pywebview types its url parameter with an untyped callable.
    window = webview.create_window(title, html=BLANK_PAGE, width=1100, height=850)  # pyright: ignore[reportUnknownMemberType]
    if window is None:
        raise BrowserError("pywebview created no window")
    driver = WebviewDriver(window)
    keeper = SessionKeeper(driver, store)

    def on_loaded() -> None:
        if not on_cardmarket(driver.current_url()):
            return
        html = driver.read_html(CALL_TIMEOUT_S)
        if html is not None and page_state(html) is PageState.SIGNED_IN:
            keeper.save()
            on_signed_in()

    try:
        _open_start_page(window, driver, keeper, on_loaded)
    except BaseException:
        window.destroy()
        raise
    return SignInWindow(window)


def _open_start_page(
    window: Window, driver: WebviewDriver, keeper: SessionKeeper, on_loaded: Callable[[], None]
) -> None:
    """Once WebView2 is up: password autofill on, the saved session in, then Cardmarket's front page."""
    if not driver.wait_ready():
        raise LoadTimeout("the sign-in window did not start")
    driver.configure()
    keeper.restore()
    window.events.loaded += on_loaded
    if not driver.navigate(START_URL, LOAD_TIMEOUT_S):
        raise LoadTimeout("Cardmarket did not load in the sign-in window")
