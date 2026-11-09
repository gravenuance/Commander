"""`within` and `WebviewDriver` against a fake pywebview window (its UI-thread call, which needs .NET, is
overridden)."""

import sys
import threading
import types
from collections.abc import Callable
from http.cookies import SimpleCookie
from typing import TYPE_CHECKING, Any, cast

import pytest
from webview.errors import JavascriptException, WebViewException

from commander import cmsession
from commander.cmscan import ScanRequest, ScanStatus, scan
from commander.cmsession import BrowserError, LoadTimeout, SessionBrowser, WebviewDriver, within
from commander.credentials import CredentialError, SessionCookie
from commander.sellersolve import WantedCard

if TYPE_CHECKING:
    from webview import Window

HTML = "<html><body>Sol Ring</body></html>"


class Events:
    def __init__(self) -> None:
        self.loaded = threading.Event()


class FakeWindow:
    """The part of pywebview's `Window` the driver uses. `evaluate_js` answers `answer`, raises `error` or,
    when `hang` is set, blocks until the test releases it."""

    def __init__(self) -> None:
        self.events = Events()
        self.answer: object = HTML
        self.error: BaseException | None = None
        self.hang: threading.Event | None = None
        self.scripts: list[str] = []
        self.jars: list[SimpleCookie] = []
        self.cookie_error: Exception | None = None
        self.destroyed = False

    def evaluate_js(self, script: str) -> object:
        self.scripts.append(script)
        if self.hang is not None:
            self.hang.wait(5)
        if self.error is not None:
            raise self.error
        return self.answer

    def get_cookies(self) -> list[SimpleCookie]:
        if self.cookie_error is not None:
            raise self.cookie_error
        return self.jars

    def destroy(self) -> None:
        self.destroyed = True


class UiLessDriver(WebviewDriver):
    """A driver whose UI-thread call runs `on_ui` instead of reaching .NET."""

    def __init__(self, window: FakeWindow, on_ui: Callable[[], None] = lambda: None) -> None:
        super().__init__(cast("Window", window))
        self.on_ui = on_ui

    def _on_ui_thread(self, work: Callable[[Any], None]) -> None:  # noqa: ARG002 - the override's parameter
        self.on_ui()


def jar(name: str, value: str, domain: str = "", path: str = "") -> SimpleCookie:
    cookies: SimpleCookie = SimpleCookie()
    cookies[name] = value
    cookies[name]["domain"] = domain
    cookies[name]["path"] = path
    return cookies


class TestWithin:
    def test_it_returns_what_the_call_returns(self) -> None:
        assert within(1.0, lambda: 42) == 42

    def test_a_failure_of_the_call_is_raised_to_the_caller(self) -> None:
        def fail() -> int:
            raise KeyError("gone")

        with pytest.raises(KeyError, match="gone"):
            within(1.0, fail)

    def test_a_call_that_does_not_finish_in_time_is_a_timeout(self) -> None:
        release = threading.Event()

        with pytest.raises(TimeoutError, match="no answer within"):
            within(0.05, lambda: release.wait(5))

        release.set()

    def test_a_system_exit_in_the_call_is_raised_to_the_caller_not_swallowed(self) -> None:
        def leave() -> int:
            raise SystemExit(3)

        with pytest.raises(SystemExit):
            within(1.0, leave)

    def test_a_keyboard_interrupt_in_a_window_call_is_not_turned_into_a_browser_error(self) -> None:
        window = FakeWindow()
        window.error = KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            UiLessDriver(window).read_html(1.0)


class TestReadHtml:
    def test_it_returns_the_documents_html(self) -> None:
        window = FakeWindow()

        assert UiLessDriver(window).read_html(1.0) == HTML
        assert window.scripts == ["document.documentElement.outerHTML"]

    def test_a_window_that_does_not_answer_gives_none(self) -> None:
        window = FakeWindow()
        window.hang = threading.Event()

        try:
            assert UiLessDriver(window).read_html(0.05) is None
        finally:
            window.hang.set()

    def test_an_answer_that_is_not_text_gives_none(self) -> None:
        window = FakeWindow()
        window.answer = None

        assert UiLessDriver(window).read_html(1.0) is None

    @pytest.mark.parametrize("error", [JavascriptException("boom"), WebViewException("not ready")])
    def test_a_failing_script_call_is_a_browser_error_keeping_its_cause(self, error: Exception) -> None:
        window = FakeWindow()
        window.error = error

        with pytest.raises(BrowserError) as caught:
            UiLessDriver(window).read_html(1.0)

        assert caught.value.__cause__ is error


class TestRunScript:
    def test_it_returns_the_string_the_script_answers(self) -> None:
        window = FakeWindow()
        window.answer = "ok"

        assert UiLessDriver(window).run_script("1 + 1", 1.0) == "ok"
        assert window.scripts == ["1 + 1"]

    def test_a_window_that_does_not_answer_gives_none(self) -> None:
        window = FakeWindow()
        window.hang = threading.Event()

        try:
            assert UiLessDriver(window).run_script("1 + 1", 0.05) is None
        finally:
            window.hang.set()

    def test_an_answer_that_is_not_text_gives_none(self) -> None:
        window = FakeWindow()
        window.answer = 3

        assert UiLessDriver(window).run_script("1 + 1", 1.0) is None

    def test_a_script_that_throws_is_a_browser_error_keeping_its_cause(self) -> None:
        window = FakeWindow()
        window.error = JavascriptException("TypeError: null")

        with pytest.raises(BrowserError, match="TypeError: null") as caught:
            UiLessDriver(window).run_script("el.click()", 1.0)

        assert caught.value.__cause__ is window.error


class TestNavigate:
    def test_a_load_event_left_over_from_an_earlier_page_is_cleared_before_navigating(self) -> None:
        window = FakeWindow()
        window.events.loaded.set()
        seen: list[bool] = []
        driver = UiLessDriver(window, lambda: seen.append(window.events.loaded.is_set()))

        assert driver.navigate("https://www.cardmarket.com/en/Magic") is True

        assert seen == [False]
        assert driver.wait_loaded(0.01) is False

    def test_the_load_event_that_follows_the_navigation_is_waited_for(self) -> None:
        window = FakeWindow()
        driver = UiLessDriver(window, window.events.loaded.set)

        driver.navigate("https://www.cardmarket.com/en/Magic")

        assert driver.wait_loaded(0.01) is True

    def test_a_ui_thread_that_does_not_answer_is_a_navigation_that_did_not_start(self) -> None:
        def stuck() -> None:
            raise LoadTimeout("the window's UI thread did not answer")

        assert UiLessDriver(FakeWindow(), stuck).navigate("https://www.cardmarket.com/en/Magic") is False

    def test_begin_navigation_clears_the_load_event(self) -> None:
        window = FakeWindow()
        window.events.loaded.set()

        driver = UiLessDriver(window)
        driver.begin_navigation()

        assert driver.wait_loaded(0.01) is False


class TestReadSessionCookie:
    def test_the_cookie_comes_with_its_domain_and_path(self) -> None:
        window = FakeWindow()
        window.jars = [jar("PHPSESSID", "abc", ".cardmarket.com", "/en")]

        assert UiLessDriver(window).read_session_cookie() == SessionCookie(
            "PHPSESSID", "abc", ".cardmarket.com", "/en"
        )

    def test_a_cookie_without_domain_or_path_gets_the_site_and_the_root(self) -> None:
        window = FakeWindow()
        window.jars = [jar("PHPSESSID", "abc")]

        assert UiLessDriver(window).read_session_cookie() == SessionCookie(
            "PHPSESSID", "abc", "www.cardmarket.com", "/"
        )

    def test_the_session_cookie_is_found_among_other_cookies_and_empty_ones_are_skipped(self) -> None:
        window = FakeWindow()
        window.jars = [jar("cm_TD", "x"), jar("PHPSESSID", ""), jar("PHPSESSID", "abc")]

        cookie = UiLessDriver(window).read_session_cookie()

        assert cookie is not None
        assert cookie.value == "abc"

    def test_no_session_cookie_gives_none(self) -> None:
        window = FakeWindow()
        window.jars = [jar("cm_TD", "x")]

        assert UiLessDriver(window).read_session_cookie() is None

    def test_a_cookie_for_another_site_is_not_kept(self, caplog: pytest.LogCaptureFixture) -> None:
        window = FakeWindow()
        window.jars = [jar("PHPSESSID", "abc", "evil.example", "/")]

        assert UiLessDriver(window).read_session_cookie() is None
        assert "not keeping the session cookie" in caplog.text

    def test_a_window_that_does_not_answer_gives_none(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        window = FakeWindow()
        hang = threading.Event()
        window.hang = hang
        monkeypatch.setattr(cmsession, "CALL_TIMEOUT_S", 0.05)
        monkeypatch.setattr(window, "get_cookies", lambda: hang.wait(5))

        assert UiLessDriver(window).read_session_cookie() is None
        hang.set()
        assert "did not give its cookies" in caplog.text

    def test_a_window_that_fails_gives_none_and_says_so(self, caplog: pytest.LogCaptureFixture) -> None:
        window = FakeWindow()
        window.cookie_error = WebViewException("not ready")

        assert UiLessDriver(window).read_session_cookie() is None
        assert "could not give its cookies" in caplog.text


class FakeCore:
    """WebView2's core, as far as the driver reaches it."""

    def __init__(self) -> None:
        self.Settings = types.SimpleNamespace(IsPasswordAutosaveEnabled=False, IsGeneralAutofillEnabled=False)
        self.navigated: list[str] = []

    def Navigate(self, url: str) -> None:  # noqa: N802 - .NET's name
        self.navigated.append(url)


class FakeNative:
    """The window's form: `on_ui` says whether the caller already is on the UI thread; `Invoke` records the
    marshalled calls and runs them where it is called, as the UI thread would."""

    def __init__(self, *, on_ui: bool, ready: bool = True) -> None:
        self.on_ui = on_ui
        self.invokes = 0
        self.core = FakeCore()
        self.webview = types.SimpleNamespace(CoreWebView2=self.core if ready else None)

    @property
    def InvokeRequired(self) -> bool:  # noqa: N802 - .NET's name
        return not self.on_ui

    def Invoke(self, action: Callable[[], None]) -> None:  # noqa: N802 - .NET's name
        self.invokes += 1
        action()


class TestOnTheUiThread:
    @staticmethod
    def driver(native: FakeNative, monkeypatch: pytest.MonkeyPatch) -> WebviewDriver:
        window = FakeWindow()
        vars(window)["native"] = native
        dotnet = types.ModuleType("System")
        vars(dotnet)["Action"] = lambda work: work
        monkeypatch.setitem(sys.modules, "System", dotnet)
        return WebviewDriver(cast("Window", window))

    def test_a_call_made_from_the_ui_thread_runs_there_instead_of_waiting_on_itself(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        native = FakeNative(on_ui=True)

        self.driver(native, monkeypatch).configure()

        assert native.invokes == 0
        assert native.core.Settings.IsPasswordAutosaveEnabled is True
        assert native.core.Settings.IsGeneralAutofillEnabled is True

    def test_a_call_made_from_another_thread_is_marshalled_to_the_ui_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        native = FakeNative(on_ui=False)

        assert self.driver(native, monkeypatch).navigate("https://www.cardmarket.com/en/Magic") is True

        assert native.invokes == 1
        assert native.core.navigated == ["https://www.cardmarket.com/en/Magic"]

    def test_a_window_whose_webview_is_not_ready_fails_the_call_on_either_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for on_ui in (True, False):
            native = FakeNative(on_ui=on_ui, ready=False)

            with pytest.raises(BrowserError, match="WebView2 is not ready"):
                self.driver(native, monkeypatch).configure()


class NoStore:
    def get(self) -> SessionCookie | None:
        return None

    def put(self, cookie: SessionCookie) -> None:  # noqa: ARG002 - the protocol's name
        raise CredentialError("not expected")

    def delete(self) -> bool:
        return False


def test_a_scan_whose_window_throws_stops_with_what_it_read() -> None:
    not_found = (
        "<html><head><title>x</title></head><body><main>"
        '<h1 class="display-1"><span>404</span><span class="visually-hidden">Page not found</span></h1>'
        "</main></body></html>"
    )
    window = FakeWindow()
    answers: list[object] = [not_found, JavascriptException("boom")]

    def evaluate(_script: str) -> object:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    window.evaluate_js = evaluate  # type: ignore[method-assign]  # the fake answers from a queue here
    browser = SessionBrowser(UiLessDriver(window, window.events.loaded.set), NoStore())

    result = scan(
        browser,
        ScanRequest([WantedCard("Card A", 1), WantedCard("Card B", 1)]),
        sleep=lambda _: None,
    )

    assert result.status is ScanStatus.STOPPED
    assert result.pages_read == 1
    assert result.not_found == ("Card A",)
    assert result.stop_reason is not None
    assert result.stop_reason.startswith("Card B: ")
    assert "boom" in result.stop_reason


def test_close_destroys_the_window() -> None:
    window = FakeWindow()

    UiLessDriver(window).close()

    assert window.destroyed is True
