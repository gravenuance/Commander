import copy
from datetime import UTC, datetime

import httpx
import pytest

from commander.archidekt import ArchidektError, ArchidektSearch, parse_search_page
from commander.net import Fetcher, Pacer
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
    decks = search_for(fake).recent_decks(ELLIVERE, bracket=5, since=SINCE)
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
    decks = search_for(fake).recent_decks(ELLIVERE, bracket=None, since=SINCE)
    assert len(decks) == 5 + 13
    assert "edhBracket" not in fake.requests[0].url.params
    assert fake.requests[1].url.scheme == "https"
    assert fake.requests[1].url.params["page"] == "2"


def test_searches_and_deck_reads_are_paced() -> None:
    fake = FakeSearch([search_page()])
    sleeps: list[float] = []
    search = search_for(fake, sleeps)
    search.recent_decks(ELLIVERE, bracket=5, since=SINCE)
    deck = search.deck(DECK_ID)
    assert deck.id == DECK_ID
    assert sleeps == [2.0]


def test_a_time_without_a_zone_is_refused() -> None:
    page = search_page()
    page["results"][0]["updatedAt"] = "2026-10-15T20:22:13"
    with pytest.raises(ArchidektError, match=r"updatedAt: no time zone"):
        search_for(FakeSearch([page])).recent_decks(ELLIVERE, bracket=5, since=SINCE)


def test_a_next_link_off_archidekt_is_refused() -> None:
    page = search_page()
    page["next"] = "https://evil.example/api/decks/v3/?page=2"
    with pytest.raises(ArchidektError, match="outside the Archidekt API"):
        search_for(FakeSearch([page])).recent_decks(ELLIVERE, bracket=5, since=SINCE)
