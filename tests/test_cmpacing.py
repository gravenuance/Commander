"""The gap between requests holds across the wants sync and the scan that follows it on one browser."""

import pytest

from commander.cmscan import ScanRequest, scan
from commander.cmsession import RequestPacer, SessionBrowser
from commander.cmwants import APP_LIST_NAME, sync_wants
from commander.credentials import CredentialError, SessionCookie
from commander.sellersolve import WantedCard

LISTS_PAGE = (
    "<html><body><main><h1>Wants</h1>"
    '<form action="/en/Magic/PostGetAction/WantsList_CreateWantsList"></form>'
    '<div><span>Commander</span><a href="/en/Magic/Wants/7">Open</a></div></main></body></html>'
)
EMPTY_LIST_PAGE = (
    f"<html><body><main><h1>{APP_LIST_NAME}</h1>"
    '<a href="/en/Magic/Wants/7/AddCards">Add</a><table></table></main></body></html>'
)
NOT_FOUND = (
    "<html><head><title>%metacardName% - MTG Cards | Cardmarket</title></head><body><main>"
    '<h1 class="display-1"><span>404</span><span class="visually-hidden">Page not found</span></h1>'
    "</main></body></html>"
)
PAGES = {
    "https://www.cardmarket.com/en/Magic/Wants": LISTS_PAGE,
    "https://www.cardmarket.com/en/Magic/Wants/7": EMPTY_LIST_PAGE,
    "https://www.cardmarket.com/en/Magic/Cards/Sol-Ring": NOT_FOUND,
}


class Clock:
    """A clock that only moves when somebody sleeps, and the sleeps it saw."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class PagedDriver:
    """Serves the page of the address last navigated to."""

    def __init__(self) -> None:
        self.url = ""

    def navigate(self, url: str) -> bool:
        self.url = url
        return True

    def read_html(self, timeout_s: float) -> str | None:  # noqa: ARG002 - the protocol's parameter name
        return PAGES[self.url.split("?")[0]]

    def run_script(self, script: str, timeout_s: float) -> str | None:  # noqa: ARG002 - the protocol
        return None

    def begin_navigation(self) -> None:
        return

    def wait_loaded(self, timeout_s: float) -> bool:  # noqa: ARG002 - the protocol's parameter name
        return True

    def read_session_cookie(self) -> SessionCookie | None:
        return None

    def add_session_cookie(self, cookie: SessionCookie) -> None:  # noqa: ARG002 - the protocol's name
        raise CredentialError("not expected")

    def close(self) -> None:
        return


class Store:
    def get(self) -> SessionCookie | None:
        return None

    def put(self, cookie: SessionCookie) -> None:  # noqa: ARG002 - the protocol's name
        raise CredentialError("not expected")

    def delete(self) -> bool:
        return False


def test_the_scans_first_page_waits_the_gap_after_the_syncs_last_request() -> None:
    clock = Clock()
    browser = SessionBrowser(PagedDriver(), Store(), clock=clock, sleep=clock.sleep)

    sync_wants(
        browser, [], languages=(), min_condition=None, sleep=clock.sleep, uniform=lambda _low, _high: 4.5
    )
    assert clock.sleeps == [4.5]

    request = ScanRequest([WantedCard("Sol Ring", 1)])
    result = scan(browser, request, sleep=clock.sleep, uniform=lambda _low, _high: 4.5)

    assert result.not_found == ("Sol Ring",)
    assert clock.sleeps == [4.5, 4.5]


def test_the_gap_already_passed_is_not_waited_twice() -> None:
    clock = Clock()
    browser = SessionBrowser(PagedDriver(), Store(), clock=clock, sleep=clock.sleep)

    sync_wants(
        browser, [], languages=(), min_condition=None, sleep=clock.sleep, uniform=lambda _low, _high: 4.5
    )
    clock.now += 3.0
    scan(
        browser, ScanRequest([WantedCard("Sol Ring", 1)]), sleep=clock.sleep, uniform=lambda _low, _high: 4.5
    )

    assert clock.sleeps == [4.5, 1.5]


class TestRequestPacer:
    def test_the_first_request_does_not_wait(self) -> None:
        clock = Clock()

        RequestPacer(clock).wait(lambda: 4.0, clock.sleep)

        assert clock.sleeps == []

    def test_only_what_is_left_of_the_gap_since_the_last_request_is_waited(self) -> None:
        clock = Clock()
        pacer = RequestPacer(clock)
        pacer.mark()
        clock.now += 1.5

        pacer.wait(lambda: 4.0, clock.sleep)

        assert clock.sleeps == [2.5]

    @pytest.mark.parametrize("elapsed", [4.0, 9.0])
    def test_a_gap_that_has_passed_waits_nothing(self, elapsed: float) -> None:
        clock = Clock()
        pacer = RequestPacer(clock)
        pacer.mark()
        clock.now += elapsed

        pacer.wait(lambda: 4.0, clock.sleep)

        assert clock.sleeps == []
