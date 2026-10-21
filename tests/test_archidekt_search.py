import copy
import logging
from collections.abc import Collection, Iterable
from datetime import UTC, datetime

import httpx
import pytest

from commander.archidekt import (
    MAX_DECK_READS,
    MAX_SEARCH_PAGES,
    SEARCH_INTERVAL_SECONDS,
    ArchidektError,
    ArchidektSearch,
    PeerDeck,
    brackets_to_read,
    most_viewed,
    parse_search_page,
)
from commander.net import Fetcher, Pacer, RateLimitedError
from conftest import DECK_ID, Json, load_fixture

SINCE = datetime(2026, 8, 16, tzinfo=UTC)
ELLIVERE = "Ellivere of the Wild Court"
IN_WINDOW = "2026-10-01T00:00:00.000000Z"
BEFORE_WINDOW = "2026-06-01T00:00:00.000000Z"
FIRST_ID = 20_000_000
NEXT_LINK = (
    "http://archidekt.com/api/decks/v3/?commanderName=Ellivere+of+the+Wild+Court"
    "&formats=3&orderBy=-viewCount&page=2"
)


def search_page() -> Json:
    return copy.deepcopy(load_fixture("archidekt_search_ellivere.json"))


def listing(
    views: Iterable[int], *, first_id: int = FIRST_ID, old: Collection[int] = (), more: bool = True
) -> Json:
    """A view-ordered search page: copies of the fixture's first row with these view counts, numbered from
    first_id, those at the `old` positions last updated before the window."""
    row = search_page()["results"][0]
    results = [
        {
            **row,
            "id": first_id + n,
            "viewCount": count,
            "updatedAt": BEFORE_WINDOW if n in old else IN_WINDOW,
        }
        for n, count in enumerate(views)
    ]
    return {"count": 500, "next": NEXT_LINK if more else None, "results": results}


def ids(first_id: int, count: int) -> list[int]:
    return list(range(first_id, first_id + count))


class FakeSearch:
    """Answers deck searches with queued pages and deck reads with the Magda fixture; records each request."""

    def __init__(self, pages: list[Json]) -> None:
        self.pages = pages
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/decks/v3/":
            return httpx.Response(200, json=self.next_page_linked(request))
        if request.url.path == f"/api/decks/{DECK_ID}/":
            return httpx.Response(200, json=load_fixture(f"deck_{DECK_ID}.json"))
        return httpx.Response(404)

    def next_page_linked(self, request: httpx.Request) -> Json:
        """The next queued page, its link made to carry this request's filters, as Archidekt's does."""
        page = self.pages.pop(0)
        if not str(page["next"]).startswith("http://archidekt.com/"):
            return page
        number = int(request.url.params.get("page", "1")) + 1
        link = str(request.url.copy_set_param("page", number)).replace("https://", "http://", 1)
        return {**page, "next": link}


def search_for(fake: FakeSearch, sleeps: list[float] | None = None) -> ArchidektSearch:
    clock = [0.0]

    def sleep(seconds: float) -> None:
        (sleeps if sleeps is not None else []).append(seconds)
        clock[0] += seconds

    fetcher = Fetcher(httpx.Client(transport=httpx.MockTransport(fake.handler)), max_attempts=1)
    return ArchidektSearch(fetcher, Pacer(2.0, sleep=sleep, monotonic=lambda: clock[0]))


def found_by(
    fake: FakeSearch,
    *brackets: int | None,
    commander: str | None = ELLIVERE,
    tag: str | None = None,
    max_pages: int = MAX_SEARCH_PAGES,
) -> list[PeerDeck]:
    return search_for(fake).popular_decks(
        commander,
        brackets=brackets or (5,),
        since=SINCE,
        label="ellivere",
        tag=tag,
        max_pages=max_pages,
    )


def test_parses_a_search_page() -> None:
    decks, next_url = parse_search_page(search_page())
    assert len(decks) == 15
    first = decks[0]
    assert (first.id, first.owner, first.views, first.bracket) == (10000002, "TESTER", 1606, 5)
    assert first.updated_at == datetime(2026, 10, 15, 20, 22, 13, 8608, tzinfo=UTC)
    assert first.url == "https://archidekt.com/decks/10000002"
    assert next_url is not None
    assert next_url.startswith("https://archidekt.com/api/decks/v3/?")


def test_the_search_asks_for_the_most_viewed_decks_of_each_bracket() -> None:
    fake = FakeSearch([listing(range(2000, 1990, -1), more=False)])
    decks = found_by(fake)
    assert len(decks) == 10
    assert dict(fake.requests[0].url.params) == {
        "commanderName": ELLIVERE,
        "formats": "3",
        "orderBy": "-viewCount",
        "edhBracket": "5",
    }


def test_only_decks_updated_in_the_window_are_kept() -> None:
    fake = FakeSearch([listing([3000, 2000, 1500, 1200, 1100, 1050], old={3}, more=False)])
    kept = [FIRST_ID, FIRST_ID + 1, FIRST_ID + 2, FIRST_ID + 4, FIRST_ID + 5]
    assert [d.id for d in found_by(fake)] == kept


def test_a_deck_updated_exactly_at_the_window_start_is_kept() -> None:
    page = listing([3000], more=False)
    page["results"][0]["updatedAt"] = "2026-08-16T00:00:00.000000Z"
    assert len(found_by(FakeSearch([page]))) == 1


FIRST_PAGE_OF_EIGHT = [5000, 4000, 3000, 2500, 2000, 1800, 1600, 1400]


def test_ten_decks_at_1000_views_end_the_search_at_the_page_that_drops_below_1000() -> None:
    fake = FakeSearch(
        [listing(FIRST_PAGE_OF_EIGHT), listing([1200, 1000, 900], first_id=FIRST_ID + 100, more=True)]
    )
    decks = found_by(fake)
    # Ten reach 1000, so the search stops without a third page (the fake has none to give).
    assert len(fake.requests) == 2
    assert len(decks) == 11


def test_nine_decks_at_1000_views_keep_paging_down_to_750() -> None:
    fake = FakeSearch(
        [
            listing(FIRST_PAGE_OF_EIGHT),
            listing([1200, 900, 800], first_id=FIRST_ID + 100),
            listing([700, 600], first_id=FIRST_ID + 200),
        ]
    )
    decks = found_by(fake)
    # Nine reach 1000 and the second page ends at 800, still over 750, so a third page is asked for; it ends
    # at 600 with eleven decks over 750.
    assert len(fake.requests) == 3
    assert fake.requests[2].url.params["page"] == "3"
    assert len(decks) == 13


def test_a_page_ending_exactly_at_the_floor_is_followed_by_the_next() -> None:
    fake = FakeSearch([listing([1000] * 10), listing([999], first_id=FIRST_ID + 100, more=False)])
    assert len(found_by(fake)) == 11
    assert len(fake.requests) == 2


def test_under_ten_at_750_the_search_goes_on_to_500() -> None:
    fake = FakeSearch(
        [
            listing([1500, 1200, 900, 800, 700, 650, 600, 550]),
            listing([520, 510, 505, 490], first_id=FIRST_ID + 100),
        ]
    )
    decks = found_by(fake)
    # The first page ends at 550, over 500, and holds three decks under ten at 750: the next page is read, and
    # eleven reach 500.
    assert len(fake.requests) == 2
    assert len(decks) == 12


def test_the_search_never_goes_below_500_views() -> None:
    fake = FakeSearch([listing([1500, 900, 600, 100]), listing([50], first_id=FIRST_ID + 100)])
    decks = found_by(fake)
    # The first page ends at 100: the three reaching 500 are all there are, and no second page is asked for.
    assert len(fake.requests) == 1
    assert [r.url.params["orderBy"] for r in fake.requests] == ["-viewCount"]
    assert len(decks) == 4


def test_the_search_follows_the_next_link_over_https() -> None:
    last = listing([900], first_id=FIRST_ID + 100, more=False)
    fake = FakeSearch([listing(range(5000, 4000, -100)), last])
    found_by(fake, None)
    assert "edhBracket" not in fake.requests[0].url.params
    assert fake.requests[1].url.scheme == "https"
    assert fake.requests[1].url.params["page"] == "2"


def test_the_brackets_page_down_the_floors_together_without_starting_over() -> None:
    fake = FakeSearch(
        [
            listing([3000, 2500, 2000, 1800, 1200, 800], first_id=FIRST_ID),
            listing([2200, 2100, 900, 700], first_id=FIRST_ID + 100, more=False),
            listing([760, 500], first_id=FIRST_ID + 200, more=False),
        ]
    )
    decks = found_by(fake, 4, 5)
    # Seven reach 1000 over both brackets, so the search steps down to 750: bracket 4's page 1 ended at 800
    # and goes on to its page 2, while bracket 5's ended at 700 and is done; ten then reach 750.
    assert [(r.url.params.get("edhBracket"), r.url.params.get("page")) for r in fake.requests] == [
        ("4", None),
        ("5", None),
        ("4", "2"),
    ]
    assert len(decks) == 12


def test_a_deck_found_in_two_brackets_counts_once() -> None:
    fake = FakeSearch(
        [listing([900, 800, 700], more=False), listing([700, 650, 600], first_id=FIRST_ID + 2, more=False)]
    )
    decks = found_by(fake, 4, 5)
    assert [r.url.params["edhBracket"] for r in fake.requests] == ["4", "5"]
    assert [d.id for d in decks] == ids(FIRST_ID, 5)


def test_seventy_matching_decks_are_all_found() -> None:
    fake = FakeSearch(
        [
            listing(range(9000, 8940, -1)),
            listing([1500] * 10, first_id=FIRST_ID + 100, more=False),
        ]
    )
    assert len(found_by(fake)) == 70


def test_searches_and_deck_reads_are_paced() -> None:
    fake = FakeSearch([listing(range(2000, 1990, -1), more=False)])
    sleeps: list[float] = []
    search = search_for(fake, sleeps)
    search.popular_decks(ELLIVERE, brackets=(5,), since=SINCE, label="ellivere")
    deck = search.deck(DECK_ID)
    assert deck.id == DECK_ID
    assert sleeps == [2.0]


def test_every_request_logs_its_target_and_time_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    search = search_for(FakeSearch([listing(range(2000, 1990, -1), more=False)]))
    with caplog.at_level(logging.DEBUG, logger="commander.archidekt"):
        search.popular_decks(ELLIVERE, brackets=(5,), since=SINCE, label="ellivere")
        search.deck(DECK_ID)
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    timings = [m for m in messages if " took " in m]
    assert len(timings) == 2
    assert timings[0].startswith("Archidekt request https://archidekt.com/api/decks/v3/?")
    assert "edhBracket=5" in timings[0]
    assert "orderBy=-viewCount" in timings[0]
    assert timings[1].startswith(f"Archidekt request https://archidekt.com/api/decks/{DECK_ID}/ took ")


def test_a_search_logs_its_order_pages_and_decks_in_the_window(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeSearch([listing(range(5000, 4000, -100)), listing([900], first_id=FIRST_ID + 100, old={0})])
    with caplog.at_level(logging.INFO, logger="commander.archidekt"):
        found_by(fake)
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.INFO] == [
        "Archidekt search for ellivere by views: 2 pages, 10 decks in the window"
    ]


def test_a_deck_read_with_the_wrong_shape_is_an_archidekt_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": DECK_ID})

    fetcher = Fetcher(httpx.Client(transport=httpx.MockTransport(handler)), max_attempts=1)

    with pytest.raises(ArchidektError, match=f"unexpected data for deck {DECK_ID}"):
        ArchidektSearch(fetcher, Pacer(0)).deck(DECK_ID)


def test_a_time_without_a_zone_is_refused() -> None:
    page = listing([2000], more=False)
    page["results"][0]["updatedAt"] = "2026-10-15T20:22:13"
    with pytest.raises(ArchidektError, match=r"updatedAt: no time zone"):
        found_by(FakeSearch([page]))


def test_a_next_link_off_archidekt_is_refused() -> None:
    page = listing([2000])
    page["next"] = "https://evil.example/api/decks/v3/?page=2"
    with pytest.raises(ArchidektError, match="outside the Archidekt API"):
        found_by(FakeSearch([page]))


def viewed(*views: int) -> list[PeerDeck]:
    """Decks numbered from 1 with these view counts, none the user's own."""
    return [PeerDeck(i, f"deck {i}", "someone", v, SINCE, 5) for i, v in enumerate(views, start=1)]


def view_counts(decks: list[PeerDeck]) -> list[int]:
    return [d.views for d in decks]


def test_no_decks_found_gives_no_decks() -> None:
    assert most_viewed([]) == []


def test_ten_decks_at_1000_views_are_all_that_is_read_most_viewed_first() -> None:
    decks = viewed(1000, 5000, 1200, 1000, 1999, 1000, 1000, 1000, 1000, 1000, 999, 750, 3)
    assert view_counts(most_viewed(decks)) == [5000, 1999, 1200, 1000, 1000, 1000, 1000, 1000, 1000, 1000]


def test_under_ten_at_1000_views_the_bar_steps_down_to_750_then_500() -> None:
    # Nine at 1000 and one at 750: the 750 step reaches ten and stops there.
    at_750 = viewed(*[1000] * 9, 750, 749, 500)
    assert view_counts(most_viewed(at_750)) == [1000] * 9 + [750]
    # Nine at 1000, none between, one at 500: only the 500 step reaches ten.
    at_500 = viewed(*[1000] * 9, 500, 499, 10)
    assert view_counts(most_viewed(at_500)) == [1000] * 9 + [500]


def test_under_ten_at_500_views_only_those_at_500_are_read_most_viewed_first() -> None:
    assert view_counts(most_viewed(viewed(120, 40, 560, 300, 500))) == [560, 500]
    assert view_counts(most_viewed(viewed(*[500] * 9, 499, 3))) == [500] * 9


def test_decks_under_500_views_are_never_read() -> None:
    assert most_viewed(viewed(499, 12, 3)) == []
    assert most_viewed(viewed(0, 7)) == []


def test_at_most_sixty_decks_are_read_most_viewed_first() -> None:
    decks = viewed(*range(1000, 1070))
    chosen = most_viewed(decks)
    assert MAX_DECK_READS == 60
    assert len(chosen) == 60
    assert view_counts(chosen) == list(range(1069, 1009, -1))
    assert len(most_viewed(decks, limit=5)) == 5


def test_decks_with_the_same_views_come_in_id_order() -> None:
    assert [d.id for d in most_viewed(viewed(500, 500, 500))] == [1, 2, 3]


def test_a_deck_reads_its_own_bracket_and_the_one_above() -> None:
    assert brackets_to_read(1) == (1, 2)
    assert brackets_to_read(3) == (3, 4)
    assert brackets_to_read(4) == (4, 5)
    # Nothing is above bracket 5, and a deck with no bracket reads every one.
    assert brackets_to_read(5) == (5,)
    assert brackets_to_read(None) == (None,)


def full_pages(count: int) -> list[Json]:
    """Pages of ten decks all far over the floors, each linking to a next one, so nothing ends a search."""
    return [listing([9000] * 10, first_id=FIRST_ID + 100 * n) for n in range(count)]


def test_each_bracket_of_a_commander_is_searched_at_most_5_pages(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeSearch(full_pages(12))
    with caplog.at_level(logging.INFO, logger="commander.archidekt"):
        decks = found_by(fake, 3, 4)
    assert MAX_SEARCH_PAGES == 5
    # Every page links to another one over the floor, so each bracket stops at its fifth.
    assert [r.url.params.get("edhBracket") for r in fake.requests] == ["3"] * 5 + ["4"] * 5
    assert [r.url.params.get("page") for r in fake.requests] == [None, "2", "3", "4", "5"] * 2
    assert len(decks) == 100
    messages = [r.getMessage() for r in caplog.records]
    assert "Archidekt search for ellivere by views was cut short at 5 pages a bracket" in messages


def test_a_search_takes_a_page_limit_of_its_own_for_each_bracket() -> None:
    fake = FakeSearch(full_pages(10))
    decks = found_by(fake, 3, 4, commander=None, tag="Landfall", max_pages=3)
    assert [r.url.params["edhBracket"] for r in fake.requests] == ["3", "3", "3", "4", "4", "4"]
    assert len(decks) == 60


def test_the_deck_tag_filter_is_sent_as_deck_tag_name() -> None:
    fake = FakeSearch([listing([2000], more=False), listing([2000], more=False)])
    found_by(fake, tag="Stax")
    found_by(fake)
    assert fake.requests[0].url.params["deckTagName"] == "Stax"
    assert "deckTagName" not in fake.requests[1].url.params


def test_a_tag_search_without_a_commander_sends_no_commander_name() -> None:
    fake = FakeSearch([listing([2000], more=False)])
    found_by(fake, 3, commander=None, tag="Landfall")
    params = fake.requests[0].url.params
    assert params["deckTagName"] == "Landfall"
    assert "commanderName" not in params


class Limited:
    """Answers each request with the next scripted response, then clock time passes only by sleeping."""

    def __init__(self, *steps: httpx.Response) -> None:
        self.steps = list(steps)
        self.at: list[float] = []
        self.sleeps: list[float] = []
        self.clock = 0.0

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock += seconds

    def handler(self, _: httpx.Request) -> httpx.Response:
        self.at.append(self.clock)
        return self.steps.pop(0)

    def search(self) -> ArchidektSearch:
        fetcher = Fetcher(httpx.Client(transport=httpx.MockTransport(self.handler)), sleep=self.sleep)
        return ArchidektSearch(
            fetcher, Pacer(SEARCH_INTERVAL_SECONDS, sleep=self.sleep, monotonic=lambda: self.clock)
        )


def deck_answer() -> httpx.Response:
    return httpx.Response(200, json=load_fixture(f"deck_{DECK_ID}.json"))


def test_requests_are_six_and_a_half_seconds_apart_so_a_minute_holds_fewer_than_ten() -> None:
    assert SEARCH_INTERVAL_SECONDS == 6.5
    limited = Limited(deck_answer(), deck_answer(), deck_answer())
    search = limited.search()
    for _ in range(3):
        search.deck(DECK_ID)
    assert limited.at == [0.0, 6.5, 13.0]


def test_after_a_429_without_retry_after_nothing_goes_out_for_a_minute() -> None:
    limited = Limited(httpx.Response(429), deck_answer())
    assert limited.search().deck(DECK_ID).id == DECK_ID
    assert limited.at == [0.0, 60.0]


def test_after_a_429_with_a_longer_retry_after_that_is_waited() -> None:
    limited = Limited(httpx.Response(429, headers={"Retry-After": "600"}), deck_answer())
    limited.search().deck(DECK_ID)
    assert limited.at == [0.0, 600.0]


def test_a_retry_after_under_a_minute_still_waits_a_minute() -> None:
    limited = Limited(httpx.Response(429, headers={"Retry-After": "20"}), deck_answer())
    limited.search().deck(DECK_ID)
    assert limited.at == [0.0, 60.0]


def test_a_second_429_gives_up_for_the_rest_of_the_run_without_another_request_or_wait() -> None:
    limited = Limited(httpx.Response(429), httpx.Response(429), deck_answer())
    search = limited.search()
    with pytest.raises(RateLimitedError):
        search.deck(DECK_ID)
    assert limited.at == [0.0, 60.0]
    slept = list(limited.sleeps)
    with pytest.raises(RateLimitedError):
        search.deck(DECK_ID)
    with pytest.raises(RateLimitedError):
        search.popular_decks(ELLIVERE, brackets=(5,), since=SINCE, label="ellivere")
    assert limited.at == [0.0, 60.0]
    assert limited.sleeps == slept


def test_a_new_search_after_a_give_up_starts_fresh() -> None:
    limited = Limited(httpx.Response(429), httpx.Response(429), deck_answer())
    with pytest.raises(RateLimitedError):
        limited.search().deck(DECK_ID)
    assert limited.search().deck(DECK_ID).id == DECK_ID


def test_a_search_page_is_retried_after_a_429_too() -> None:
    page = listing(range(2000, 1987, -1), more=False)
    limited = Limited(httpx.Response(429), httpx.Response(200, json=page))
    decks = limited.search().popular_decks(ELLIVERE, brackets=(5,), since=SINCE, label="ellivere")
    assert limited.at == [0.0, 60.0]
    assert len(decks) == 13
