import json
import os
import shutil
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from commander.archidekt import ArchidektSearch, PeerDeck
from commander.config import MetaSettings
from commander.deckcache import AnswerSource, request_key
from commander.deckreads import FOLDER as DECK_READS_FOLDER
from commander.deckreads import DeckRead, DeckReads
from commander.metadata import (
    CACHE_FOLDER,
    AdjacentDecks,
    MetaData,
    MetaSources,
    PeerLists,
    cache_stamp,
    gather,
    gather_adjacent,
    gather_for,
    start_gather_for,
    start_tournament_refresh,
    window_start,
)
from commander.models import Deck, DeckCard
from commander.net import HTTP_OK, Fetcher, Pacer
from commander.spellbook import SpellbookClient
from commander.topdeck import NO_KEY_MESSAGE, TopdeckClient
from commander.topdeckstore import TournamentStore
from conftest import (
    DECK_ID,
    FakeSpellbook,
    FakeTopdeck,
    Json,
    MemoryKeyStore,
    card_entry,
    load_fixture,
    topdeck_event,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
ELLIVERE = "Ellivere of the Wild Court"
MAGDA = "Magda, Brazen Outlaw"
KODAMA = "Kodama of the East Tree"
SETTINGS = MetaSettings(window_days=60, min_event_size=64, peer_decks=3, meta_commanders=2, meta_lists=30)


class FakeArchidektSearch:
    """The trimmed real Ellivere search page, and the Magda fixture for any deck read except `broken` ones."""

    def __init__(self) -> None:
        self.broken: set[int] = set()
        # Decks whose read gets this HTTP status instead.
        self.refused: dict[int, int] = {}
        # The commander every served deck is led by (the fixture deck's Magda renamed).
        self.commander = ELLIVERE
        # Decks served with Kodama of the East Tree as the commander's partner.
        self.partnered: set[int] = set()
        # Decks led by another commander than `commander`.
        self.led_by: dict[int, str] = {}
        self.status = 200
        # The search answer; the trimmed real page when None.
        self.page: Json | None = None
        self.requests: list[str] = []
        # The query of each search request, and the id of each deck read.
        self.searches: list[dict[str, str]] = []
        self.deck_reads: list[int] = []
        # Deck reads past this many get a 429.
        self.limit_reads: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        if request.url.path == "/api/decks/v3/":
            self.searches.append(dict(request.url.params))
        if self.status != HTTP_OK:
            return httpx.Response(self.status)
        if request.url.path == "/api/decks/v3/":
            return httpx.Response(200, json=self.page or load_fixture("archidekt_search_ellivere.json"))
        deck_id = int(request.url.path.strip("/").split("/")[-1])
        if self.limit_reads is not None and len(self.deck_reads) >= self.limit_reads:
            return httpx.Response(429)
        self.deck_reads.append(deck_id)
        if deck_id in self.refused:
            return httpx.Response(self.refused[deck_id])
        if deck_id in self.broken:
            return httpx.Response(200, json={"id": deck_id})
        deck = load_fixture(f"deck_{DECK_ID}.json")
        for entry in deck["cards"]:
            if entry["card"]["oracleCard"]["name"] == MAGDA:
                entry["card"]["oracleCard"]["name"] = self.led_by.get(deck_id, self.commander)
        if deck_id in self.partnered:
            deck["cards"].append(card_entry(9_200, KODAMA, ["Commander"]))
        return httpx.Response(200, json=deck)


def ellivere_deck(*commanders: str) -> Deck:
    """The deck led by Ellivere, or by the commanders named instead."""

    def card(entry_id: int, name: str, categories: tuple[str, ...]) -> DeckCard:
        return DeckCard(name, 1, categories, True, f"id-{entry_id}", entry_id, entry_id, "tst", "1", "Normal")  # noqa: FBT003 — positional like the model

    return Deck(
        10000002,
        "Let's all play nice",
        "TESTER",
        5,
        "2026-10-15T20:22:13Z",
        (
            *(card(n, name, ("Commander",)) for n, name in enumerate(commanders or (ELLIVERE,), 1)),
            card(10, "Containment Priest", ("Stax",)),
        ),
    )


# The 61 days of the window from 16 August to 15 October, asked for seven days at a time.
WINDOW_REQUESTS = 9


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.topdeck = FakeTopdeck()
        self.archidekt = FakeArchidektSearch()
        self.spellbook = FakeSpellbook()
        self.spellbook.combos = load_fixture("spellbook_meta_kinnan.json")
        self.cache = tmp_path / "cache"
        # The pauses Archidekt's pacer would have slept.
        self.sleeps: list[float] = []
        self.now = NOW
        self.keys = MemoryKeyStore()
        self.settings = SETTINGS

    def sources(self) -> MetaSources:
        return MetaSources(
            TopdeckClient(
                Fetcher(self.topdeck.client(), max_attempts=1),
                Pacer(0),
                TournamentStore(self.cache),
                self.keys,
            ),
            ArchidektSearch(
                Fetcher(httpx.Client(transport=httpx.MockTransport(self.archidekt.handler)), max_attempts=1),
                Pacer(0, sleep=self.sleeps.append),
            ),
            SpellbookClient(Fetcher(self.spellbook.client(), max_attempts=1)),
            Pacer(0),
            "tester",
            self.settings,
        )

    def gather(self, *, refresh: bool = False, deck: Deck | None = None) -> MetaData:
        return gather(
            deck or ellivere_deck(), self.sources(), self.cache, now=lambda: self.now, refresh=refresh
        )


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_the_window_runs_back_from_today_in_utc() -> None:
    assert window_start(NOW, SETTINGS) == date(2026, 8, 16)
    late_evening = datetime(2026, 10, 15, 23, 30, tzinfo=UTC).astimezone()
    assert window_start(late_evening, SETTINGS) == date(2026, 8, 16)


def test_gathers_the_commander_lists_the_meta_and_archidekt_decks(rig: Rig) -> None:
    data = rig.gather()
    assert data.commander == ELLIVERE
    assert data.since == date(2026, 8, 16)
    lists = data.lists.data
    assert lists is not None
    assert lists.known
    assert [e.tournament.held_on for e in lists.entries] == [
        date(2026, 10, 4),
        date(2026, 9, 13),
        date(2026, 8, 16),
    ]
    field = data.field.data
    assert field is not None
    # The two most-played commanders of the fake events: three Ellivere decks and two Kinnan decks.
    assert [(c.stats.name, len(c.entries)) for c in field] == [(ELLIVERE, 3), ("Kinnan, Bonder Prodigy", 2)]
    peers = data.peers.data
    assert peers is not None
    # The three most viewed, the user's own deck left out.
    assert [(p.deck.id, p.deck.views) for p in peers.decks] == [
        (5553433, 2131),
        (13668856, 1847),
        (7432856, 1782),
    ]
    assert ELLIVERE in peers.decks[0].cards
    # Counts above one are kept, so a deck's basic lands can be counted (ADR 0019).
    multiples = peers.decks[0].multiples
    assert multiples
    assert all(count > 1 for _, count in multiples)
    assert peers.skipped == 0
    assert {data.lists.source, data.field.source, data.peers.source} == {AnswerSource.FETCHED}


def test_the_decks_read_step_down_the_view_bar_until_enough_pass(rig: Rig) -> None:
    rig.settings = replace(SETTINGS, peer_decks=60)
    peers = rig.gather().peers.data
    assert peers is not None
    # Three decks have 1000 views and five 500: under ten each time, so those five are read and no deck
    # under 500 views is.
    assert [p.deck.views for p in peers.decks] == [2131, 1847, 1782, 673, 607]


def bracketed(rig: Rig, bracket: int) -> MetaData:
    deck = replace(ellivere_deck(), bracket=bracket)
    return gather_for(deck, rig.sources(), rig.cache, now=lambda: rig.now, refresh=False)


@pytest.mark.parametrize("bracket", [4, 5])
def test_the_reads_that_need_no_tournaments_start_first_and_tournaments_are_asked_only_at_the_finish(
    rig: Rig, bracket: int
) -> None:
    deck = replace(ellivere_deck(), bracket=bracket)
    finish = start_gather_for(deck, rig.sources(), rig.cache, now=lambda: rig.now, refresh=False)
    assert rig.topdeck.requests == []
    data = finish()
    assert len(rig.topdeck.requests) == WINDOW_REQUESTS
    assert data.lists.source is AnswerSource.FETCHED
    assert data.peers.source is AnswerSource.FETCHED
    assert rig.archidekt.searches


def test_the_tournament_window_is_fetched_up_front_only_when_a_deck_reads_tournament_lists(rig: Rig) -> None:
    decks = [replace(ellivere_deck(), bracket=bracket) for bracket in (2, 3)]
    assert start_tournament_refresh(decks, rig.sources(), now=lambda: rig.now) is None
    assert rig.topdeck.requests == []
    refresh = start_tournament_refresh(
        [*decks, replace(ellivere_deck(), bracket=4)], rig.sources(), now=lambda: rig.now
    )
    assert refresh is not None
    refresh.result(timeout=30)
    assert len(rig.topdeck.requests) == WINDOW_REQUESTS


@pytest.mark.parametrize("bracket", [3, 4, 5])
def test_every_step_that_is_reported_started_is_reported_done(rig: Rig, bracket: int) -> None:
    started: list[str] = []
    finished: list[str] = []
    deck = replace(ellivere_deck(), bracket=bracket)
    gather_for(
        deck,
        rig.sources(),
        rig.cache,
        now=lambda: rig.now,
        refresh=False,
        on_progress=started.append,
        on_done=finished.append,
    )
    assert "Ellivere: Ellivere of the Wild Court decks" in started
    assert sorted(started) == sorted(finished)


def test_the_adjacent_reads_report_each_step_started_and_done(rig: Rig) -> None:
    started: list[str] = []
    finished: list[str] = []
    gather_adjacent(
        ellivere_deck(),
        [(MAGDA,)],
        rig.sources(),
        rig.cache,
        now=lambda: rig.now,
        refresh=False,
        tags=["Stax"],
        on_progress=started.append,
        on_done=finished.append,
    )
    assert started == ["Ellivere: Magda decks (alike)", "Ellivere: Stax decks (theme)"]
    assert finished == started


def test_a_step_names_the_deck_it_reads_for_not_only_the_commander_it_searches(rig: Rig) -> None:
    started: list[str] = []
    for deck in (ellivere_deck(), ellivere_deck(MAGDA)):
        gather_adjacent(
            deck,
            [("Ygra, Eater of All",)],
            rig.sources(),
            rig.cache,
            now=lambda: rig.now,
            refresh=False,
            tags=["Landfall"],
            on_progress=started.append,
        )
    assert started == [
        "Ellivere: Ygra decks (alike)",
        "Ellivere: Landfall decks (theme)",
        "Magda: Ygra decks (alike)",
        "Magda: Landfall decks (theme)",
    ]


def test_the_tournament_refresh_reports_its_step_started_before_it_returns_and_done_after(rig: Rig) -> None:
    started: list[str] = []
    finished: list[str] = []
    refresh = start_tournament_refresh(
        [replace(ellivere_deck(), bracket=5)],
        rig.sources(),
        now=lambda: rig.now,
        on_progress=started.append,
        on_done=finished.append,
    )
    assert refresh is not None
    assert started == ["TopDeck: recent tournaments"]
    refresh.result(timeout=30)
    assert finished == started


def test_a_bracket_3_deck_reads_brackets_3_and_4_of_archidekt_and_no_tournament_lists(rig: Rig) -> None:
    data = bracketed(rig, 3)
    assert [q["edhBracket"] for q in rig.archidekt.searches] == ["3", "4"]
    assert rig.topdeck.requests == []
    assert data.lists.source is AnswerSource.UNAVAILABLE
    assert data.peers.source is AnswerSource.FETCHED


def test_a_bracket_4_deck_reads_brackets_4_and_5_of_archidekt_and_the_commanders_tournament_lists(
    rig: Rig,
) -> None:
    data = bracketed(rig, 4)
    assert [q["edhBracket"] for q in rig.archidekt.searches] == ["4", "5"]
    # Only the commander's own lists: not the most-played commanders, nor Spellbook.
    assert len(rig.topdeck.requests) == WINDOW_REQUESTS
    assert rig.spellbook.combo_bodies == []
    assert data.lists.source is AnswerSource.FETCHED
    assert data.lists.data is not None
    assert len(data.lists.data.entries) == 3
    assert data.peers.source is AnswerSource.FETCHED
    assert data.field.source is AnswerSource.UNAVAILABLE


def test_a_bracket_4_deck_offline_asks_nothing(rig: Rig) -> None:
    deck = replace(ellivere_deck(), bracket=4)
    data = gather_for(deck, rig.sources(), rig.cache, now=lambda: NOW, refresh=False, offline=True)
    assert (rig.topdeck.requests, rig.archidekt.requests) == ([], [])
    assert (data.lists.source, data.peers.source) == (AnswerSource.UNAVAILABLE, AnswerSource.UNAVAILABLE)
    assert data.lists.error == "not fetched yet"


def test_a_bracket_5_deck_reads_only_bracket_5_of_archidekt(rig: Rig) -> None:
    bracketed(rig, 5)
    assert [q["edhBracket"] for q in rig.archidekt.searches] == ["5"]


def deck_files(rig: Rig) -> list[Path]:
    return sorted((rig.cache / CACHE_FOLDER / DECK_READS_FOLDER).glob("*.json"))


def plant(rig: Rig, deck_id: int, *, updated_days_ago: int, unused_days: int) -> Path:
    """A cached deck read of some other deck, listed as updated and last used that many days before NOW."""
    peer = PeerDeck(deck_id, "x", "someone", 900, NOW - timedelta(days=updated_days_ago), 5)
    folder = rig.cache / CACHE_FOLDER / DECK_READS_FOLDER
    DeckReads(folder).save(peer, DeckRead((ELLIVERE,), ("Mind Stone",), (), ()), NOW)
    path = folder / f"{deck_id}.json"
    stamp = (NOW - timedelta(days=unused_days)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def test_a_gather_prunes_decks_unused_for_a_month_or_out_of_the_window_and_keeps_the_rest(rig: Rig) -> None:
    unused = plant(rig, 101, updated_days_ago=5, unused_days=31)
    out_of_window = plant(rig, 102, updated_days_ago=61, unused_days=2)
    recent = plant(rig, 103, updated_days_ago=5, unused_days=29)
    rig.gather()
    assert (unused.exists(), out_of_window.exists(), recent.exists()) == (False, False, True)
    # The decks the run itself read or hit are used, whatever their file time was.
    assert {p.name for p in deck_files(rig)} == {"103.json", "13668856.json", "5553433.json", "7432856.json"}


def test_a_peers_only_gather_prunes_too(rig: Rig) -> None:
    unused = plant(rig, 101, updated_days_ago=5, unused_days=31)
    bracketed(rig, 3)
    assert not unused.exists()


def test_an_adjacent_gather_prunes_too(rig: Rig) -> None:
    unused = plant(rig, 101, updated_days_ago=5, unused_days=31)
    adjacent(rig, (ELLIVERE,))
    assert not unused.exists()


def test_gathers_told_not_to_prune_leave_the_deck_reads_alone(rig: Rig) -> None:
    unused = plant(rig, 101, updated_days_ago=5, unused_days=31)
    deck = ellivere_deck()
    for bracket in (5, 3):
        start_gather_for(
            replace(deck, bracket=bracket),
            rig.sources(),
            rig.cache,
            now=lambda: NOW,
            refresh=False,
            prune=False,
        )()
    gather_adjacent(
        deck, ((ELLIVERE,),), rig.sources(), rig.cache, now=lambda: NOW, refresh=False, prune=False
    )
    assert unused.exists()


def test_a_deck_hit_by_a_run_is_not_pruned_for_having_sat_unused(rig: Rig) -> None:
    rig.gather()
    for path in deck_files(rig):
        stamp = (NOW - timedelta(days=40)).timestamp()
        os.utime(path, (stamp, stamp))
    rig.gather(refresh=True)
    assert len(deck_files(rig)) == 3
    assert len(rig.archidekt.deck_reads) == 3


def test_an_offline_gather_never_prunes(rig: Rig) -> None:
    unused = plant(rig, 101, updated_days_ago=5, unused_days=31)
    out_of_window = plant(rig, 102, updated_days_ago=61, unused_days=2)
    gather(ellivere_deck(), rig.sources(), rig.cache, now=lambda: NOW, refresh=False, offline=True)
    gather_for(
        replace(ellivere_deck(), bracket=3),
        rig.sources(),
        rig.cache,
        now=lambda: NOW,
        refresh=False,
        offline=True,
    )
    gather_adjacent(
        ellivere_deck(), [(ELLIVERE,)], rig.sources(), rig.cache, now=lambda: NOW, refresh=False, offline=True
    )
    assert (unused.exists(), out_of_window.exists()) == (True, True)


def test_a_deck_unchanged_since_it_was_read_is_never_read_again(rig: Rig) -> None:
    rig.gather()
    assert rig.archidekt.deck_reads == [5553433, 13668856, 7432856]
    assert [p.name for p in deck_files(rig)] == ["13668856.json", "5553433.json", "7432856.json"]
    rig.now = NOW + timedelta(days=2)
    again = rig.gather(refresh=True)
    assert rig.archidekt.deck_reads == [5553433, 13668856, 7432856]
    assert again.peers.source is AnswerSource.FETCHED
    assert again.peers.data is not None
    assert [p.deck.id for p in again.peers.data.decks] == [5553433, 13668856, 7432856]


def test_a_deck_edited_since_it_was_read_is_read_again_alone(rig: Rig) -> None:
    rig.gather()
    page = load_fixture("archidekt_search_ellivere.json")
    [edited] = [r for r in page["results"] if r["id"] == 13668856]
    edited["updatedAt"] = "2026-10-14T08:00:00.000000Z"
    rig.archidekt.page = page
    rig.now = NOW + timedelta(days=2)
    data = rig.gather(refresh=True)
    assert rig.archidekt.deck_reads == [5553433, 13668856, 7432856, 13668856]
    assert data.peers.data is not None
    assert {p.deck.id: p.deck.updated_at.day for p in data.peers.data.decks}[13668856] == 14


def test_a_corrupt_deck_cache_file_is_a_miss_that_is_logged_and_repaired(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.gather()
    broken = rig.cache / CACHE_FOLDER / DECK_READS_FOLDER / "13668856.json"
    broken.write_text("{not json", encoding="utf-8")
    other = rig.cache / CACHE_FOLDER / DECK_READS_FOLDER / "5553433.json"
    other.write_text(
        json.dumps({"version": 1, "key": "x", "fetchedAt": "2026-10-15T12:00:00+00:00"}), encoding="utf-8"
    )
    with caplog.at_level("WARNING"):
        data = rig.gather(refresh=True)
    assert sorted(rig.archidekt.deck_reads[3:]) == [5553433, 13668856]
    assert data.peers.data is not None
    assert len(data.peers.data.decks) == 3
    assert "ignoring unreadable cached Archidekt deck" in caplog.text
    assert json.loads(broken.read_text(encoding="utf-8"))["version"] == 2
    reads = len(rig.archidekt.deck_reads)
    rig.gather(refresh=True)
    assert len(rig.archidekt.deck_reads) == reads


def test_a_deck_cached_in_another_shape_is_a_miss_not_a_crash(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.gather()
    path = rig.cache / CACHE_FOLDER / DECK_READS_FOLDER / "7432856.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["answer"]["cards"] = "Mind Stone"
    path.write_text(json.dumps(raw), encoding="utf-8")
    raw["version"] = 0
    other = path.with_name("5553433.json")
    other.write_text(json.dumps(raw), encoding="utf-8")
    with caplog.at_level("WARNING"):
        data = rig.gather(refresh=True)
    assert sorted(rig.archidekt.deck_reads[3:]) == [5553433, 7432856]
    assert data.peers.data is not None
    assert len(data.peers.data.decks) == 3
    assert "ignoring malformed cached Archidekt deck" in caplog.text


def test_after_a_rate_limit_the_next_run_reads_only_the_decks_still_missing(rig: Rig) -> None:
    rig.archidekt.limit_reads = 2
    stopped = rig.gather()
    # Two 429s in a row end the run; the minute was waited between them.
    assert stopped.peers.source is AnswerSource.UNAVAILABLE
    assert stopped.peers.error is not None
    assert "HTTP 429" in stopped.peers.error
    assert rig.sleeps == [pytest.approx(60.0, abs=1.0)]
    assert [p.name for p in deck_files(rig)] == ["13668856.json", "5553433.json"]
    rig.archidekt.limit_reads = None
    rig.now = NOW + timedelta(minutes=10)
    resumed = rig.gather()
    assert rig.archidekt.deck_reads == [5553433, 13668856, 7432856]
    assert resumed.peers.source is AnswerSource.FETCHED


def test_after_archidekt_gives_up_on_a_rate_limit_later_gathers_of_the_run_send_and_wait_for_nothing(
    rig: Rig,
) -> None:
    rig.archidekt.limit_reads = 0
    sources = rig.sources()
    first = gather(ellivere_deck(), sources, rig.cache, now=lambda: rig.now, refresh=False)
    assert first.peers.source is AnswerSource.UNAVAILABLE
    sent, slept = len(rig.archidekt.requests), list(rig.sleeps)

    second = gather(ellivere_deck(), sources, rig.cache, now=lambda: rig.now, refresh=False)

    assert second.peers.source is AnswerSource.UNAVAILABLE
    assert (len(rig.archidekt.requests), rig.sleeps) == (sent, slept)


@pytest.mark.parametrize("status", [403, 404, 410])
def test_a_deck_that_went_private_or_was_deleted_is_skipped_not_fatal(rig: Rig, status: int) -> None:
    rig.archidekt.refused[13668856] = status
    peers = rig.gather().peers.data
    assert peers is not None
    assert [p.deck.id for p in peers.decks] == [5553433, 7432856]
    assert peers.skipped == 1


@pytest.mark.parametrize("status", [401, 500])
def test_any_other_refused_deck_read_still_fails_the_source(rig: Rig, status: int) -> None:
    rig.archidekt.refused[13668856] = status
    peers = rig.gather().peers
    assert peers.source is AnswerSource.UNAVAILABLE
    assert peers.data is None


def test_a_search_that_finds_no_decks_gives_an_empty_answer_not_an_error(rig: Rig) -> None:
    rig.archidekt.page = {"count": 0, "next": None, "results": []}
    peers = rig.gather().peers
    assert peers.source is AnswerSource.FETCHED
    assert peers.data == PeerLists((), 0)


def popular_page(count: int, *, first_id: int = 30_000_000, more: bool = False) -> Json:
    """A view-ordered search page of `count` decks, each owned by someone else, from 9000 views down by one
    and updated inside the window; with `more` it links to a next page."""
    row = load_fixture("archidekt_search_ellivere.json")["results"][0]
    results = [
        {**row, "id": first_id + n, "owner": {"id": 1, "username": "someone"}, "viewCount": 9000 - n}
        for n in range(count)
    ]
    link = "http://archidekt.com/api/decks/v3/?commanderName=x&formats=3&orderBy=-viewCount&page=2"
    return {"count": 500, "next": link if more else None, "results": results}


def test_the_commander_search_asks_for_the_most_viewed_decks(rig: Rig) -> None:
    bracketed(rig, 4)
    assert [(q["orderBy"], q["edhBracket"]) for q in rig.archidekt.searches] == [
        ("-viewCount", "4"),
        ("-viewCount", "5"),
    ]


def test_seventy_matching_decks_give_the_sixty_most_viewed_to_read(rig: Rig) -> None:
    rig.settings = replace(SETTINGS, peer_decks=60)
    rig.archidekt.page = popular_page(70)
    peers = rig.gather().peers.data
    assert peers is not None
    assert [p.deck.id for p in peers.decks] == list(range(30_000_000, 30_000_060))
    assert len(rig.archidekt.deck_reads) == 60


def test_each_bracket_of_a_commander_is_searched_to_the_fifth_page_at_most(rig: Rig) -> None:
    rig.archidekt.page = popular_page(10, more=True)
    bracketed(rig, 3)
    # Every page links to another over the floor: five requests a bracket, 3 and then 4.
    assert [q.get("page") for q in rig.archidekt.searches] == [None, "2", "2", "2", "2"] * 2


def test_a_tag_search_pages_each_bracket_to_the_second_page_by_views(rig: Rig) -> None:
    rig.settings = replace(SETTINGS, peer_decks=12)
    rig.archidekt.page = popular_page(10, more=True)
    gather_adjacent(
        replace(ellivere_deck(), bracket=3),
        [],
        rig.sources(),
        rig.cache,
        now=lambda: rig.now,
        refresh=False,
        tags=("Landfall",),
    )
    searches = rig.archidekt.searches
    assert {q["orderBy"] for q in searches} == {"-viewCount"}
    assert [q.get("page") for q in searches] == [None, "2"] * 2
    firsts = [q for q in searches if "page" not in q]
    assert [(q["deckTagName"], q["edhBracket"]) for q in firsts] == [("Landfall", "3"), ("Landfall", "4")]


def test_an_alike_commanders_search_pages_to_the_third_page_and_reads_a_quarter_of_peer_decks(
    rig: Rig,
) -> None:
    rig.settings = replace(SETTINGS, peer_decks=60)
    rig.archidekt.page = popular_page(70, more=True)
    found = gather_adjacent(
        replace(ellivere_deck(), bracket=3),
        [(ELLIVERE,)],
        rig.sources(),
        rig.cache,
        now=lambda: rig.now,
        refresh=False,
    )
    assert [q.get("page") for q in rig.archidekt.searches] == [None, "2", "2"] * 2
    assert len(rig.archidekt.deck_reads) == 15
    assert [p.deck.id for p in found.decks] == list(range(30_000_000, 30_000_015))


def test_a_pair_of_alike_commanders_reads_no_more_than_a_single_one(rig: Rig) -> None:
    rig.settings = replace(SETTINGS, peer_decks=60)
    rig.archidekt.page = popular_page(70)
    rig.archidekt.partnered = set(range(30_000_000, 30_000_070))
    found = adjacent(rig, (ELLIVERE, KODAMA))
    assert len(rig.archidekt.deck_reads) == 15
    assert len(found.decks) == 15


def test_a_commanders_own_search_keeps_its_five_pages_and_sixty_reads(rig: Rig) -> None:
    rig.settings = replace(SETTINGS, peer_decks=60)
    rig.archidekt.page = popular_page(70, more=True)
    rig.gather()
    assert [q.get("page") for q in rig.archidekt.searches] == [None, "2", "2", "2", "2"]
    assert len(rig.archidekt.deck_reads) == 60


def test_an_alike_search_uses_the_commanders_own_stored_answer_cut_to_its_reads(rig: Rig) -> None:
    rig.settings = replace(SETTINGS, peer_decks=60)
    rig.archidekt.page = popular_page(70)
    rig.gather()
    asked = len(rig.archidekt.requests)
    found = adjacent(rig, (ELLIVERE,))
    assert len(rig.archidekt.requests) == asked
    assert [p.deck.id for p in found.decks] == list(range(30_000_000, 30_000_015))


def test_an_alike_searchs_smaller_answer_does_not_stand_in_for_the_commanders_own(rig: Rig) -> None:
    rig.settings = replace(SETTINGS, peer_decks=60)
    rig.archidekt.page = popular_page(70)
    adjacent(rig, (ELLIVERE,))
    searched = len(rig.archidekt.searches)
    own = rig.gather().peers.data
    assert len(rig.archidekt.searches) > searched
    assert own is not None
    assert len(own.decks) == 60
    # Its own answer now serves the alike search again, with no new search.
    searched = len(rig.archidekt.searches)
    assert len(adjacent(rig, (ELLIVERE,)).decks) == 15
    assert len(rig.archidekt.searches) == searched


# The fingerprint of a tag answer as stored while a tag search paged 3 pages a bracket and read 15 decks.
LARGER_TAG_REQUEST = {
    "tag": "landfall",
    "brackets": [5],
    "pages": 3,
    "decks": 15,
    "viewFloors": [1000, 750, 500],
    "minDecks": 10,
    "windowDays": 60,
    "user": "tester",
}


def test_a_tag_answer_stored_under_the_larger_limits_stays_fresh_and_is_cut_to_the_new_reads(
    rig: Rig,
) -> None:
    rig.settings = replace(SETTINGS, peer_decks=60)
    rig.archidekt.commander = MAGDA
    rig.archidekt.page = popular_page(70)
    tagged(rig, "Landfall")
    [path] = (rig.cache / CACHE_FOLDER).glob("archidekt-tag-landfall*.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    template = raw["answer"]["decks"][0]
    # Fifteen decks, stored the least viewed first.
    raw["answer"]["decks"] = [
        {**template, "id": 30_000_000 + n, "views": 9000 - n} for n in reversed(range(15))
    ]
    raw["key"] = request_key(LARGER_TAG_REQUEST)
    path.write_text(json.dumps(raw), encoding="utf-8")
    asked = len(rig.archidekt.requests)
    again = tagged(rig, "Landfall")
    assert len(rig.archidekt.requests) == asked
    assert [p.deck.id for p in again.tagged[0].decks] == list(range(30_000_000, 30_000_010))


def old_answer_with_search(rig: Rig, search: object) -> None:
    """Make the stored answer the one the version before the view-ordered search wrote: under the request key
    of that version (written out here, with its page cap of 20) and holding a `search` field."""
    [path] = (rig.cache / CACHE_FOLDER).glob("archidekt-ellivere*.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["answer"]["search"] = search
    raw["key"] = request_key(
        {
            "commander": ELLIVERE,
            "brackets": [5],
            "pages": 20,
            "decks": 3,
            "viewFloors": [1000, 750, 500],
            "minDecks": 10,
            "windowDays": 60,
            "user": "tester",
        }
    )
    path.write_text(json.dumps(raw), encoding="utf-8")


OLD_SEARCH = {
    "fullAt": "2026-10-14T00:00:00+00:00",
    "decks": [
        {
            "id": 5553433,
            "name": "Deck",
            "owner": "player7",
            "views": 2131,
            "updatedAt": "2026-09-08T19:23:58+00:00",
            "bracket": 5,
        }
    ],
}


@pytest.mark.parametrize("search", [OLD_SEARCH, "not what an old answer held"])
def test_an_answer_stored_with_the_old_search_hits_still_serves_and_is_not_refetched(
    rig: Rig, search: object
) -> None:
    first = rig.gather().peers
    old_answer_with_search(rig, search)
    rig.now = NOW + timedelta(hours=23)
    asked = len(rig.archidekt.requests)
    again = rig.gather().peers
    assert again.source is AnswerSource.CACHED
    assert again.data == first.data
    assert len(rig.archidekt.requests) == asked
    offline = gather(
        ellivere_deck(), rig.sources(), rig.cache, now=lambda: rig.now, refresh=False, offline=True
    )
    assert offline.peers.data == first.data


def test_a_cache_from_before_the_view_bars_reads_as_not_fetched(rig: Rig) -> None:
    rig.gather()
    path = min((rig.cache / CACHE_FOLDER).glob("archidekt-*"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["key"] = request_key(
        {"commander": ELLIVERE, "bracket": 5, "decks": 3, "windowDays": 60, "user": "tester"}
    )
    path.write_text(json.dumps(raw), encoding="utf-8")
    old = gather(ellivere_deck(), rig.sources(), rig.cache, now=lambda: NOW, refresh=False, offline=True)
    assert old.peers.source is AnswerSource.UNAVAILABLE
    assert old.peers.error == "not fetched yet"


def test_a_second_run_the_same_day_reads_the_cache(rig: Rig) -> None:
    first = rig.gather()
    asked = (len(rig.topdeck.requests), len(rig.archidekt.requests))
    rig.now = NOW + timedelta(hours=23)
    second = rig.gather()
    assert (len(rig.topdeck.requests), len(rig.archidekt.requests)) == asked
    assert {second.lists.source, second.field.source, second.peers.source} == {AnswerSource.CACHED}
    assert (second.lists.data, second.field.data, second.peers.data) == (
        first.lists.data,
        first.field.data,
        first.peers.data,
    )
    assert min(p.name for p in (rig.cache / CACHE_FOLDER).iterdir()).startswith(
        "archidekt-ellivere-of-the-wild-court-5-"
    )


def test_peer_caches_from_before_counts_and_categories_still_read(rig: Rig) -> None:
    rig.gather()
    path = min((rig.cache / CACHE_FOLDER).glob("archidekt-*"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    for deck in raw["answer"]["decks"]:
        del deck["multiples"], deck["planned"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    peers = rig.gather().peers.data
    assert peers is not None
    # Without counts the land count can't be read, which differs from a deck with no basic land twice.
    assert {(p.multiples, p.planned) for p in peers.decks} == {(None, ())}


def test_a_changed_window_does_not_read_the_cache_of_the_old_one(rig: Rig) -> None:
    rig.gather()
    rig.settings = replace(SETTINGS, window_days=30)
    asked = (len(rig.topdeck.requests), len(rig.archidekt.requests))
    data = rig.gather()
    assert (len(rig.topdeck.requests), len(rig.archidekt.requests)) > asked
    assert {data.lists.source, data.field.source, data.peers.source} == {AnswerSource.FETCHED}
    assert data.since == date(2026, 9, 15)


def test_refresh_works_the_answers_out_again_but_asks_tournaments_only_for_unseen_days(rig: Rig) -> None:
    rig.gather()
    asked = (len(rig.topdeck.requests), len(rig.archidekt.requests))
    again = rig.gather(refresh=True)
    assert again.lists.source is AnswerSource.FETCHED
    assert len(rig.archidekt.requests) > asked[1]
    assert len(rig.topdeck.requests) == asked[0]


def test_a_failing_service_falls_back_to_the_last_good_answer(rig: Rig) -> None:
    rig.gather()
    rig.now = NOW + timedelta(days=2)
    rig.topdeck.status = 503
    rig.archidekt.status = 503
    data = rig.gather()
    assert data.lists.source is AnswerSource.STALE
    assert data.field.source is AnswerSource.STALE
    assert data.peers.source is AnswerSource.STALE
    assert data.lists.error is not None
    assert "HTTP 503" in data.lists.error


def test_without_a_cached_answer_a_failure_is_unavailable(rig: Rig) -> None:
    rig.archidekt.status = 500
    data = rig.gather()
    assert data.peers.source is AnswerSource.UNAVAILABLE
    assert data.peers.data is None
    assert data.lists.source is AnswerSource.FETCHED


def test_the_next_day_without_a_key_the_stored_events_answer_as_a_stale_copy(rig: Rig) -> None:
    first = rig.gather()
    assert first.lists.data is not None
    # No cached answer to fall back on, only the stored events.
    shutil.rmtree(rig.cache / CACHE_FOLDER)
    rig.now = NOW + timedelta(days=1)
    rig.keys.key = None
    rig.topdeck.requests.clear()
    data = rig.gather()
    assert data.lists.source is AnswerSource.STALE
    # The August event left the window with the day; the two later ones stand.
    assert data.lists.data is not None
    assert [e.standing for e in data.lists.data.entries] == [12, 5]
    assert data.lists.error == NO_KEY_MESSAGE
    assert data.lists.key_problem
    assert data.field.source is AnswerSource.STALE
    assert data.field.data is not None
    assert ELLIVERE in [c.stats.name for c in data.field.data]
    assert rig.topdeck.requests == []
    # A stale copy is not cached as a fresh one: the next run tries TopDeck again.
    assert not list((rig.cache / CACHE_FOLDER).glob("tournament*.json"))


def test_an_unreadable_deck_is_skipped_and_counted(rig: Rig) -> None:
    rig.archidekt.broken = {13668856}
    peers = rig.gather().peers.data
    assert peers is not None
    assert [p.deck.id for p in peers.decks] == [5553433, 7432856]
    assert peers.skipped == 1


def test_a_commander_no_event_had_is_marked_unknown(rig: Rig) -> None:
    deck = ellivere_deck()
    other = DeckCard("Nobody, the Missing", 1, ("Commander",), True, "x", 9, 9, "tst", "1", "Normal")  # noqa: FBT003 — positional like the model
    lists = rig.gather(deck=Deck(deck.id, deck.name, deck.owner, 5, deck.updated_at, (other,))).lists.data
    assert lists is not None
    assert not lists.known
    assert lists.entries == ()


def test_zero_peer_decks_and_meta_commanders_ask_nothing(rig: Rig) -> None:
    sources = rig.sources()
    quiet = MetaSources(
        sources.tournaments,
        sources.archidekt,
        sources.spellbook,
        sources.spellbook_pacer,
        sources.user,
        MetaSettings(peer_decks=0, meta_commanders=0),
    )
    data = gather(ellivere_deck(), quiet, rig.cache, now=lambda: NOW, refresh=False)
    assert rig.archidekt.requests == []
    assert data.field.data == ()
    assert len(rig.topdeck.requests) == WINDOW_REQUESTS


def test_offline_reads_only_the_cache_whatever_its_age(rig: Rig) -> None:
    empty = gather(ellivere_deck(), rig.sources(), rig.cache, now=lambda: NOW, refresh=False, offline=True)
    assert empty.lists.source is AnswerSource.UNAVAILABLE
    assert rig.topdeck.requests == []
    assert rig.archidekt.requests == []
    rig.gather()
    asked = len(rig.topdeck.requests)
    later = NOW + timedelta(days=3)
    old = gather(ellivere_deck(), rig.sources(), rig.cache, now=lambda: later, refresh=False, offline=True)
    assert old.lists.source is AnswerSource.STALE
    assert old.lists.data is not None
    assert len(rig.topdeck.requests) == asked


def test_combos_are_looked_for_in_each_top_commanders_core_cards(rig: Rig) -> None:
    data = rig.gather()
    combos = data.combos.data
    assert combos is not None
    # The fake Spellbook gives the same three combos for any request.
    ids = ["513-5034--46", "129-4131", "4222-5168"]
    assert [(c.commander, [d.id for d in c.combos]) for c in combos] == [
        (ELLIVERE, ids),
        ("Kinnan, Bonder Prodigy", ids),
    ]
    # Two cores and the deck's own list, which is read beside them and in no set order.
    assert len(rig.spellbook.combo_bodies) == 3
    [kinnan] = [b for b in rig.spellbook.combo_bodies if b["commanders"][0]["card"] != ELLIVERE]
    [ellivere] = [
        b for b in rig.spellbook.combo_bodies if {"card": "Gaea's Cradle", "quantity": 1} in b["main"]
    ]
    [own] = [
        b for b in rig.spellbook.combo_bodies if {"card": "Containment Priest", "quantity": 1} in b["main"]
    ]
    assert kinnan["commanders"] == [{"card": "Kinnan, Bonder Prodigy", "quantity": 1}]
    assert ellivere["commanders"] == [{"card": ELLIVERE, "quantity": 1}]
    assert own["commanders"] == [{"card": ELLIVERE, "quantity": 1}]
    assert data.own_combos.source is AnswerSource.FETCHED
    # Every card of the two Kinnan lists is in at least a fifth of them, so all are core; so is every
    # card of the three Ellivere lists, which is in a third.
    assert {"card": "Basalt Monolith", "quantity": 1} in kinnan["main"]
    assert {"card": "Circle of Dreams Druid", "quantity": 1} in kinnan["main"]
    assert {"card": "Gaea's Cradle", "quantity": 1} in ellivere["main"]
    again = rig.gather()
    assert again.combos.source is AnswerSource.CACHED
    assert again.own_combos.source is AnswerSource.CACHED
    assert len(rig.spellbook.combo_bodies) == 3


def test_combos_without_meta_lists_are_unavailable(rig: Rig) -> None:
    rig.topdeck.status = 403
    data = rig.gather()
    assert data.combos.source is AnswerSource.UNAVAILABLE
    # The deck's own combos need no meta lists.
    [own] = rig.spellbook.combo_bodies
    assert own["commanders"] == [{"card": "Ellivere of the Wild Court", "quantity": 1}]


def test_the_cache_stamp_without_a_cache_folder(tmp_path: Path) -> None:
    assert cache_stamp(tmp_path) == "-"


def test_the_cache_stamp_changes_when_an_answer_is_saved_rewritten_or_removed(tmp_path: Path) -> None:
    folder = tmp_path / CACHE_FOLDER
    folder.mkdir()
    first, second = folder / "a.json", folder / "b.json"
    first.write_text("{}", encoding="utf-8")
    os.utime(first, ns=(1_000, 1_000))
    assert cache_stamp(tmp_path) == "1:1000"
    second.write_text("{}", encoding="utf-8")
    os.utime(second, ns=(2_000, 2_000))
    assert cache_stamp(tmp_path) == "2:2000"
    os.utime(first, ns=(3_000, 3_000))
    assert cache_stamp(tmp_path) == "2:3000"
    first.unlink()
    assert cache_stamp(tmp_path) == "1:2000"


def adjacent(rig: Rig, *groups: tuple[str, ...]) -> AdjacentDecks:
    return gather_adjacent(
        ellivere_deck(), groups, rig.sources(), rig.cache, now=lambda: rig.now, refresh=False
    )


def test_a_partner_pair_keeps_only_decks_led_by_both(rig: Rig) -> None:
    # The 3rd and 5th most viewed of the five decks over 500 views pair Magda with Kodama; the 27-view deck
    # that also does is under the bar and never read.
    rig.archidekt.commander = MAGDA
    rig.archidekt.partnered = {7432856, 23074747, 26605393}
    rig.settings = replace(SETTINGS, peer_decks=20)
    found = adjacent(rig, (MAGDA, KODAMA))
    assert [p.deck.id for p in found.decks] == [7432856, 23074747]
    assert 26605393 not in rig.archidekt.deck_reads
    assert found.errors == ()


def test_a_lone_alike_commander_keeps_decks_that_pair_it_with_a_partner(rig: Rig) -> None:
    # The 3rd most viewed pairs Magda with Kodama: it still leads with Magda, so it is a Magda deck.
    rig.archidekt.commander = MAGDA
    rig.archidekt.partnered = {7432856}
    rig.settings = replace(SETTINGS, peer_decks=12)
    found = adjacent(rig, (MAGDA,))
    assert [p.deck.id for p in found.decks] == [5553433, 13668856, 7432856]


def test_a_lone_alike_commander_still_leaves_out_decks_led_by_another_commander(rig: Rig) -> None:
    rig.archidekt.commander = MAGDA
    rig.archidekt.led_by = {13668856: KODAMA}
    rig.settings = replace(SETTINGS, peer_decks=12)
    found = adjacent(rig, (MAGDA,))
    assert [p.deck.id for p in found.decks] == [5553433, 7432856]


def test_a_commanders_own_search_still_leaves_out_decks_that_pair_it_with_a_partner(rig: Rig) -> None:
    rig.archidekt.partnered = {7432856}
    peers = rig.gather().peers.data
    assert peers is not None
    assert [p.deck.id for p in peers.decks] == [5553433, 13668856]


def test_adjacent_groups_pool_their_decks_once_each(rig: Rig) -> None:
    # The fake search answers every commander with the same decks, so the second group adds none.
    rig.settings = replace(SETTINGS, peer_decks=12)
    found = adjacent(rig, (ELLIVERE,), (ELLIVERE,))
    assert [p.deck.id for p in found.decks] == [5553433, 13668856, 7432856]


GROUP_NAMES = tuple(f"Fictional Commander {n:02}" for n in range(1, 13))


def searched_groups(rig: Rig, since: int) -> list[str]:
    return [q["commanderName"] for q in rig.archidekt.searches[since:]]


def oldest_group_answer(rig: Rig) -> timedelta:
    stamps = [
        datetime.fromisoformat(json.loads(p.read_text(encoding="utf-8"))["fetchedAt"])
        for p in (rig.cache / CACHE_FOLDER).glob("archidekt-fictional-*")
    ]
    return rig.now - min(stamps)


def test_each_group_is_refreshed_once_in_three_days_and_none_is_left_older(rig: Rig) -> None:
    adjacent(rig, *((name,) for name in GROUP_NAMES))
    for first_day in (1, 4):
        refreshed: dict[int, list[str]] = {}
        for day in range(first_day, first_day + 3):
            rig.now = NOW + timedelta(days=day)
            before = len(rig.archidekt.searches)
            adjacent(rig, *((name,) for name in GROUP_NAMES))
            refreshed[day] = searched_groups(rig, before)
            assert oldest_group_answer(rig) <= timedelta(days=3)
        every = sorted(name for names in refreshed.values() for name in names)
        assert every == sorted(GROUP_NAMES)
        # About a third of the groups each day, whichever day of the cycle the first one falls on.
        assert all(1 <= len(names) <= 7 for names in refreshed.values())


def tagged(
    rig: Rig, *tags: str, groups: tuple[tuple[str, ...], ...] = (), offline: bool = False
) -> AdjacentDecks:
    return gather_adjacent(
        ellivere_deck(),
        groups,
        rig.sources(),
        rig.cache,
        now=lambda: rig.now,
        refresh=False,
        offline=offline,
        tags=tags,
    )


def tag_searches(rig: Rig) -> list[dict[str, str]]:
    return [q for q in rig.archidekt.searches if "deckTagName" in q]


def test_a_theme_tag_searches_recent_decks_of_any_commander_at_the_decks_brackets(rig: Rig) -> None:
    # A quarter of peer_decks (12) is read per tag: the 3 most viewed.
    rig.settings = replace(SETTINGS, peer_decks=12)
    rig.archidekt.commander = MAGDA
    found = tagged(rig, "Landfall")
    assert [(q["deckTagName"], q["edhBracket"], "commanderName" in q) for q in tag_searches(rig)] == [
        ("Landfall", "5", False)
    ]
    assert [(t.tag, [p.deck.id for p in t.decks]) for t in found.tagged] == [
        ("Landfall", [5553433, 13668856, 7432856])
    ]
    assert found.decks == ()
    assert found.errors == ()


def test_a_tag_search_at_bracket_3_reads_brackets_3_and_4_with_a_page_budget_of_its_own(rig: Rig) -> None:
    rig.settings = replace(SETTINGS, peer_decks=12)
    rig.archidekt.commander = MAGDA
    gather_adjacent(
        replace(ellivere_deck(), bracket=3),
        [],
        rig.sources(),
        rig.cache,
        now=lambda: rig.now,
        refresh=False,
        tags=("Landfall",),
    )
    assert [q["edhBracket"] for q in tag_searches(rig)] == ["3", "4"]


@pytest.mark.parametrize(("peer_decks", "reads"), [(3, 1), (4, 1), (5, 2), (12, 3)])
def test_a_tag_search_reads_a_quarter_of_the_commander_reads_rounded_up(
    rig: Rig, peer_decks: int, reads: int
) -> None:
    rig.settings = replace(SETTINGS, peer_decks=peer_decks)
    rig.archidekt.commander = MAGDA
    tagged(rig, "Landfall")
    assert len(rig.archidekt.deck_reads) == reads


@pytest.mark.parametrize(("peer_decks", "reads"), [(40, 10), (44, 10), (60, 10), (20, 5)])
def test_a_tag_search_reads_ten_decks_at_most_whatever_the_commander_reads(
    rig: Rig, peer_decks: int, reads: int
) -> None:
    rig.settings = replace(SETTINGS, peer_decks=peer_decks)
    rig.archidekt.commander = MAGDA
    rig.archidekt.page = popular_page(70)
    tagged(rig, "Landfall")
    assert len(rig.archidekt.deck_reads) == reads


def test_decks_of_the_decks_own_commander_are_left_out_of_a_tag_search(rig: Rig) -> None:
    rig.settings = replace(SETTINGS, peer_decks=12)
    rig.archidekt.commander = MAGDA
    rig.archidekt.led_by = {13668856: ELLIVERE}
    rig.archidekt.partnered = {7432856}
    found = tagged(rig, "Landfall")
    # Partnered with Kodama, Magda's deck stays: only Ellivere's own commander is the deck's.
    assert [p.deck.id for p in found.tagged[0].decks] == [5553433, 7432856]


def test_a_partner_decks_tag_search_leaves_out_decks_with_either_partner(rig: Rig) -> None:
    rig.settings = replace(SETTINGS, peer_decks=12)
    # Ellivere decks, one of them with Kodama as partner, and one led by Kodama alone.
    rig.archidekt.partnered = {5553433}
    rig.archidekt.led_by = {7432856: KODAMA}
    found = gather_adjacent(
        ellivere_deck(MAGDA, KODAMA),
        [],
        rig.sources(),
        rig.cache,
        now=lambda: rig.now,
        refresh=False,
        tags=("Landfall",),
    )
    assert [p.deck.id for p in found.tagged[0].decks] == [13668856]


def test_without_theme_tags_no_tag_search_is_made(rig: Rig) -> None:
    tagged(rig, groups=((MAGDA,),))
    assert rig.archidekt.searches
    assert tag_searches(rig) == []
    assert tagged(rig).tagged == ()


def test_each_tag_is_searched_once_whatever_its_case(rig: Rig) -> None:
    found = tagged(rig, "Landfall", "landfall", "Tokens")
    assert [q["deckTagName"] for q in tag_searches(rig)] == ["Landfall", "Tokens"]
    assert [t.tag for t in found.tagged] == ["Landfall", "Tokens"]


def test_tag_decks_sit_beside_the_commander_groups_decks(rig: Rig) -> None:
    rig.settings = replace(SETTINGS, peer_decks=12)
    rig.archidekt.commander = MAGDA
    found = tagged(rig, "Landfall", groups=((MAGDA,),))
    assert [p.deck.id for p in found.decks] == [5553433, 13668856, 7432856]
    assert [t.tag for t in found.tagged] == ["Landfall"]


def test_a_deck_read_for_a_commander_search_is_not_read_again_for_a_tag(rig: Rig) -> None:
    rig.settings = replace(SETTINGS, peer_decks=12)
    rig.archidekt.commander = MAGDA
    tagged(rig, "Landfall", groups=((MAGDA,),))
    assert sorted(rig.archidekt.deck_reads) == [5553433, 7432856, 13668856]


def test_a_tag_answer_is_cached_for_the_day_and_shared_by_decks_of_other_commanders(rig: Rig) -> None:
    rig.settings = replace(SETTINGS, peer_decks=12)
    rig.archidekt.commander = MAGDA
    tagged(rig, "Landfall")
    searches = len(rig.archidekt.searches)
    again = tagged(rig, "Landfall")
    assert len(rig.archidekt.searches) == searches
    assert len(again.tagged[0].decks) == 3
    # A deck of another commander asks the same question, so the same cached answer serves it.
    other = gather_adjacent(
        ellivere_deck("Kinnan, Bonder Prodigy"),
        [],
        rig.sources(),
        rig.cache,
        now=lambda: rig.now,
        refresh=False,
        tags=("landfall",),
    )
    assert len(rig.archidekt.searches) == searches
    assert len(other.tagged[0].decks) == 3
    # The cached decks still say who leads them, so Magda's own deck finds none of them.
    magda = gather_adjacent(
        ellivere_deck(MAGDA),
        [],
        rig.sources(),
        rig.cache,
        now=lambda: rig.now,
        refresh=False,
        tags=("landfall",),
    )
    assert magda.tagged[0].decks == ()


def test_a_tag_answer_is_refreshed_once_in_three_days(rig: Rig) -> None:
    tagged(rig, "Landfall")
    counts: list[int] = []
    for day in range(1, 7):
        rig.now = NOW + timedelta(days=day)
        before = len(tag_searches(rig))
        tagged(rig, "Landfall")
        counts.append(len(tag_searches(rig)) - before)
    assert sum(counts[:3]) == 1
    assert sum(counts[3:]) == 1


def test_offline_a_tag_search_reads_only_the_cache(rig: Rig) -> None:
    rig.settings = replace(SETTINGS, peer_decks=12)
    rig.archidekt.commander = MAGDA
    empty = tagged(rig, "Landfall", offline=True)
    assert rig.archidekt.requests == []
    assert empty.tagged == ()
    [(name, why)] = empty.errors
    assert (name, why) == ("Landfall-tagged", "not fetched yet")
    tagged(rig, "Landfall")
    asked = len(rig.archidekt.requests)
    rig.now = NOW + timedelta(days=3)
    old = tagged(rig, "Landfall", offline=True)
    assert len(rig.archidekt.requests) == asked
    assert [p.deck.id for p in old.tagged[0].decks] == [5553433, 13668856, 7432856]


def test_a_tag_whose_search_fails_is_named_and_the_rest_carry_on(rig: Rig) -> None:
    rig.archidekt.status = 500
    found = tagged(rig, "Landfall")
    assert found.tagged == ()
    [(name, why)] = found.errors
    assert name == "Landfall-tagged"
    assert "HTTP 500" in why


def test_an_adjacent_group_that_cannot_be_read_is_named(rig: Rig) -> None:
    rig.archidekt.status = 500
    found = adjacent(rig, (MAGDA,))
    assert found.decks == ()
    [(name, why)] = found.errors
    assert name == MAGDA
    assert "HTTP 500" in why


def test_a_future_dated_and_a_malformed_event_leave_the_commander_lists_readable(rig: Rig) -> None:
    deck = (2, [ELLIVERE], ["Sol Ring"])
    future = topdeck_event("future-test-event", "2030-10-26T10:00:00+00:00", 64, 8, [deck])
    rig.topdeck.extra = [future, {"TID": "broken-test-event", "standings": "none"}]

    lists = rig.gather().lists.data

    assert lists is not None
    assert [e.tournament.held_on for e in lists.entries] == [
        date(2026, 10, 4),
        date(2026, 9, 13),
        date(2026, 8, 16),
    ]


REFUSAL = "TopDeck refused the key (HTTP 401); check it in the TopDeck panel"


def test_a_cache_only_read_after_topdeck_refused_the_key_says_why_not_not_fetched_yet(rig: Rig) -> None:
    rig.keys.put("a-wrong-key")
    fetched = rig.gather()
    assert (fetched.lists.source, fetched.lists.error) == (AnswerSource.UNAVAILABLE, REFUSAL)
    assert fetched.field.error == REFUSAL
    assert fetched.lists.key_problem
    assert fetched.field.key_problem
    assert not fetched.peers.key_problem
    cache_only = gather(
        ellivere_deck(), rig.sources(), rig.cache, now=lambda: rig.now, refresh=False, offline=True
    )
    assert cache_only.lists.error == REFUSAL
    assert cache_only.field.error == REFUSAL
    # Archidekt answered, so its cached decks are read as before.
    assert cache_only.peers.source is AnswerSource.CACHED
