import copy
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


def search_page() -> Json:
    return copy.deepcopy(load_fixture("archidekt_search_ellivere.json"))


class FakeSearch:
    """Answers deck searches with queued pages and deck reads with the Magda fixture; records each request."""

    def __init__(self, pages: list[Json]) -> None:
        self.pages = pages
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/decks/v3/":
            return httpx.Response(200, json=self.pages.pop(0))
        if request.url.path == f"/api/decks/{DECK_ID}/":
            return httpx.Response(200, json=load_fixture(f"deck_{DECK_ID}.json"))
        return httpx.Response(404)


def search_for(fake: FakeSearch, sleeps: list[float] | None = None) -> ArchidektSearch:
    clock = [0.0]

    def sleep(seconds: float) -> None:
        (sleeps if sleeps is not None else []).append(seconds)
        clock[0] += seconds

    fetcher = Fetcher(httpx.Client(transport=httpx.MockTransport(fake.handler)), max_attempts=1)
    return ArchidektSearch(fetcher, Pacer(2.0, sleep=sleep, monotonic=lambda: clock[0]))


def test_parses_a_search_page() -> None:
    decks, next_url = parse_search_page(search_page())
    assert len(decks) == 15
    first = decks[0]
    assert (first.id, first.owner, first.views, first.bracket) == (10078393, "MAKKIS", 1606, 5)
    assert first.updated_at == datetime(2026, 10, 15, 20, 22, 13, 8608, tzinfo=UTC)
    assert first.url == "https://archidekt.com/decks/10078393"
    assert next_url is not None
    assert next_url.startswith("https://archidekt.com/api/decks/v3/?")


def test_recent_decks_stop_at_the_page_reaching_past_the_window() -> None:
    fake = FakeSearch([search_page()])
    decks = search_for(fake).recent_decks(ELLIVERE, brackets=(5,), since=SINCE)
    # The last two on the page were updated in July and June.
    assert [d.id for d in decks][-2:] == [25161352, 23074747]
    assert len(decks) == 13
    assert len(fake.requests) == 1
    assert dict(fake.requests[0].url.params) == {
        "commanderName": ELLIVERE,
        "formats": "3",
        "orderBy": "-updatedAt",
        "pageSize": "50",
        "edhBracket": "5",
    }


def test_recent_decks_follow_the_next_link_over_https() -> None:
    first = search_page()
    first["results"] = first["results"][:5]
    fake = FakeSearch([first, search_page()])
    decks = search_for(fake).recent_decks(ELLIVERE, brackets=(None,), since=SINCE)
    # The second page repeats the first five, which count once.
    assert len(decks) == 13
    assert "edhBracket" not in fake.requests[0].url.params
    assert fake.requests[1].url.scheme == "https"
    assert fake.requests[1].url.params["page"] == "2"


def test_searches_and_deck_reads_are_paced() -> None:
    fake = FakeSearch([search_page()])
    sleeps: list[float] = []
    search = search_for(fake, sleeps)
    search.recent_decks(ELLIVERE, brackets=(5,), since=SINCE)
    deck = search.deck(DECK_ID)
    assert deck.id == DECK_ID
    assert sleeps == [2.0]


def test_a_deck_read_with_the_wrong_shape_is_an_archidekt_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": DECK_ID})

    fetcher = Fetcher(httpx.Client(transport=httpx.MockTransport(handler)), max_attempts=1)

    with pytest.raises(ArchidektError, match=f"unexpected data for deck {DECK_ID}"):
        ArchidektSearch(fetcher, Pacer(0)).deck(DECK_ID)


def test_a_time_without_a_zone_is_refused() -> None:
    page = search_page()
    page["results"][0]["updatedAt"] = "2026-10-15T20:22:13"
    with pytest.raises(ArchidektError, match=r"updatedAt: no time zone"):
        search_for(FakeSearch([page])).recent_decks(ELLIVERE, brackets=(5,), since=SINCE)


def test_a_next_link_off_archidekt_is_refused() -> None:
    page = search_page()
    page["next"] = "https://evil.example/api/decks/v3/?page=2"
    with pytest.raises(ArchidektError, match="outside the Archidekt API"):
        search_for(FakeSearch([page])).recent_decks(ELLIVERE, brackets=(5,), since=SINCE)


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


def test_under_ten_at_500_views_every_deck_is_read_most_viewed_first() -> None:
    assert view_counts(most_viewed(viewed(120, 40, 560, 300))) == [560, 300, 120, 40]
    assert view_counts(most_viewed(viewed(0, 7))) == [7, 0]


def test_all_decks_below_500_views_still_give_data() -> None:
    assert view_counts(most_viewed(viewed(499, 12, 3))) == [499, 12, 3]


def test_at_most_sixty_decks_are_read_most_viewed_first() -> None:
    decks = viewed(*range(1000, 1070))
    chosen = most_viewed(decks)
    assert MAX_DECK_READS == 60
    assert len(chosen) == 60
    assert view_counts(chosen) == list(range(1069, 1009, -1))
    # The stepped-down and the all-decks cases are capped alike.
    assert len(most_viewed(viewed(*range(70)))) == 60
    assert len(most_viewed(viewed(*range(70)), limit=5)) == 5


def test_decks_with_the_same_views_come_in_id_order() -> None:
    assert [d.id for d in most_viewed(viewed(5, 5, 5))] == [1, 2, 3]


def test_a_deck_reads_its_own_bracket_and_the_one_above() -> None:
    assert brackets_to_read(1) == (1, 2)
    assert brackets_to_read(3) == (3, 4)
    assert brackets_to_read(4) == (4, 5)
    # Nothing is above bracket 5, and a deck with no bracket reads every one.
    assert brackets_to_read(5) == (5,)
    assert brackets_to_read(None) == (None,)


def test_each_bracket_is_searched_once_and_a_deck_found_twice_counts_once() -> None:
    first = search_page()
    first["results"] = first["results"][:3]
    first["next"] = None
    second = copy.deepcopy(first)
    second["results"] = [second["results"][1], search_page()["results"][2]]
    fake = FakeSearch([first, second])
    decks = search_for(fake).recent_decks(ELLIVERE, brackets=(4, 5), since=SINCE)
    assert [r.url.params["edhBracket"] for r in fake.requests] == ["4", "5"]
    assert [d.id for d in decks] == [10078393, 23204858, 7432856]


def test_a_commander_is_searched_at_most_20_pages_across_its_brackets() -> None:
    def full_page() -> Json:
        page = search_page()
        page["results"] = page["results"][:2]
        return page

    fake = FakeSearch([full_page() for _ in range(2 * MAX_SEARCH_PAGES)])
    decks = search_for(fake).recent_decks(ELLIVERE, brackets=(3, 4), since=SINCE)
    assert MAX_SEARCH_PAGES == 20
    # The first bracket takes all twenty, as every page links to a next one still inside the window.
    assert len(fake.requests) == MAX_SEARCH_PAGES
    assert fake.requests[0].url.params["edhBracket"] == "3"
    assert {r.url.params.get("page") for r in fake.requests[1:]} == {"2"}
    assert len(decks) == 2


def test_the_deck_tag_filter_is_sent_as_deck_tag_name() -> None:
    fake = FakeSearch([search_page(), search_page()])
    search = search_for(fake)
    search.recent_decks(ELLIVERE, brackets=(5,), since=SINCE, tag="Stax")
    search.recent_decks(ELLIVERE, brackets=(5,), since=SINCE)
    assert fake.requests[0].url.params["deckTagName"] == "Stax"
    assert "deckTagName" not in fake.requests[1].url.params


def test_a_tag_search_without_a_commander_sends_no_commander_name() -> None:
    fake = FakeSearch([search_page()])
    search_for(fake).recent_decks(None, brackets=(3,), since=SINCE, tag="Landfall")
    params = fake.requests[0].url.params
    assert params["deckTagName"] == "Landfall"
    assert "commanderName" not in params


def test_a_search_takes_a_page_limit_of_its_own() -> None:
    def full_page() -> Json:
        page = search_page()
        page["results"] = page["results"][:2]
        return page

    fake = FakeSearch([full_page() for _ in range(10)])
    search_for(fake).recent_decks(None, brackets=(3, 4), since=SINCE, tag="Landfall", max_pages=3)
    assert len(fake.requests) == 3


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
        return ArchidektSearch(fetcher, Pacer(5.0, sleep=self.sleep, monotonic=lambda: self.clock))


def deck_answer() -> httpx.Response:
    return httpx.Response(200, json=load_fixture(f"deck_{DECK_ID}.json"))


def test_requests_are_five_seconds_apart() -> None:
    assert SEARCH_INTERVAL_SECONDS == 5.0
    limited = Limited(deck_answer(), deck_answer(), deck_answer())
    search = limited.search()
    for _ in range(3):
        search.deck(DECK_ID)
    assert limited.at == [0.0, 5.0, 10.0]


def test_after_a_429_without_retry_after_nothing_goes_out_for_180_seconds() -> None:
    limited = Limited(httpx.Response(429), deck_answer())
    assert limited.search().deck(DECK_ID).id == DECK_ID
    assert limited.at == [0.0, 180.0]


def test_after_a_429_with_a_longer_retry_after_that_is_waited() -> None:
    limited = Limited(httpx.Response(429, headers={"Retry-After": "600"}), deck_answer())
    limited.search().deck(DECK_ID)
    assert limited.at == [0.0, 600.0]


def test_a_retry_after_under_180_seconds_still_waits_180() -> None:
    limited = Limited(httpx.Response(429, headers={"Retry-After": "20"}), deck_answer())
    limited.search().deck(DECK_ID)
    assert limited.at == [0.0, 180.0]


def test_a_second_429_gives_up_and_the_next_request_still_waits() -> None:
    limited = Limited(httpx.Response(429), httpx.Response(429), deck_answer())
    search = limited.search()
    with pytest.raises(RateLimitedError):
        search.deck(DECK_ID)
    assert limited.at == [0.0, 180.0]
    assert search.deck(DECK_ID).id == DECK_ID
    assert limited.at == [0.0, 180.0, 360.0]


def test_a_search_page_is_retried_after_a_429_too() -> None:
    limited = Limited(httpx.Response(429), httpx.Response(200, json=search_page()))
    decks = limited.search().recent_decks(ELLIVERE, brackets=(5,), since=SINCE)
    assert limited.at == [0.0, 180.0]
    assert len(decks) == 13
