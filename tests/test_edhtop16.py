import copy
import logging
from datetime import date

import httpx
import pytest

from commander.edhtop16 import (
    DecklistCard,
    Edhtop16Client,
    Edhtop16Error,
    EntrySort,
    commander_name,
    commander_url,
    graphql_data,
    parse_entries_page,
)
from commander.net import Fetcher, NetworkError, Pacer
from conftest import FakeEdhtop16, Json, load_fixture

ELLIVERE = "Ellivere of the Wild Court"
KINNAN = "Kinnan, Bonder Prodigy"
SINCE = date(2026, 8, 16)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def client_for(fake: FakeEdhtop16, clock: Clock | None = None) -> Edhtop16Client:
    clock = clock or Clock()
    pacer = Pacer(1.5, sleep=clock.sleep, monotonic=lambda: clock.now)
    return Edhtop16Client(Fetcher(fake.client(), max_attempts=1), pacer)


def ellivere_page() -> Json:
    return copy.deepcopy(load_fixture("edhtop16_entries_ellivere.json"))


def edges(page: Json) -> list[Json]:
    found: list[Json] = page["data"]["commander"]["entries"]["edges"]
    return found


def test_parses_an_entries_page() -> None:
    parsed = parse_entries_page(ellivere_page()["data"])
    assert parsed is not None
    entries, after = parsed
    assert after == "4"
    assert [(e.tournament.held_on, e.tournament.size, e.tournament.top_cut, e.standing) for e in entries] == [
        (date(2026, 10, 4), 30, 8, 12),
        (date(2026, 9, 13), 20, 0, 5),
        (date(2026, 8, 16), 32, 4, 21),
        (date(2026, 7, 19), 45, 8, 18),
    ]
    first = entries[0]
    assert (first.wins, first.losses, first.draws) == (2, 1, 1)
    assert first.tournament.tid == "test-open-1"
    assert first.decklist_url == ("https://edhtop16.com/tournament/test-open-1/decklist/TestDeckAaaa0001")
    assert DecklistCard("Oblivion Ring", 1, 2) in first.cards
    assert DecklistCard("Fable of the Mirror-Breaker // Reflection of Kiki-Jiki", 1, 3) in first.cards
    assert len(first.cards) == 7


def test_top_cut_needs_a_placing_inside_the_cut() -> None:
    parsed = parse_entries_page(ellivere_page()["data"])
    assert parsed is not None
    # 12th of 30 with a top 8; 5th of 20 at an event without a cut.
    assert [e.made_top_cut for e in parsed[0]] == [False, False, False, False]
    assert parsed[0][1].placing == pytest.approx(4 / 20)


def test_a_page_without_more_has_no_cursor() -> None:
    page = ellivere_page()
    page["data"]["commander"]["entries"]["pageInfo"] = {"hasNextPage": False, "endCursor": "4"}
    parsed = parse_entries_page(page["data"])
    assert parsed is not None
    assert parsed[1] is None


def test_newest_first_stops_at_the_page_reaching_past_the_window() -> None:
    fake = FakeEdhtop16()
    found = client_for(fake).entries(ELLIVERE, since=SINCE, min_event_size=16, sort=EntrySort.NEW, limit=400)
    assert found is not None
    assert [e.tournament.held_on for e in found] == [date(2026, 10, 4), date(2026, 9, 13), date(2026, 8, 16)]
    assert fake.requests == [
        {"name": ELLIVERE, "first": 50, "after": None, "sortBy": "NEW", "minEventSize": 16},
    ]


def test_newest_first_follows_the_cursor_while_the_window_lasts() -> None:
    fake = FakeEdhtop16()
    inside = ellivere_page()
    inside["data"]["commander"]["entries"]["edges"] = edges(inside)[:3]
    fake.pages[ELLIVERE] = [inside, ellivere_page()]
    found = client_for(fake).entries(ELLIVERE, since=SINCE, min_event_size=16, sort=EntrySort.NEW, limit=400)
    assert found is not None
    assert len(found) == 6
    assert [r["after"] for r in fake.requests] == [None, "4"]


def test_best_first_keeps_reading_past_old_entries_and_honours_the_limit() -> None:
    fake = FakeEdhtop16()
    client = client_for(fake)
    found = client.entries(KINNAN, since=date(2026, 9, 1), min_event_size=16, sort=EntrySort.TOP, limit=1)
    assert found is not None
    assert [(e.standing, e.made_top_cut) for e in found] == [(1, True)]
    assert fake.requests[-1]["first"] == 1
    assert fake.requests[-1]["sortBy"] == "TOP"


def test_best_first_drops_entries_older_than_the_window() -> None:
    fake = FakeEdhtop16()
    page = copy.deepcopy(load_fixture("edhtop16_entries_kinnan_top.json"))
    page["data"]["commander"]["entries"]["pageInfo"] = {"hasNextPage": False, "endCursor": "2"}
    fake.pages[KINNAN] = [page]
    found = client_for(fake).entries(
        KINNAN, since=date(2026, 10, 1), min_event_size=16, sort=EntrySort.TOP, limit=2
    )
    assert found is not None
    assert [e.tournament.held_on for e in found] == [date(2026, 10, 7)]


def test_a_commander_edhtop16_does_not_know_is_none() -> None:
    fake = FakeEdhtop16()
    found = client_for(fake).entries("Nobody", since=SINCE, min_event_size=16, sort=EntrySort.NEW, limit=10)
    assert found is None


def test_top_commanders_are_ranked_by_entries_in_the_window() -> None:
    fake = FakeEdhtop16()
    ranked = client_for(fake).top_commanders(since=SINCE, min_event_size=16, count=2)
    assert [
        (r.name, r.entries, r.top_cuts, round(r.conversion, 4), round(r.meta_share, 4)) for r in ranked
    ] == [
        (KINNAN, 840, 210, 0.25, 0.04),
        ("Kraum, Ludevic's Opus / Tymna the Weaver", 800, 240, 0.3, 0.038),
    ]
    assert fake.requests == [{"first": 4, "minDate": "2026-08-16", "minSize": 16}]


def test_the_shorter_window_reorders_the_api_ranking() -> None:
    fake = FakeEdhtop16()
    fake.commanders["data"]["commanders"]["edges"][3]["node"]["stats"]["count"] = 2000
    ranked = client_for(fake).top_commanders(since=SINCE, min_event_size=16, count=1)
    assert [r.name for r in ranked] == ["Sisay, Weatherlight Captain"]


def test_queries_are_paced() -> None:
    fake = FakeEdhtop16()
    clock = Clock()
    client = client_for(fake, clock)
    client.top_commanders(since=SINCE, min_event_size=16, count=1)
    client.top_commanders(since=SINCE, min_event_size=16, count=1)
    assert clock.sleeps == [1.5]


def test_a_refused_query_is_an_error() -> None:
    with pytest.raises(Exception, match="no data; errors: Cannot query field"):
        graphql_data({"errors": [{"message": 'Cannot query field "x"'}]})
    fake = FakeEdhtop16()
    fake.commanders = {"errors": [{"message": "Cannot query field"}]}
    with pytest.raises(Edhtop16Error, match="unexpected answer from EDHTop16"):
        client_for(fake).top_commanders(since=SINCE, min_event_size=16, count=1)


def test_a_server_error_is_a_network_error() -> None:
    fake = FakeEdhtop16()
    fake.status = 500
    with pytest.raises(NetworkError, match="HTTP 500"):
        client_for(fake).top_commanders(since=SINCE, min_event_size=16, count=1)


def test_spice_outside_its_scale_is_refused() -> None:
    fake = FakeEdhtop16()
    page = ellivere_page()
    edges(page)[0]["node"]["decklistItems"][0]["spice"] = 5
    fake.pages[ELLIVERE] = [page]
    with pytest.raises(Edhtop16Error, match=r"spice: 5 is outside 0-4"):
        client_for(fake).entries(ELLIVERE, since=SINCE, min_event_size=16, sort=EntrySort.NEW, limit=10)


@pytest.mark.parametrize("link", ["https://evil.example/x", "//evil.example/x", "decklist/x"])
def test_only_site_relative_decklist_links_are_kept(link: str) -> None:
    page = ellivere_page()
    edges(page)[0]["node"]["decklistUrl"] = link
    parsed = parse_entries_page(page["data"])
    assert parsed is not None
    assert parsed[0][0].decklist_url is None


def test_partners_are_named_in_alphabetical_order() -> None:
    assert commander_name(["Tymna the Weaver", "Kraum, Ludevic's Opus"]) == (
        "Kraum, Ludevic's Opus / Tymna the Weaver"
    )
    assert (
        commander_url("Magda, Brazen Outlaw") == "https://edhtop16.com/commander/Magda%2C%20Brazen%20Outlaw"
    )
    with pytest.raises(ValueError, match="needs a commander"):
        commander_name([])


def test_an_answer_that_is_not_graphql_is_refused() -> None:
    fake = FakeEdhtop16()

    def html(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    client = Edhtop16Client(
        Fetcher(httpx.Client(transport=httpx.MockTransport(html)), max_attempts=1), Pacer(0)
    )
    with pytest.raises(NetworkError, match="did not return JSON"):
        client.top_commanders(since=SINCE, min_event_size=16, count=1)
    assert fake.requests == []


def test_a_whole_number_rate_is_read_as_a_number_but_true_is_not() -> None:
    fake = FakeEdhtop16()
    stats = fake.commanders["data"]["commanders"]["edges"][0]["node"]["stats"]
    stats["conversionRate"] = 0
    assert client_for(fake).top_commanders(since=SINCE, min_event_size=16, count=1)[0].conversion == 0.0
    stats["conversionRate"] = True
    with pytest.raises(Edhtop16Error, match=r"conversionRate: expected a number"):
        client_for(fake).top_commanders(since=SINCE, min_event_size=16, count=1)


def test_best_first_reads_a_single_page() -> None:
    fake = FakeEdhtop16()
    found = client_for(fake).entries(
        KINNAN, since=date(2026, 10, 1), min_event_size=16, sort=EntrySort.TOP, limit=30
    )
    assert found is not None
    assert len(found) == 1
    assert [r["first"] for r in fake.requests] == [30]


def test_best_first_refuses_a_limit_a_single_page_cannot_hold() -> None:
    fake = FakeEdhtop16()

    with pytest.raises(ValueError, match="at most 50"):
        client_for(fake).entries(
            KINNAN, since=date(2026, 10, 1), min_event_size=16, sort=EntrySort.TOP, limit=51
        )

    assert fake.requests == []


def test_best_first_takes_a_full_page_and_newest_first_may_ask_for_more() -> None:
    fake = FakeEdhtop16()
    client = client_for(fake)

    client.entries(KINNAN, since=date(2026, 10, 1), min_event_size=16, sort=EntrySort.TOP, limit=50)
    client.entries(ELLIVERE, since=SINCE, min_event_size=16, sort=EntrySort.NEW, limit=400)

    assert [r["first"] for r in fake.requests] == [50, 50]


def test_errors_beside_usable_data_are_logged_and_the_data_is_used(
    caplog: pytest.LogCaptureFixture,
) -> None:
    answer = {
        "data": {"commander": {"name": "Kinnan"}},
        "errors": [{"message": "field spice is deprecated"}, {"message": "slow resolver"}],
    }

    with caplog.at_level(logging.WARNING, logger="commander.edhtop16"):
        data = graphql_data(answer)

    assert data == {"commander": {"name": "Kinnan"}}
    assert caplog.messages == [
        "EDHTop16 answered with data and errors: field spice is deprecated; slow resolver"
    ]


def test_errors_in_an_odd_shape_beside_data_are_still_only_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="commander.edhtop16"):
        data = graphql_data({"data": {"commanders": []}, "errors": "boom"})

    assert data == {"commanders": []}
    assert caplog.messages == [
        "EDHTop16 answered with data and errors: unreadable (answer.errors: expected a list, got str)"
    ]


def test_an_answer_with_data_and_no_errors_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="commander.edhtop16"):
        graphql_data({"data": {"commanders": []}, "errors": []})

    assert caplog.messages == []
