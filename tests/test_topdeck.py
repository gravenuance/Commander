"""The TopDeck client over a fake of the API: what it asks for, keeps and works out from the answers."""

import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from commander import net
from commander.metadata import topdeck_client
from commander.net import Fetcher, NetworkError, Pacer
from commander.topdeck import (
    API_URL,
    NO_KEY_MESSAGE,
    TopdeckClient,
    TopdeckError,
    TopdeckKeyError,
    parse_tournaments,
)
from commander.topdeckstore import FOLDER, TournamentStore
from commander.tournaments import DecklistCard, EntrySort
from conftest import TOPDECK_API, TOPDECK_KEY, FakeTopdeck, Json, MemoryKeyStore, topdeck_event

ELLIVERE = "Ellivere of the Wild Court"
KINNAN = "Kinnan, Bonder Prodigy"
SINCE = date(2026, 8, 16)
UNTIL = date(2026, 10, 15)
DAY = 24 * 60 * 60


def stamp(year: int, month: int, day: int, *, last_second: bool = False) -> int:
    """The first second of the UTC day, or its last."""
    midnight = int(datetime(year, month, day, tzinfo=UTC).timestamp())
    return midnight + DAY - 1 if last_second else midnight


def client_for(fake: FakeTopdeck, root: Path, keys: MemoryKeyStore | None = None) -> TopdeckClient:
    return TopdeckClient(
        Fetcher(fake.client(), max_attempts=1), Pacer(0), TournamentStore(root), keys or MemoryKeyStore()
    )


def ask(client: TopdeckClient, name: str = ELLIVERE, **overrides: object) -> tuple[tuple[date, int], ...]:
    """The held-on date and standing of each entry the client gives for the commander."""
    options: dict[str, object] = {
        "since": SINCE,
        "until": UNTIL,
        "min_event_size": 64,
        "sort": EntrySort.NEW,
        "limit": 400,
    } | overrides
    found = client.entries(name, **options)  # pyright: ignore[reportArgumentType]
    return tuple((e.tournament.held_on, e.standing) for e in found or ())


def test_entries_come_newest_event_first_inside_the_window(tmp_path: Path) -> None:
    client = client_for(FakeTopdeck(), tmp_path)
    # The July event is older than the window.
    assert ask(client) == ((date(2026, 10, 4), 12), (date(2026, 9, 13), 5), (date(2026, 8, 16), 21))


def test_the_best_placings_come_first_as_a_share_of_the_field(tmp_path: Path) -> None:
    client = client_for(FakeTopdeck(), tmp_path)
    # 4 of 64 is 0.0625, 11 of 70 is 0.157, 20 of 72 is 0.278.
    assert ask(client, sort=EntrySort.TOP) == (
        (date(2026, 9, 13), 5),
        (date(2026, 10, 4), 12),
        (date(2026, 8, 16), 21),
    )
    assert ask(client, sort=EntrySort.TOP, limit=1) == ((date(2026, 9, 13), 5),)


def test_an_entry_carries_the_decks_cards_and_its_events_numbers(tmp_path: Path) -> None:
    client = client_for(FakeTopdeck(), tmp_path)
    entry = client.entries(
        ELLIVERE, since=date(2026, 10, 1), until=UNTIL, min_event_size=64, sort=EntrySort.NEW, limit=5
    )
    assert entry is not None
    [only] = entry
    assert (only.tournament.tid, only.tournament.size, only.tournament.top_cut) == ("test-open-1", 70, 8)
    assert only.cards[0] == DecklistCard("Smothering Tithe", 1)
    assert len(only.cards) == 7
    assert not only.made_top_cut


def test_a_commander_no_event_had_is_unknown(tmp_path: Path) -> None:
    client = client_for(FakeTopdeck(), tmp_path)
    assert (
        client.entries("Nobody", since=SINCE, until=UNTIL, min_event_size=64, sort=EntrySort.NEW, limit=9)
        is None
    )


def test_smaller_events_than_asked_for_are_left_out(tmp_path: Path) -> None:
    client = client_for(FakeTopdeck(), tmp_path)
    # Only the 72-player event of August has at least 72 players.
    assert ask(client, min_event_size=72) == ((date(2026, 8, 16), 21),)


def test_the_commanders_are_ranked_by_decks_played_with_shares_of_all_the_decks(tmp_path: Path) -> None:
    client = client_for(FakeTopdeck(), tmp_path)
    ranked = client.top_commanders(since=SINCE, until=UNTIL, min_event_size=64, count=5)
    # Three Ellivere decks, none in a top cut (standings 12 of 8, 5 of none, 21 of 4); two Kinnan decks,
    # both winners. Five decks in all.
    assert [(c.name, c.entries, c.top_cuts, c.conversion, c.meta_share) for c in ranked] == [
        (ELLIVERE, 3, 0, 0.0, 0.6),
        (KINNAN, 2, 2, 1.0, 0.4),
    ]
    assert [c.name for c in client.top_commanders(since=SINCE, until=UNTIL, min_event_size=64, count=1)] == [
        ELLIVERE
    ]


def test_partners_are_filed_under_both_names_in_alphabetical_order(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    pair = ["Tymna the Weaver", "Kraum, Ludevic's Opus"]
    fake.events = [topdeck_event("pair-1", "2026-10-01T10:00:00+00:00", 64, 8, [(3, pair, ["Sol Ring"])])]
    client = client_for(fake, tmp_path)
    [stats] = client.top_commanders(since=SINCE, until=UNTIL, min_event_size=64, count=3)
    assert stats.name == "Kraum, Ludevic's Opus / Tymna the Weaver"
    assert ask(client, "kraum, ludevic's opus / tymna the weaver") == ((date(2026, 10, 1), 3),)


# What is asked for, and when


def test_the_first_run_asks_for_every_chunk_of_the_window_oldest_first(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    ask(client_for(fake, tmp_path))
    starts = [r["start"] for r in fake.requests]
    ends = [r["end"] for r in fake.requests]
    # 61 days from 16 August to 15 October: eight chunks of seven days and one of five, back to back.
    assert [(end - start + 1) // DAY for start, end in zip(starts, ends, strict=True)] == [7] * 8 + [5]
    assert starts[0] == stamp(2026, 8, 16)
    assert ends[-1] == stamp(2026, 10, 15, last_second=True)
    assert starts[1:] == [end + 1 for end in ends[:-1]]
    body = fake.requests[0]
    assert (body["game"], body["format"], body["participantMin"], body["columns"]) == (
        "Magic: The Gathering",
        "EDH",
        16,
        ["decklist"],
    )


def test_the_key_goes_in_the_authorization_header_only(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    client = TopdeckClient(
        Fetcher(httpx.Client(transport=httpx.MockTransport(handler)), max_attempts=1),
        Pacer(0),
        TournamentStore(tmp_path),
        MemoryKeyStore(),
    )
    ask(client)
    assert {str(r.url) for r in seen} == {API_URL} == {TOPDECK_API}
    assert {r.headers["Authorization"] for r in seen} == {TOPDECK_KEY}
    assert all(TOPDECK_KEY not in r.content.decode() for r in seen)


def test_asking_again_the_same_day_asks_nothing(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    first = ask(client_for(fake, tmp_path))
    asked = len(fake.requests)
    # A new client, as the next run has: the stored events and the coverage are read from the folder.
    assert ask(client_for(fake, tmp_path)) == first
    assert len(fake.requests) == asked


def test_a_store_from_another_version_is_fetched_again(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    first = ask(client_for(fake, tmp_path))
    asked = len(fake.requests)
    # A store filled at 64+ players (version 1) does not cover the smaller events.
    coverage = tmp_path / FOLDER / "coverage.json"
    coverage.write_text(coverage.read_text(encoding="utf-8").replace('"version":2', '"version":1'), "utf-8")

    assert ask(client_for(fake, tmp_path)) == first
    assert len(fake.requests) == 2 * asked


def test_the_next_day_asks_only_from_the_last_settled_day(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    ask(client_for(fake, tmp_path))
    fake.requests.clear()
    ask(client_for(fake, tmp_path), since=date(2026, 8, 17), until=date(2026, 10, 16))
    # The first run settled up to the end of 12 October (three days before the end of the 15th); events
    # that started later may still have been playing. Four days take one chunk.
    assert [(r["start"], r["end"]) for r in fake.requests] == [
        (stamp(2026, 10, 13), stamp(2026, 10, 16, last_second=True))
    ]


def test_an_event_that_finished_after_the_last_run_arrives_with_its_decks(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    fake.events = [topdeck_event("late-1", "2026-10-14T10:00:00+00:00", 64, 8, [])]
    client = client_for(fake, tmp_path)
    assert ask(client) == ()
    # The 14th is inside the days asked for again, so the decks that were not shown yet are picked up.
    fake.events = [
        topdeck_event("late-1", "2026-10-14T10:00:00+00:00", 64, 8, [(2, [ELLIVERE], ["Sol Ring"])])
    ]
    assert ask(client_for(fake, tmp_path), since=date(2026, 8, 17), until=date(2026, 10, 16)) == (
        (date(2026, 10, 14), 2),
    )


def test_a_wider_window_than_the_stored_one_is_fetched_whole(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    ask(client_for(fake, tmp_path), since=date(2026, 9, 1))
    fake.requests.clear()
    found = ask(client_for(fake, tmp_path), since=date(2026, 7, 1))
    assert (found[-1][0], len(found)) == (date(2026, 7, 19), 4)
    assert fake.requests[0]["start"] == stamp(2026, 7, 1)


def test_events_older_than_the_window_are_dropped_from_the_folder(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    ask(client_for(fake, tmp_path), since=date(2026, 7, 1))
    assert len(list((tmp_path / FOLDER / "events").glob("*.json"))) == 6
    ask(client_for(fake, tmp_path), since=date(2026, 8, 17), until=date(2026, 10, 16))
    assert len(list((tmp_path / FOLDER / "events").glob("*.json"))) == 4


def test_a_failed_chunk_keeps_what_came_before_it(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    calls = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503) if calls > 3 else fake.handler(request)

    broken = TopdeckClient(
        Fetcher(httpx.Client(transport=httpx.MockTransport(flaky)), max_attempts=1),
        Pacer(0),
        TournamentStore(tmp_path),
        MemoryKeyStore(),
    )
    with pytest.raises(NetworkError):
        ask(broken)
    fake.requests.clear()
    ask(client_for(fake, tmp_path))
    # Three chunks of seven days were stored; the rest of the 9 is asked for, starting where they end.
    assert len(fake.requests) == 6
    assert fake.requests[0]["start"] == stamp(2026, 8, 16) + 21 * DAY


def test_two_readers_at_once_fetch_the_window_once(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    client = client_for(fake, tmp_path)
    results: list[object] = []

    def read() -> None:
        results.append(ask(client))

    threads = [threading.Thread(target=read) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(fake.requests) == 9
    assert results[0] == results[1]


# Loading the window up front


def test_loading_the_window_fetches_it_once_for_the_reads_that_follow(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    client = client_for(fake, tmp_path)
    client.load(since=SINCE, until=UNTIL)
    assert len(fake.requests) == 9
    assert len(ask(client)) == 3
    assert len(ask(client, KINNAN)) == 2
    assert len(fake.requests) == 9


def test_a_failed_load_raises_nothing_and_the_reads_that_follow_get_the_failure_unsent(
    tmp_path: Path,
) -> None:
    fake = FakeTopdeck()
    fake.status = 503
    client = client_for(fake, tmp_path)
    client.load(since=SINCE, until=UNTIL)
    assert len(fake.requests) == 1
    with pytest.raises(NetworkError):
        ask(client)
    assert len(fake.requests) == 1


# Rate limit


class RecordingPacer(Pacer):
    """A pacer on a fake clock that records the cool-downs it is asked to hold and the pauses it slept."""

    def __init__(self, interval: float) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.cool_downs: list[float] = []
        super().__init__(interval, sleep=self._sleep, monotonic=lambda: self.now)

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def cool_down(self, seconds: float) -> None:
        self.cool_downs.append(seconds)
        super().cool_down(seconds)


# Four days to the 15th: the last three are never settled, so this is the shortest window that is fetched.
FOUR_DAYS = date(2026, 10, 12)


def limited_client(root: Path, pacer: Pacer, *answers: httpx.Response) -> tuple[TopdeckClient, list[int]]:
    """A client whose requests get `answers` in turn, then empty 200s; the list counts the requests."""
    asked: list[int] = []
    waiting = list(answers)

    def handler(_: httpx.Request) -> httpx.Response:
        asked.append(1)
        return waiting.pop(0) if waiting else httpx.Response(200, json=[])

    # One attempt only: a 429 that counted as an attempt would end the run here.
    fetcher = Fetcher(httpx.Client(transport=httpx.MockTransport(handler)), max_attempts=1)
    return TopdeckClient(fetcher, pacer, TournamentStore(root), MemoryKeyStore()), asked


def test_a_429_holds_the_pacer_for_its_retry_after_and_the_request_goes_again(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pacer = RecordingPacer(12.0)
    limited = httpx.Response(429, headers={"Retry-After": "30"})
    client, asked = limited_client(tmp_path, pacer, limited, limited, httpx.Response(200, json=[]))
    with caplog.at_level(logging.INFO, logger="commander.net"):
        ask(client, since=FOUR_DAYS)
    assert pacer.cool_downs == [30.0, 30.0]
    assert pacer.sleeps == [30.0, 30.0]
    assert len(asked) == 3
    waits = [r for r in caplog.records if "waiting" in r.getMessage()]
    assert [(r.levelno, "waiting 30 s" in r.getMessage()) for r in waits] == [(logging.INFO, True)] * 2


def test_a_429_with_the_wait_in_its_json_body_is_waited_out_too(tmp_path: Path) -> None:
    pacer = RecordingPacer(12.0)
    body = httpx.Response(429, json={"error": "Too many requests", "retryAfterSeconds": 17})
    client, _ = limited_client(tmp_path, pacer, body)
    ask(client, since=FOUR_DAYS)
    assert pacer.cool_downs == [17.0]


def test_a_429_that_says_nothing_about_how_long_is_waited_out_for_half_a_minute(tmp_path: Path) -> None:
    pacer = RecordingPacer(12.0)
    client, _ = limited_client(tmp_path, pacer, httpx.Response(429))
    ask(client, since=FOUR_DAYS)
    assert pacer.cool_downs == [30.0]


def test_a_request_gives_up_when_the_429_waits_add_up_to_more_than_five_minutes(tmp_path: Path) -> None:
    pacer = RecordingPacer(12.0)
    limited = httpx.Response(429, headers={"Retry-After": "200"})
    client, asked = limited_client(tmp_path, pacer, limited, limited, limited)
    with pytest.raises(NetworkError, match="rate limited"):
        ask(client, since=FOUR_DAYS)
    # 200 s was waited; another 200 would pass the 300 s cap, so it is not.
    assert pacer.cool_downs == [200.0]
    assert len(asked) == 2
    # The failure is remembered, so the run sends nothing more.
    with pytest.raises(NetworkError, match="rate limited"):
        ask(client, KINNAN, since=FOUR_DAYS)
    assert len(asked) == 2


def test_a_key_check_that_gets_a_429_does_not_wait_and_says_when_to_try_again(tmp_path: Path) -> None:
    pacer = RecordingPacer(12.0)
    limited = httpx.Response(429, headers={"Retry-After": "30"})
    client, asked = limited_client(tmp_path, pacer, limited, httpx.Response(200, json=[]))
    with pytest.raises(TopdeckError, match=r"^TopDeck is busy; try again in 30 s$"):
        client.check_key(TOPDECK_KEY)
    assert len(asked) == 1
    assert (pacer.sleeps, pacer.cool_downs) == ([], [])


def test_a_key_check_that_gets_a_429_without_a_time_says_a_minute(tmp_path: Path) -> None:
    client, asked = limited_client(tmp_path, RecordingPacer(12.0), httpx.Response(429))
    with pytest.raises(TopdeckError, match=r"^TopDeck is busy; try again in a minute$"):
        client.check_key(TOPDECK_KEY)
    assert len(asked) == 1


def test_the_429_waits_of_one_request_do_not_count_against_the_next(tmp_path: Path) -> None:
    pacer = RecordingPacer(12.0)
    limited = httpx.Response(429, headers={"Retry-After": "200"})
    client, asked = limited_client(tmp_path, pacer, limited, httpx.Response(200, json=[]), limited)
    # Eleven days: a chunk of seven, then one of four.
    ask(client, since=date(2026, 10, 5))
    assert pacer.cool_downs == [200.0, 200.0]
    assert len(asked) == 4


def test_requests_start_twelve_seconds_apart(tmp_path: Path) -> None:
    intervals: list[float] = []

    def pacer_factory(interval: float) -> Pacer:
        intervals.append(interval)
        return Pacer(interval)

    topdeck_client(httpx.Client(), pacer_factory, tmp_path)
    assert intervals == [12.0]


# Answers over the size cap


CAP = 200_000


def capped_client(
    fake: FakeTopdeck, root: Path, monkeypatch: pytest.MonkeyPatch, too_big: Callable[[int, int], bool]
) -> TopdeckClient:
    """A client whose HTTP cap is CAP bytes, over a fake that answers a span with CAP + 1 bytes when
    `too_big(start, end)` says so."""
    monkeypatch.setattr(net, "MAX_RESPONSE_BYTES", CAP)

    def handler(request: httpx.Request) -> httpx.Response:
        body: Json = json.loads(request.content)
        if too_big(body["start"], body["end"]):
            fake.requests.append(body)
            return httpx.Response(200, content=b"x" * (CAP + 1))
        return fake.handler(request)

    return TopdeckClient(
        Fetcher(httpx.Client(transport=httpx.MockTransport(handler)), max_attempts=1),
        Pacer(0),
        TournamentStore(root),
        MemoryKeyStore(),
    )


def test_an_answer_over_the_cap_is_asked_for_again_in_halves_down_to_what_fits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeTopdeck()
    # Nothing longer than a day fits.
    client = capped_client(fake, tmp_path, monkeypatch, lambda start, end: end - start + 1 > DAY)
    with caplog.at_level(logging.INFO):
        found = ask(client)
    assert found == ((date(2026, 10, 4), 12), (date(2026, 9, 13), 5), (date(2026, 8, 16), 21))
    # The first seven days: the whole span, then its first three, which split down to single days, then the
    # last four.
    spans = [(r["start"], r["end"]) for r in fake.requests[:7]]
    assert spans == [
        (stamp(2026, 8, 16), stamp(2026, 8, 22, last_second=True)),
        (stamp(2026, 8, 16), stamp(2026, 8, 18, last_second=True)),
        (stamp(2026, 8, 16), stamp(2026, 8, 16, last_second=True)),
        (stamp(2026, 8, 17), stamp(2026, 8, 18, last_second=True)),
        (stamp(2026, 8, 17), stamp(2026, 8, 17, last_second=True)),
        (stamp(2026, 8, 18), stamp(2026, 8, 18, last_second=True)),
        (stamp(2026, 8, 19), stamp(2026, 8, 22, last_second=True)),
    ]
    assert "over the size cap" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    # The window is covered: a second run asks for nothing.
    asked = len(fake.requests)
    assert ask(client_for(fake, tmp_path)) == found
    assert len(fake.requests) == asked


def test_a_day_that_alone_is_over_the_cap_is_skipped_and_coverage_moves_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeTopdeck()
    day = stamp(2026, 9, 13)
    client = capped_client(
        fake, tmp_path, monkeypatch, lambda start, end: start <= day + DAY - 1 and end >= day
    )
    with caplog.at_level(logging.INFO):
        found = ask(client)
    # The 13 September event is lost; the rest of the window stands.
    assert found == ((date(2026, 10, 4), 12), (date(2026, 8, 16), 21))
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "2026-09-13" in warnings[0]
    asked = len(fake.requests)
    assert ask(client_for(fake, tmp_path)) == found
    assert len(fake.requests) == asked


def test_another_network_error_is_not_split(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    fake.status = 503
    with pytest.raises(NetworkError):
        ask(client_for(fake, tmp_path))
    assert len(fake.requests) == 1


# Keys


def test_without_a_key_nothing_is_asked_and_the_error_says_what_to_do(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    with pytest.raises(TopdeckKeyError, match="no TopDeck key") as raised:
        ask(client_for(fake, tmp_path, MemoryKeyStore(None)))
    assert str(raised.value) == NO_KEY_MESSAGE
    assert fake.requests == []


def test_a_refused_key_is_reported_without_the_key(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    with pytest.raises(TopdeckKeyError) as raised:
        ask(client_for(fake, tmp_path, MemoryKeyStore("not-the-key")))
    assert "refused the key" in str(raised.value)
    assert "not-the-key" not in str(raised.value)


def test_a_stored_window_needs_no_key(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    ask(client_for(fake, tmp_path))
    assert len(ask(client_for(fake, tmp_path, MemoryKeyStore(None)))) == 3


def test_the_next_day_without_a_key_answers_from_the_store_and_says_why_it_is_stale(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeTopdeck()
    ask(client_for(fake, tmp_path))
    fake.requests.clear()
    client = client_for(fake, tmp_path, MemoryKeyStore(None))
    with caplog.at_level(logging.WARNING):
        found = ask(client, since=date(2026, 8, 17), until=date(2026, 10, 16))
    assert found == ((date(2026, 10, 4), 12), (date(2026, 9, 13), 5))
    note = client.stale_note()
    assert note is not None
    assert (note.reason, note.key_problem) == (NO_KEY_MESSAGE, True)
    # The store is complete to the end of 12 October (three days before the end of the 15th).
    assert note.as_of == datetime(2026, 10, 12, 23, 59, 59, tzinfo=UTC)
    assert fake.requests == []
    assert "TopDeck refresh failed" in caplog.text


def test_the_next_day_with_a_refused_key_or_a_down_service_answers_from_the_store(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    ask(client_for(fake, tmp_path))
    refused = client_for(fake, tmp_path, MemoryKeyStore("not-the-key"))
    assert len(ask(refused, since=date(2026, 8, 17), until=date(2026, 10, 16))) == 2
    refusal = refused.stale_note()
    assert refusal is not None
    assert refusal.key_problem
    fake.status = 503
    down = client_for(fake, tmp_path)
    assert len(ask(down, since=date(2026, 8, 17), until=date(2026, 10, 16))) == 2
    note = down.stale_note()
    assert note is not None
    assert not note.key_problem
    assert "503" in note.reason


def test_a_window_the_store_never_saw_still_fails_without_a_key(tmp_path: Path) -> None:
    ask(client_for(FakeTopdeck(), tmp_path))
    client = client_for(FakeTopdeck(), tmp_path, MemoryKeyStore(None))
    with pytest.raises(TopdeckKeyError):
        ask(client, since=date(2026, 1, 1), until=date(2026, 1, 31))


def test_a_fresh_store_has_no_stale_note(tmp_path: Path) -> None:
    client = client_for(FakeTopdeck(), tmp_path)
    ask(client)
    assert client.stale_note() is None


def test_checking_a_key_asks_for_no_events(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    client_for(fake, tmp_path).check_key(TOPDECK_KEY)
    [body] = fake.requests
    assert body == {"game": "Magic: The Gathering", "format": "EDH", "last": 1, "participantMin": 100_000}
    with pytest.raises(TopdeckKeyError):
        client_for(fake, tmp_path).check_key("wrong")


def test_a_server_error_is_a_network_error(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    fake.status = 500
    with pytest.raises(NetworkError):
        ask(client_for(fake, tmp_path))


def test_after_a_network_failure_the_client_sends_nothing_more(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    fake.status = 503
    client = client_for(fake, tmp_path)
    with pytest.raises(NetworkError):
        ask(client)
    assert len(fake.requests) == 1
    # Another commander, another window and the meta field all read through the same client.
    with pytest.raises(NetworkError):
        ask(client, KINNAN)
    with pytest.raises(NetworkError):
        ask(client, since=date(2026, 9, 1))
    with pytest.raises(NetworkError):
        client.top_commanders(since=SINCE, until=UNTIL, min_event_size=64, count=3)
    assert len(fake.requests) == 1


def test_after_a_refused_key_the_client_sends_nothing_more(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    client = client_for(fake, tmp_path, MemoryKeyStore("not-the-key"))
    with pytest.raises(TopdeckKeyError, match="refused the key"):
        ask(client)
    with pytest.raises(TopdeckKeyError, match="refused the key"):
        ask(client, KINNAN)
    assert len(fake.requests) == 1


def test_a_failure_is_forgotten_with_the_client(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    fake.status = 503
    with pytest.raises(NetworkError):
        ask(client_for(fake, tmp_path))
    fake.status = 200
    assert len(ask(client_for(fake, tmp_path))) == 3


def test_an_answer_that_is_not_a_list_is_refused(tmp_path: Path) -> None:
    client = TopdeckClient(
        Fetcher(
            httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"error": "x"}))),
            max_attempts=1,
        ),
        Pacer(0),
        TournamentStore(tmp_path),
        MemoryKeyStore(),
    )
    with pytest.raises(TopdeckError, match="unexpected answer from TopDeck"):
        ask(client)


# What is kept


def test_no_player_name_or_id_reaches_the_folder(tmp_path: Path) -> None:
    ask(client_for(FakeTopdeck(), tmp_path))
    text = "".join(path.read_text(encoding="utf-8") for path in (tmp_path / FOLDER).rglob("*.json"))
    assert "Ellivere of the Wild Court" in text
    assert "Player" not in text
    assert "player-" not in text
    assert TOPDECK_KEY not in text


def test_an_unreadable_stored_event_is_skipped_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeTopdeck()
    ask(client_for(fake, tmp_path))
    [target] = (tmp_path / FOLDER / "events").glob("test-open-1-*.json")
    target.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        found = ask(client_for(fake, tmp_path))
    assert found == ((date(2026, 9, 13), 5), (date(2026, 8, 16), 21))
    assert "ignoring unreadable cached TopDeck event" in caplog.text


# Reading an answer


def test_the_standing_is_the_place_in_the_answer_and_players_without_decks_still_count_in_the_size() -> None:
    answer = [
        topdeck_event(
            "e1", "2026-10-01T10:00:00+00:00", 64, 8, [(1, [ELLIVERE], ["Sol Ring"]), (40, [KINNAN], [])]
        )
    ]
    [record] = parse_tournaments(answer)
    assert (record.tournament.tid, record.tournament.size, record.tournament.top_cut) == ("e1", 64, 8)
    assert [(d.commander, d.standing) for d in record.decks] == [(ELLIVERE, 1), (KINNAN, 40)]


def test_cards_with_a_count_under_one_are_dropped_and_counts_are_kept() -> None:
    event = topdeck_event(
        "e1", "2026-10-01T10:00:00+00:00", 64, 8, [(1, [ELLIVERE], ["Sol Ring", "Forest", "Mox"])]
    )
    main = event["standings"][0]["deckObj"]["Mainboard"]
    main["Forest"]["count"] = 12
    main["Mox"]["count"] = 0
    [record] = parse_tournaments([event])
    assert record.decks[0].cards == (DecklistCard("Sol Ring", 1), DecklistCard("Forest", 12))


def test_a_malformed_event_or_deck_is_skipped_and_the_rest_read(caplog: pytest.LogCaptureFixture) -> None:
    good = topdeck_event("good", "2026-10-01T10:00:00+00:00", 64, 8, [(1, [ELLIVERE], ["Sol Ring"])])
    no_start: Json = {"TID": "bad-1", "standings": []}
    odd_deck = topdeck_event("odd", "2026-10-01T10:00:00+00:00", 64, 8, [(1, [ELLIVERE], ["Sol Ring"])])
    odd_deck["standings"][0]["deckObj"]["Mainboard"] = "nonsense"
    with caplog.at_level(logging.WARNING):
        records = parse_tournaments([no_start, good, odd_deck, "nonsense"])
    assert [r.tournament.tid for r in records] == ["good", "odd"]
    assert records[1].decks == ()
    assert "Skipping malformed TopDeck event #0" in caplog.text
    assert "Skipping malformed TopDeck event #3" in caplog.text
    assert "Skipped 1 malformed TopDeck decks in event odd" in caplog.text


def test_a_deck_without_a_commander_is_left_out() -> None:
    event = topdeck_event("e1", "2026-10-01T10:00:00+00:00", 64, 8, [(1, [ELLIVERE], ["Sol Ring"])])
    event["standings"][0]["deckObj"]["Commanders"] = {}
    [record] = parse_tournaments([event])
    assert record.decks == ()


class LockedStore(TournamentStore):
    """A store that cannot be written, as when an indexer holds the file or the disk is full."""

    def save(self, record: object) -> None:  # noqa: ARG002
        raise PermissionError("denied")


def test_a_store_that_cannot_be_written_is_a_topdeck_error_and_the_client_then_sends_nothing_more(
    tmp_path: Path,
) -> None:
    fake = FakeTopdeck()
    client = TopdeckClient(
        Fetcher(fake.client(), max_attempts=1), Pacer(0), LockedStore(tmp_path), MemoryKeyStore()
    )
    client.load(since=SINCE, until=UNTIL)
    assert len(fake.requests) == 1
    with pytest.raises(TopdeckError, match="could not be stored: denied"):
        ask(client)
    with pytest.raises(TopdeckError, match="could not be stored"):
        ask(client, KINNAN)
    assert len(fake.requests) == 1
