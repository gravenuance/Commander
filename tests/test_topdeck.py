"""The TopDeck client over a fake of the API: what it asks for, keeps and works out from the answers."""

import logging
import threading
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from commander.net import Fetcher, NetworkError, Pacer
from commander.topdeck import (
    API_URL,
    CHUNK_DAYS,
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


def test_the_first_run_asks_for_every_week_of_the_window_oldest_first(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    ask(client_for(fake, tmp_path))
    starts = [r["start"] for r in fake.requests]
    ends = [r["end"] for r in fake.requests]
    # 61 days from 16 August to 15 October: eight weeks and five days, back to back.
    assert len(fake.requests) == 9
    assert starts[0] == stamp(2026, 8, 16)
    assert ends[-1] == stamp(2026, 10, 15, last_second=True)
    assert all(end - start + 1 <= CHUNK_DAYS * DAY for start, end in zip(starts, ends, strict=True))
    assert starts[1:] == [end + 1 for end in ends[:-1]]
    body = fake.requests[0]
    assert (body["game"], body["format"], body["participantMin"], body["columns"]) == (
        "Magic: The Gathering",
        "EDH",
        64,
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


def test_the_next_day_asks_only_from_the_last_settled_day(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    ask(client_for(fake, tmp_path))
    fake.requests.clear()
    ask(client_for(fake, tmp_path), since=date(2026, 8, 17), until=date(2026, 10, 16))
    # The first run settled up to the end of 12 October (three days before the end of the 15th); events
    # that started later may still have been playing.
    [body] = fake.requests
    assert (body["start"], body["end"]) == (stamp(2026, 10, 13), stamp(2026, 10, 16, last_second=True))


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


def test_a_failed_week_keeps_what_came_before_it(tmp_path: Path) -> None:
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
    # Three weeks were stored; the rest of the nine is asked for, starting where the stored ones end.
    assert len(fake.requests) == 6
    assert fake.requests[0]["start"] == stamp(2026, 8, 16) + 3 * CHUNK_DAYS * DAY


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


def test_checking_a_key_asks_for_no_events(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    client_for(fake, tmp_path).check_key(TOPDECK_KEY)
    [body] = fake.requests
    assert body == {"last": 1, "participantMin": 100_000}
    with pytest.raises(TopdeckKeyError):
        client_for(fake, tmp_path).check_key("wrong")


def test_a_server_error_is_a_network_error(tmp_path: Path) -> None:
    fake = FakeTopdeck()
    fake.status = 500
    with pytest.raises(NetworkError):
        ask(client_for(fake, tmp_path))


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
