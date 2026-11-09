import logging
from collections.abc import Callable, Sequence

import pytest

from commander.cmsession import (
    SUBMITTED,
    BrowserError,
    Cancelled,
    ChallengeError,
    Interrupted,
    LoadTimeout,
    Need,
    SessionBrowser,
    SignedOut,
    on_cardmarket,
)
from commander.credentials import CredentialError, SessionCookie

URL = "https://www.cardmarket.com/en/Magic/Cards/Sol-Ring"
CHALLENGE = "<html><head><title>Just a moment...</title></head><body></body></html>"
SIGN_IN = (
    "<html><head><title>Cardmarket</title></head><body><form><input name='username'>"
    "<input type='password'></form></body></html>"
)
SIGNED_IN = (
    "<html><body><a href='/en/Magic/PostGetAction/User_Logout'>Log out</a><main>Sol Ring</main></body></html>"
)
NEUTRAL = "<html><body><main>Sol Ring</main></body></html>"
FIRST = SessionCookie("PHPSESSID", "first", "www.cardmarket.com", "/")
SECOND = SessionCookie("PHPSESSID", "second", "www.cardmarket.com", "/")


class FakeDriver:
    """Answers `read_html` from a script (the last answer repeats) and holds one session cookie."""

    def __init__(self, answers: Sequence[str | None], *, loads: bool = True) -> None:
        self.answers = answers
        self.loads = loads
        self.navigated: list[str] = []
        self.reads = 0
        self.cookie: SessionCookie | None = None
        self.added: list[SessionCookie] = []
        self.closed = False
        # What run_script answers, and the order of the calls the submit makes.
        self.script_answer: str | None = SUBMITTED
        self.scripts: list[str] = []
        self.calls: list[str] = []
        # The timeout of each wait for the load event, and a hook that runs at each.
        self.loaded_waits: list[float] = []
        self.on_wait: Callable[[], None] = lambda: None

    def run_script(self, script: str, timeout_s: float) -> str | None:  # noqa: ARG002 - the protocol's name
        self.scripts.append(script)
        self.calls.append("script")
        return self.script_answer

    def begin_navigation(self) -> None:
        self.calls.append("begin")

    def wait_loaded(self, timeout_s: float) -> bool:
        self.calls.append("wait")
        self.loaded_waits.append(timeout_s)
        self.on_wait()
        return self.loads

    def navigate(self, url: str) -> bool:
        self.navigated.append(url)
        return True

    def read_html(self, timeout_s: float) -> str | None:  # noqa: ARG002 - the protocol's parameter name
        answer = self.answers[min(self.reads, len(self.answers) - 1)]
        self.reads += 1
        return answer

    def read_session_cookie(self) -> SessionCookie | None:
        return self.cookie

    def add_session_cookie(self, cookie: SessionCookie) -> None:
        self.added.append(cookie)

    def close(self) -> None:
        self.closed = True


class FakeStore:
    def __init__(self, cookie: SessionCookie | None = None) -> None:
        self.cookie = cookie
        self.puts: list[SessionCookie] = []
        self.fail_reading = False
        self.fail_writing = False

    def get(self) -> SessionCookie | None:
        if self.fail_reading:
            raise CredentialError("locked")
        return self.cookie

    def put(self, cookie: SessionCookie) -> None:
        if self.fail_writing:
            raise CredentialError("locked")
        self.puts.append(cookie)
        self.cookie = cookie

    def delete(self) -> bool:
        return False


class FakeClock:
    """A clock that only moves when the browser sleeps."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def browser_of(
    driver: FakeDriver, store: FakeStore | None = None, clock: FakeClock | None = None
) -> SessionBrowser:
    clock = clock or FakeClock()
    return SessionBrowser(driver, store or FakeStore(), clock=clock, sleep=clock.sleep)


class TestLoad:
    def test_it_returns_the_page_after_loading_the_address(self) -> None:
        driver = FakeDriver([NEUTRAL])

        assert browser_of(driver).load(URL) == NEUTRAL
        assert driver.navigated == [URL]

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/en/Magic",
            "http://www.cardmarket.com/en/Magic",
            "https://www.cardmarket.com.evil.com/en/Magic",
            "file:///C:/secrets.txt",
            "",
        ],
    )
    def test_an_address_off_cardmarket_is_refused_before_anything_loads(self, url: str) -> None:
        driver = FakeDriver([NEUTRAL])

        with pytest.raises(ValueError, match="not a Cardmarket address"):
            browser_of(driver).load(url)

        assert driver.navigated == []

    def test_a_missing_load_event_is_a_timeout(self) -> None:
        driver = FakeDriver([NEUTRAL], loads=False)

        with pytest.raises(LoadTimeout, match="did not finish loading within 45 s"):
            browser_of(driver).load(URL)

        assert driver.reads == 0

    def test_a_page_that_asks_to_sign_in_is_signed_out(self) -> None:
        store = FakeStore()
        driver = FakeDriver([SIGN_IN])
        driver.cookie = FIRST

        with pytest.raises(SignedOut, match="session has ended"):
            browser_of(driver, store).load(URL)

        assert store.puts == []

    def test_close_closes_the_window(self) -> None:
        driver = FakeDriver([NEUTRAL])

        browser_of(driver).close()

        assert driver.closed is True


class TestAct:
    def test_it_returns_what_the_script_answers(self) -> None:
        driver = FakeDriver([NEUTRAL])
        driver.script_answer = "ok"

        assert browser_of(driver).act("1 + 1") == "ok"
        assert driver.scripts == ["1 + 1"]
        assert driver.navigated == []

    def test_a_window_that_does_not_answer_is_a_timeout(self) -> None:
        driver = FakeDriver([NEUTRAL])
        driver.script_answer = None

        with pytest.raises(LoadTimeout, match="did not answer the script"):
            browser_of(driver).act("1 + 1")


class TestSubmit:
    def test_the_load_event_is_cleared_before_the_script_and_waited_for_after(self) -> None:
        driver = FakeDriver([SIGNED_IN])

        page = browser_of(driver).submit("click()")

        assert page == SIGNED_IN
        assert driver.calls == ["begin", "script", "wait"]
        assert driver.scripts == ["click()"]

    def test_a_script_that_did_not_click_fails_with_its_reason_and_waits_for_nothing(self) -> None:
        driver = FakeDriver([NEUTRAL])
        driver.script_answer = "no element matches"

        with pytest.raises(BrowserError, match="did not take the step: no element matches"):
            browser_of(driver).submit("click()")

        assert driver.calls == ["begin", "script"]

    def test_a_page_that_never_loads_is_a_timeout(self) -> None:
        driver = FakeDriver([NEUTRAL], loads=False)

        with pytest.raises(LoadTimeout, match="no page loaded within 45 s"):
            browser_of(driver).submit("click()")

    def test_a_window_silent_about_the_script_still_gets_the_page_that_loads(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        driver = FakeDriver([NEUTRAL])
        driver.script_answer = None

        with caplog.at_level(logging.WARNING):
            assert browser_of(driver).submit("click()") == NEUTRAL

        assert "no answer to a submit script" in caplog.text

    def test_the_page_that_loads_is_waited_out_of_a_challenge(self) -> None:
        clock = FakeClock()
        driver = FakeDriver([CHALLENGE, NEUTRAL])

        assert browser_of(driver, clock=clock).submit("click()") == NEUTRAL
        assert clock.sleeps == [1.0]

    def test_a_page_that_asks_to_sign_in_is_signed_out(self) -> None:
        driver = FakeDriver([SIGN_IN])

        with pytest.raises(SignedOut, match="session has ended"):
            browser_of(driver).submit("click()")

    def test_a_signed_in_page_saves_the_session_cookie(self) -> None:
        store = FakeStore()
        driver = FakeDriver([SIGNED_IN])
        driver.cookie = FIRST

        browser_of(driver, store).submit("click()")

        assert store.puts == [FIRST]


class TestChallenge:
    def test_a_challenge_that_clears_is_waited_out_a_second_at_a_time(self) -> None:
        clock = FakeClock()
        driver = FakeDriver([CHALLENGE, CHALLENGE, CHALLENGE, NEUTRAL])

        html = browser_of(driver, clock=clock).load(URL)

        assert html == NEUTRAL
        assert clock.sleeps == [1.0, 1.0, 1.0]

    def test_a_challenge_that_stays_fails_after_30_seconds(self) -> None:
        clock = FakeClock()
        driver = FakeDriver([CHALLENGE])

        with pytest.raises(ChallengeError, match="still up after 30 s"):
            browser_of(driver, clock=clock).load(URL)

        assert clock.sleeps == [1.0] * 30
        assert driver.reads == 31

    def test_a_window_that_stops_answering_fails_as_a_timeout(self) -> None:
        clock = FakeClock()
        driver = FakeDriver([None])

        with pytest.raises(LoadTimeout, match="stopped answering"):
            browser_of(driver, clock=clock).load(URL)

        assert clock.sleeps == [1.0] * 30

    def test_a_window_that_answers_after_a_silence_is_read(self) -> None:
        clock = FakeClock()
        driver = FakeDriver([None, None, SIGNED_IN])

        assert browser_of(driver, clock=clock).load(URL) == SIGNED_IN
        assert clock.sleeps == [1.0, 1.0]

    def test_the_page_after_a_challenge_is_checked_for_a_sign_in_form(self) -> None:
        driver = FakeDriver([CHALLENGE, SIGN_IN])

        with pytest.raises(SignedOut):
            browser_of(driver).load(URL)


class FakeHandover:
    """Records what the browser tells the screen; `cancel_at` is the first poll (1-based) to be cancelled."""

    def __init__(self, cancel_at: int | None = None) -> None:
        self.events: list[str] = []
        self.polls = 0
        self.cancel_at = cancel_at

    def show(self, need: Need) -> None:
        self.events.append(f"show:{need.value}")

    def hide(self) -> None:
        self.events.append("hide")

    def cancelled(self) -> bool:
        self.polls += 1
        return self.cancel_at is not None and self.polls >= self.cancel_at


def handed_browser(
    driver: FakeDriver, handover: FakeHandover, store: FakeStore | None = None, clock: FakeClock | None = None
) -> SessionBrowser:
    clock = clock or FakeClock()
    return SessionBrowser(driver, store or FakeStore(), clock=clock, sleep=clock.sleep, handover=handover)


class TestHandover:
    def test_a_signed_out_page_is_handed_over_until_signed_in_then_loaded_again(self) -> None:
        clock, handover, store = FakeClock(), FakeHandover(), FakeStore()
        driver = FakeDriver([SIGN_IN, SIGN_IN, SIGNED_IN])
        driver.cookie = FIRST

        html = handed_browser(driver, handover, store, clock).load(URL)

        assert html == SIGNED_IN
        assert handover.events == ["show:signin", "hide"]
        assert driver.navigated == [URL, URL]
        assert clock.sleeps == [1.0, 1.0]
        assert store.puts == [FIRST]

    def test_a_challenge_that_outlasts_8_seconds_is_handed_over_as_a_check(self) -> None:
        clock, handover = FakeClock(), FakeHandover()
        driver = FakeDriver([CHALLENGE] * 10 + [SIGNED_IN])

        html = handed_browser(driver, handover, clock=clock).load(URL)

        assert html == SIGNED_IN
        assert handover.events == ["show:check", "hide"]
        assert clock.sleeps == [1.0] * 10
        assert driver.navigated == [URL, URL]

    def test_a_challenge_that_passes_within_8_seconds_is_not_handed_over(self) -> None:
        clock, handover = FakeClock(), FakeHandover()
        driver = FakeDriver([CHALLENGE] * 7 + [NEUTRAL])

        assert handed_browser(driver, handover, clock=clock).load(URL) == NEUTRAL
        assert handover.events == []
        assert clock.sleeps == [1.0] * 7

    def test_the_need_follows_the_page_the_owner_is_on(self) -> None:
        handover = FakeHandover()
        driver = FakeDriver([CHALLENGE] * 9 + [SIGN_IN, SIGNED_IN])

        handed_browser(driver, handover).load(URL)

        assert handover.events == ["show:check", "show:signin", "hide"]

    def test_pages_in_between_are_waited_out_without_a_new_show(self) -> None:
        clock, handover = FakeClock(), FakeHandover()
        driver = FakeDriver([SIGN_IN, NEUTRAL, None, SIGNED_IN])

        handed_browser(driver, handover, clock=clock).load(URL)

        assert handover.events == ["show:signin", "hide"]
        assert clock.sleeps == [1.0, 1.0, 1.0]

    def test_a_second_sign_in_on_the_reloaded_page_is_handed_over_again(self) -> None:
        handover = FakeHandover()
        driver = FakeDriver([SIGN_IN, SIGNED_IN, SIGN_IN, SIGNED_IN])

        assert handed_browser(driver, handover).load(URL) == SIGNED_IN
        assert handover.events == ["show:signin", "hide", "show:signin", "hide"]
        assert driver.navigated == [URL, URL, URL]

    def test_cancelling_during_the_handover_raises_and_hides_the_window(self) -> None:
        clock, handover = FakeClock(), FakeHandover(cancel_at=2)
        driver = FakeDriver([SIGN_IN])

        with pytest.raises(Cancelled):
            handed_browser(driver, handover, clock=clock).load(URL)

        assert handover.events == ["show:signin", "hide"]
        assert clock.sleeps == [1.0, 1.0]
        assert driver.navigated == [URL]

    def test_a_step_that_ends_in_a_handover_is_interrupted_and_not_loaded_again(self) -> None:
        handover = FakeHandover()
        driver = FakeDriver([SIGN_IN, SIGNED_IN])

        with pytest.raises(Interrupted):
            handed_browser(driver, handover).submit("click()")

        assert handover.events == ["show:signin", "hide"]
        assert driver.navigated == []

    def test_a_step_that_needs_no_handover_returns_its_page(self) -> None:
        handover = FakeHandover()

        assert handed_browser(FakeDriver([SIGNED_IN]), handover).submit("click()") == SIGNED_IN
        assert handover.events == []

    def test_a_window_that_never_answers_is_still_a_timeout_after_30_seconds(self) -> None:
        clock, handover = FakeClock(), FakeHandover()

        with pytest.raises(LoadTimeout, match="stopped answering"):
            handed_browser(FakeDriver([None]), handover, clock=clock).load(URL)

        assert clock.sleeps == [1.0] * 30
        assert handover.events == []

    def test_without_a_handover_a_challenge_fails_after_30_seconds_and_a_sign_in_is_refused(self) -> None:
        clock = FakeClock()

        with pytest.raises(ChallengeError):
            browser_of(FakeDriver([CHALLENGE]), clock=clock).load(URL)
        with pytest.raises(SignedOut):
            browser_of(FakeDriver([SIGN_IN])).load(URL)

        assert clock.sleeps == [1.0] * 30

    def test_a_window_silent_five_reads_in_a_row_during_the_handover_is_a_timeout(self) -> None:
        clock, handover = FakeClock(), FakeHandover(cancel_at=50)

        with pytest.raises(LoadTimeout, match="stopped answering"):
            handed_browser(FakeDriver([SIGN_IN, None]), handover, clock=clock).load(URL)

        assert clock.sleeps == [1.0] * 5
        assert handover.events == ["show:signin", "hide"]

    def test_silent_reads_apart_do_not_add_up_during_the_handover(self) -> None:
        clock, handover = FakeClock(), FakeHandover()
        answers = [SIGN_IN, *[None] * 4, NEUTRAL, *[None] * 4, SIGNED_IN]

        assert handed_browser(FakeDriver(answers), handover, clock=clock).load(URL) == SIGNED_IN
        assert clock.sleeps == [1.0] * 10


class TestCancel:
    """A cancel is heard while a page loads and while a check is waited out, not only in a handover."""

    def test_the_handovers_cancel_ends_the_wait_for_the_load_event(self) -> None:
        handover = FakeHandover(cancel_at=3)
        driver = FakeDriver([NEUTRAL], loads=False)

        with pytest.raises(Cancelled, match="cancelled while"):
            handed_browser(driver, handover).load(URL)

        assert driver.loaded_waits == [1.0, 1.0, 1.0]

    def test_the_browsers_own_cancel_ends_the_wait_for_the_load_event(self) -> None:
        driver = FakeDriver([NEUTRAL], loads=False)
        cancelled = False

        def wait_seen() -> None:
            nonlocal cancelled
            cancelled = len(driver.loaded_waits) == 2

        driver.on_wait = wait_seen
        browser = SessionBrowser(driver, FakeStore(), cancel=lambda: cancelled)

        with pytest.raises(Cancelled):
            browser.load(URL)

        assert driver.loaded_waits == [1.0, 1.0]

    def test_a_load_that_takes_all_45_seconds_waits_in_one_second_slices(self) -> None:
        driver = FakeDriver([NEUTRAL], loads=False)

        with pytest.raises(LoadTimeout):
            browser_of(driver).load(URL)

        assert driver.loaded_waits == [1.0] * 45

    def test_the_wait_for_the_page_after_a_submit_hears_a_cancel_too(self) -> None:
        driver = FakeDriver([NEUTRAL], loads=False)

        with pytest.raises(Cancelled):
            handed_browser(driver, FakeHandover(cancel_at=2)).submit("click()")

        assert driver.loaded_waits == [1.0, 1.0]

    def test_a_cancel_ends_the_wait_out_of_a_challenge(self) -> None:
        clock = FakeClock()
        driver = FakeDriver([CHALLENGE])

        with pytest.raises(Cancelled, match="cancelled while"):
            handed_browser(driver, FakeHandover(cancel_at=3), clock=clock).load(URL)

        assert clock.sleeps == [1.0, 1.0, 1.0]

    def test_a_cancel_ends_the_wait_for_a_window_that_does_not_answer(self) -> None:
        clock = FakeClock()
        browser = SessionBrowser(
            FakeDriver([None]), FakeStore(), clock=clock, sleep=clock.sleep, cancel=lambda: clock.now > 102
        )

        with pytest.raises(Cancelled):
            browser.load(URL)

        assert clock.sleeps == [1.0, 1.0, 1.0]

    def test_without_a_cancel_nothing_is_cancelled(self) -> None:
        assert browser_of(FakeDriver([CHALLENGE, NEUTRAL])).load(URL) == NEUTRAL


class TestSessionCookie:
    def test_the_saved_cookie_is_put_into_the_window(self) -> None:
        driver = FakeDriver([NEUTRAL])

        restored = browser_of(driver, FakeStore(FIRST)).restore_session()

        assert (restored, driver.added) == (True, [FIRST])

    def test_no_saved_cookie_adds_nothing(self) -> None:
        driver = FakeDriver([NEUTRAL])

        assert browser_of(driver, FakeStore()).restore_session() is False
        assert driver.added == []

    def test_an_unreadable_store_restores_nothing_and_says_so(self, caplog: pytest.LogCaptureFixture) -> None:
        store = FakeStore(FIRST)
        store.fail_reading = True
        driver = FakeDriver([NEUTRAL])

        with caplog.at_level(logging.WARNING):
            assert browser_of(driver, store).restore_session() is False

        assert driver.added == []
        assert "no Cardmarket session restored: locked" in caplog.text

    def test_a_signed_in_page_saves_the_cookie(self) -> None:
        store = FakeStore()
        driver = FakeDriver([SIGNED_IN])
        driver.cookie = FIRST

        browser_of(driver, store).load(URL)

        assert store.puts == [FIRST]

    def test_a_cookie_that_did_not_change_is_saved_once(self) -> None:
        store = FakeStore()
        driver = FakeDriver([SIGNED_IN])
        driver.cookie = FIRST
        browser = browser_of(driver, store)

        browser.load(URL)
        browser.load(URL)

        assert store.puts == [FIRST]

    def test_a_cookie_that_changed_is_saved_again(self) -> None:
        store = FakeStore()
        driver = FakeDriver([SIGNED_IN])
        driver.cookie = FIRST
        browser = browser_of(driver, store)
        browser.load(URL)

        driver.cookie = SECOND
        browser.load(URL)

        assert store.puts == [FIRST, SECOND]

    def test_the_restored_cookie_is_not_written_back(self) -> None:
        store = FakeStore(FIRST)
        driver = FakeDriver([SIGNED_IN])
        driver.cookie = FIRST
        browser = browser_of(driver, store)
        browser.restore_session()

        browser.load(URL)

        assert store.puts == []

    def test_a_page_that_is_not_signed_in_saves_nothing(self) -> None:
        store = FakeStore()
        driver = FakeDriver([NEUTRAL])
        driver.cookie = FIRST

        browser_of(driver, store).load(URL)

        assert store.puts == []

    def test_a_window_without_a_session_cookie_saves_nothing(self) -> None:
        store = FakeStore()

        browser_of(FakeDriver([SIGNED_IN]), store).load(URL)

        assert store.puts == []

    def test_a_store_that_refuses_the_save_is_logged_and_the_page_still_read(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = FakeStore()
        store.fail_writing = True
        driver = FakeDriver([SIGNED_IN])
        driver.cookie = FIRST

        with caplog.at_level(logging.WARNING):
            html = browser_of(driver, store).load(URL)

        assert html == SIGNED_IN
        assert "the Cardmarket session was not saved: locked" in caplog.text
        assert "first" not in caplog.text


class TestOnCardmarket:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.cardmarket.com/en/Magic", True),
            ("https://cardmarket.com/en/Magic", True),
            ("https://www.cardmarket.com./en/Magic", True),
            ("https://WWW.CARDMARKET.COM/en/Magic", True),
            ("http://www.cardmarket.com/en/Magic", False),
            ("https://help.cardmarket.com/", False),
            ("https://evil.com/www.cardmarket.com", False),
            (None, False),
        ],
    )
    def test_the_host_is_matched_exactly(self, url: str | None, expected: bool) -> None:  # noqa: FBT001
        assert on_cardmarket(url) is expected
