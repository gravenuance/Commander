"""The browser session the Cardmarket scan reads pages through (ADR 0067).

`Browser` is all the scan engine sees: load an address, get the page's HTML. `SessionBrowser` adds what a real
window needs around that: wait for the load event, wait out Cloudflare's check, refuse a page that asks to
sign in, and keep the session cookie in the secret store while signed in. It drives a `PageDriver`; the one
implementation, `WebviewDriver`, holds every pywebview and .NET call so the rest runs without a window.

When a page is signed out or Cloudflare's check does not pass by itself, the browser asks its `Handover` to
show the window to the owner, waits until they have signed in or the check has passed, and loads the page
again; the app never sees the password. The session is the `PHPSESSID` cookie, which WebView2 drops on close,
so it is saved to Credential Manager while signed in and added back before the first page loads.

A page-load or script call can hang for good while Cloudflare swaps its check for the page, so every call into
the window has a time limit, and a window is always closed with `destroy()`: killing the process leaves
WebView2's browser process holding the profile, and the next start shows a white page.
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum
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

DEFAULT_COOKIE_DOMAIN = "www.cardmarket.com"
# The load event may never come; the scan fails the page rather than wait on.
LOAD_TIMEOUT_S = 45.0
# Cloudflare's check passes by itself in about 5 s; without a handover the wait is the longer one.
CHALLENGE_WAIT_S = 30.0
CHALLENGE_SELF_S = 8.0
CHALLENGE_POLL_S = 1.0
HANDOVER_POLL_S = 1.0
# A wait for the load event is made in slices this long, so that a cancel is heard between them.
LOAD_POLL_S = 1.0
# Reads in a row that a window may leave unanswered during a handover; each leaves a thread behind.
SILENT_READS_MAX = 5
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


class Cancelled(BrowserError):  # noqa: N818 - a condition the scan reports, not an error in the usual sense
    """The owner cancelled while the window was handed over to them."""


class Interrupted(BrowserError):  # noqa: N818 - a condition the scan reports, not an error in the usual sense
    """A sign-in or check came in the middle of a form step, so the step must be done again."""


class Need(Enum):
    """What the owner must do in the window the browser hands over."""

    SIGN_IN = "signin"
    CHECK = "check"


class Handover(Protocol):
    """Hands the window to the owner when a page needs them (the Buy screen shows it over its own area)."""

    def show(self, need: Need) -> None:
        """Show the window; called again when the need changes while it is shown."""
        ...

    def hide(self) -> None: ...

    def cancelled(self) -> bool:
        """True once the owner has asked to stop."""
        ...


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

    def navigate(self, url: str) -> bool:
        """Forget any load already seen and start loading `url`; False when the window did not start it. The
        load event is then waited for with `wait_loaded`."""
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
        """True once a page has loaded since `begin_navigation` (or `navigate`), waiting up to `timeout_s`."""
        ...

    def read_session_cookie(self) -> SessionCookie | None: ...

    def add_session_cookie(self, cookie: SessionCookie) -> None: ...

    def close(self) -> None: ...


def on_cardmarket(url: str | None) -> bool:
    """True for an https page on Cardmarket itself, matched on the parsed host name."""
    parts = urlsplit(url or "")
    return parts.scheme == "https" and (parts.hostname or "").rstrip(".") in CARDMARKET_HOSTS


class RequestPacer:
    """When the last request to Cardmarket ended, so that the gap between two requests holds whichever step
    makes them. A `SessionBrowser` owns one, and the wants sync and the scan both pace against it."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last: float | None = None

    def mark(self) -> None:
        """Call when a request has ended."""
        self._last = self._clock()

    def wait(self, draw_gap: Callable[[], float], sleep: Callable[[float], object]) -> None:
        """Before a request: `sleep` what is left of the gap `draw_gap` gives (drawn anew each time) since the
        last request ended; nothing, and no draw, before the first request."""
        if self._last is None:
            return
        left = draw_gap() - (self._clock() - self._last)
        if left > 0:
            sleep(left)


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
    """A `Browser` over a `PageDriver`, signed in with the owner's saved session.

    With a `handover`, a signed-out page or a check that does not pass by itself is left to the owner; without
    one it is an error. `cancel` answers True once the owner has asked to stop (the handover's own answer
    counts too): it is heard while a page loads and while a check is waited out, and ends them with
    `Cancelled`. `pacer` keeps the time of the last request for every step that loads through this browser.
    """

    def __init__(
        self,
        driver: PageDriver,
        store: SessionStore,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = time.sleep,
        handover: Handover | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> None:
        self._driver = driver
        self._keeper = SessionKeeper(driver, store)
        self._clock = clock
        self._sleep = sleep
        self._handover = handover
        self._cancel = cancel
        self.pacer = RequestPacer(clock)

    def restore_session(self) -> bool:
        return self._keeper.restore()

    def load(self, url: str) -> str:
        if not on_cardmarket(url):
            raise ValueError(f"not a Cardmarket address: {url}")
        while True:
            if not self._driver.navigate(url):
                raise LoadTimeout(f"the window did not start loading {url}")
            if not self._wait_loaded(LOAD_TIMEOUT_S, url):
                raise LoadTimeout(f"{url} did not finish loading within {LOAD_TIMEOUT_S:.0f} s")
            html = self._settled_page(url)
            if html is not None:
                return html

    def _cancelled(self) -> bool:
        if self._cancel is not None and self._cancel():
            return True
        return self._handover is not None and self._handover.cancelled()

    def _check_cancel(self, where: str) -> None:
        if self._cancelled():
            raise Cancelled(f"cancelled while {where} was loading")

    def _wait_loaded(self, timeout_s: float, where: str) -> bool:
        """True once the page has loaded. Waits in slices, hearing a cancel between them."""
        waited = 0.0
        while waited < timeout_s:
            slice_s = min(LOAD_POLL_S, timeout_s - waited)
            if self._driver.wait_loaded(slice_s):
                return True
            waited += slice_s
            self._check_cancel(where)
        return False

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
        if not self._wait_loaded(LOAD_TIMEOUT_S, "the page after the submit"):
            raise LoadTimeout(f"no page loaded within {LOAD_TIMEOUT_S:.0f} s of the submit")
        page = self._settled_page("the page after the submit")
        if page is None:
            raise Interrupted("a sign-in or check came after the submit; the step must be done again")
        return page

    def _settled_page(self, where: str) -> str | None:
        """The loaded page's HTML once any challenge has passed; None after the owner was handed the window
        (the page is then another one). Without a handover a sign-in page raises."""
        html = self._wait_out_challenge(where)
        state = page_state(html)
        if state is PageState.SIGNED_IN:
            self._keeper.save()
        if state is PageState.CHALLENGE:
            self._hand_over(Need.CHECK, where)
            return None
        if state is PageState.SIGNED_OUT:
            if self._handover is None:
                raise SignedOut(f"{where} asks to sign in; the Cardmarket session has ended")
            self._hand_over(Need.SIGN_IN, where)
            return None
        return html

    def _wait_out_challenge(self, url: str) -> str:
        """The page's HTML once it is not Cloudflare's check, polling each second. A check still up after the
        wait is returned for the handover, or fails the page when there is none."""
        self_wait = CHALLENGE_WAIT_S if self._handover is None else CHALLENGE_SELF_S
        start = self._clock()
        html = self._driver.read_html(CALL_TIMEOUT_S)
        while html is None or page_state(html) is PageState.CHALLENGE:
            waited = self._clock() - start
            if html is None and waited >= CHALLENGE_WAIT_S:
                raise LoadTimeout(f"{url}: the window stopped answering")
            if html is not None and waited >= self_wait:
                if self._handover is None:
                    raise ChallengeError(f"{url}: Cloudflare's check was still up after {self_wait:.0f} s")
                return html
            log.info("waiting for %s: %s", url, "no answer yet" if html is None else "Cloudflare's check")
            self._sleep(CHALLENGE_POLL_S)
            self._check_cancel(url)
            html = self._driver.read_html(CALL_TIMEOUT_S)
        return html

    def _hand_over(self, need: Need, where: str) -> None:
        """Show the window to the owner until the page is signed in; they end it with the screen's Cancel."""
        handover = self._handover
        if handover is None:
            raise ValueError("no handover to show the window with")
        log.info("%s needs the owner: %s", where, need.value)
        handover.show(need)
        silent = 0
        try:
            while True:
                self._sleep(HANDOVER_POLL_S)
                if self._cancelled():
                    raise Cancelled(f"cancelled while {where} waited for the owner")
                html = self._driver.read_html(CALL_TIMEOUT_S)
                if html is None:
                    # Each unanswered read leaves its thread behind, so a window that has gone is given up.
                    silent += 1
                    if silent >= SILENT_READS_MAX:
                        raise LoadTimeout(f"{where}: the window stopped answering during the handover")
                    continue
                silent = 0
                state = page_state(html)
                if state is PageState.SIGNED_IN:
                    self._keeper.save()
                    return
                shown = {PageState.CHALLENGE: Need.CHECK, PageState.SIGNED_OUT: Need.SIGN_IN}.get(state, need)
                if shown is not need:
                    need = shown
                    handover.show(need)
        finally:
            handover.hide()

    def close(self) -> None:
        self._driver.close()


def pacer_for(browser: Browser, clock: Callable[[], float] = time.monotonic) -> RequestPacer:
    """The pacer the steps using `browser` share: a `SessionBrowser`'s own, or for any other browser (a test's
    fake) a new one on `clock`."""
    return browser.pacer if isinstance(browser, SessionBrowser) else RequestPacer(clock)


def within[T](timeout_s: float, call: Callable[[], T]) -> T:
    """Run `call` on a worker thread; TimeoutError when it has not finished within `timeout_s`. Whatever the
    call raises, a SystemExit included, is raised again to the caller. A call that hangs for good is left
    behind on its daemon thread."""
    outcome: list[T] = []
    failure: list[BaseException] = []

    def work() -> None:
        try:
            outcome.append(call())
        except BaseException as error:  # noqa: BLE001 - raised again in the caller's thread below
            failure.append(error)

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        raise TimeoutError(f"no answer within {timeout_s:.0f} s")
    if failure:
        raise failure[0]
    return outcome[0]


def _within_window[T](timeout_s: float, call: Callable[[], T], what: str) -> T:
    """`within` for a call into the window: what the call throws (pywebview's `JavascriptException` and
    `WebViewException`, a .NET exception) is a `BrowserError` that keeps it as its cause, so the scan can
    stop with what it has read. `TimeoutError` passes for the caller to map."""
    try:
        return within(timeout_s, call)
    except (TimeoutError, BrowserError):
        raise
    except Exception as error:
        raise BrowserError(f"{what} failed: {error}") from error


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

    def navigate(self, url: str) -> bool:
        # Cleared before the navigation starts: a load event left over from the last page would otherwise be
        # taken for this one.
        self._window.events.loaded.clear()
        # pywebview's load_url sets the control's Source, which WebView2 ignores when it already holds that
        # address: reloading the wants list after a form posted back to it never loaded (live, 2026-11-09).
        # CoreWebView2.Navigate always loads.
        try:
            self._on_ui_thread(lambda core: core.Navigate(url))
        except LoadTimeout:
            log.warning("the window did not start loading %s", url)
            return False
        return True

    def read_html(self, timeout_s: float) -> str | None:
        try:
            html = _within_window(timeout_s, lambda: self._window.evaluate_js(_OUTER_HTML), "the page read")
        except TimeoutError:
            return None
        return html if isinstance(html, str) else None

    def run_script(self, script: str, timeout_s: float) -> str | None:
        try:
            answer = _within_window(timeout_s, lambda: self._window.evaluate_js(script), "the script")
        except TimeoutError:
            return None
        return answer if isinstance(answer, str) else None

    def begin_navigation(self) -> None:
        # Only load_url or this clears the event; a click that navigates leaves it set.
        self._window.events.loaded.clear()

    def wait_loaded(self, timeout_s: float) -> bool:
        return self._window.events.loaded.wait(timeout_s)

    def read_session_cookie(self) -> SessionCookie | None:
        # pywebview leaves get_cookies untyped; on WebView2 it returns one SimpleCookie per cookie.
        get_cookies = cast(
            "Callable[[], list[SimpleCookie]]",
            self._window.get_cookies,  # pyright: ignore[reportUnknownMemberType]
        )
        try:
            jars = _within_window(CALL_TIMEOUT_S, get_cookies, "reading the cookies")
        except TimeoutError:
            log.warning("the window did not give its cookies")
            return None
        except BrowserError as error:
            # The stored copy of the session is not needed by the page being read.
            log.warning("the window could not give its cookies: %s", error)
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

    def close(self) -> None:
        self._window.destroy()

    def _on_ui_thread(self, work: Callable[[_DotNet], None]) -> None:
        """Run `work` on the window's UI thread, or inline when already there, where a marshalled call would
        wait for the thread it blocks."""
        native: Any = cast("Any", self._window.native)

        def on_ui_thread() -> None:
            core: Any = native.webview.CoreWebView2
            if core is None:
                raise BrowserError("WebView2 is not ready")
            work(core)

        if not native.InvokeRequired:
            on_ui_thread()
            return
        # pythonnet's .NET objects are untyped; pywebview has loaded the CLR by the time a window exists.
        from System import (  # noqa: PLC0415  # pyright: ignore[reportMissingImports]
            Action,  # pyright: ignore[reportUnknownVariableType]
        )

        try:
            _within_window(CALL_TIMEOUT_S, lambda: native.Invoke(Action(on_ui_thread)), "the UI thread call")
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
        handover: Handover | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> None:
        driver = WebviewDriver(window)
        super().__init__(driver, store, clock=clock, sleep=sleep, handover=handover, cancel=cancel)
        if not driver.wait_ready():
            raise LoadTimeout("the Cardmarket window did not start")
        driver.configure()
        self.restore_session()
