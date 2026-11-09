import logging

import pytest

from commander.cmsession import (
    ChallengeError,
    LoadTimeout,
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

    def __init__(self, answers: list[str | None], *, loads: bool = True) -> None:
        self.answers = answers
        self.loads = loads
        self.navigated: list[str] = []
        self.reads = 0
        self.cookie: SessionCookie | None = None
        self.added: list[SessionCookie] = []
        self.closed = False

    def navigate(self, url: str, timeout_s: float) -> bool:  # noqa: ARG002 - the protocol's parameter name
        self.navigated.append(url)
        return self.loads

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
