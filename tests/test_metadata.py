from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from commander.archidekt import ArchidektSearch
from commander.config import MetaSettings
from commander.deckcache import AnswerSource
from commander.edhtop16 import Edhtop16Client
from commander.metadata import CACHE_FOLDER, MetaData, MetaSources, gather, window_start
from commander.models import Deck, DeckCard
from commander.net import HTTP_OK, Fetcher, Pacer
from conftest import DECK_ID, FakeEdhtop16, load_fixture

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
ELLIVERE = "Ellivere of the Wild Court"
SETTINGS = MetaSettings(window_days=60, min_event_size=16, peer_decks=3, meta_commanders=2, meta_lists=30)


class FakeArchidektSearch:
    """The trimmed real Ellivere search page, and the Magda fixture for any deck read except `broken` ones."""

    def __init__(self) -> None:
        self.broken: set[int] = set()
        self.status = 200
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        if self.status != HTTP_OK:
            return httpx.Response(self.status)
        if request.url.path == "/api/decks/v3/":
            return httpx.Response(200, json=load_fixture("archidekt_search_ellivere.json"))
        deck_id = int(request.url.path.strip("/").split("/")[-1])
        if deck_id in self.broken:
            return httpx.Response(200, json={"id": deck_id})
        return httpx.Response(200, json=load_fixture(f"deck_{DECK_ID}.json"))


def ellivere_deck() -> Deck:
    def card(entry_id: int, name: str, categories: tuple[str, ...]) -> DeckCard:
        return DeckCard(name, 1, categories, True, f"id-{entry_id}", entry_id, entry_id, "tst", "1", "Normal")  # noqa: FBT003 — positional like the model

    return Deck(
        10078393,
        "Let's not play together",
        "MAKKIS",
        5,
        "2026-10-15T20:22:13Z",
        (card(1, ELLIVERE, ("Commander",)), card(2, "Containment Priest", ("Stax",))),
    )


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.edhtop16 = FakeEdhtop16()
        self.archidekt = FakeArchidektSearch()
        self.cache = tmp_path / "cache"
        self.now = NOW

    def sources(self) -> MetaSources:
        return MetaSources(
            Edhtop16Client(Fetcher(self.edhtop16.client(), max_attempts=1), Pacer(0)),
            ArchidektSearch(
                Fetcher(httpx.Client(transport=httpx.MockTransport(self.archidekt.handler)), max_attempts=1),
                Pacer(0),
            ),
            "makkis",
            SETTINGS,
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
        date(2026, 10, 10),
        date(2026, 9, 20),
        date(2026, 8, 16),
    ]
    field = data.field.data
    assert field is not None
    assert [(c.stats.name, len(c.entries)) for c in field] == [
        ("Kinnan, Bonder Prodigy", 2),
        # EDHTop16 has no entries query answer for this pair in the fake, so it counts as unknown and empty.
        ("Kraum, Ludevic's Opus / Tymna the Weaver", 0),
    ]
    peers = data.peers.data
    assert peers is not None
    # The three most viewed, the user's own deck left out.
    assert [(p.deck.id, p.deck.views) for p in peers.decks] == [
        (5553433, 2131),
        (13668856, 1847),
        (7432856, 1782),
    ]
    assert "Magda, Brazen Outlaw" in peers.decks[0].cards
    assert peers.skipped == 0
    assert {data.lists.source, data.field.source, data.peers.source} == {AnswerSource.FETCHED}


def test_a_second_run_the_same_day_reads_the_cache(rig: Rig) -> None:
    first = rig.gather()
    asked = (len(rig.edhtop16.requests), len(rig.archidekt.requests))
    rig.now = NOW + timedelta(hours=23)
    second = rig.gather()
    assert (len(rig.edhtop16.requests), len(rig.archidekt.requests)) == asked
    assert {second.lists.source, second.field.source, second.peers.source} == {AnswerSource.CACHED}
    assert (second.lists.data, second.field.data, second.peers.data) == (
        first.lists.data,
        first.field.data,
        first.peers.data,
    )
    assert min(p.name for p in (rig.cache / CACHE_FOLDER).iterdir()).startswith(
        "archidekt-ellivere-of-the-wild-court-5-"
    )


def test_refresh_fetches_again(rig: Rig) -> None:
    rig.gather()
    asked = len(rig.edhtop16.requests)
    again = rig.gather(refresh=True)
    assert len(rig.edhtop16.requests) > asked
    assert again.lists.source is AnswerSource.FETCHED


def test_a_failing_service_falls_back_to_the_last_good_answer(rig: Rig) -> None:
    rig.gather()
    rig.now = NOW + timedelta(days=2)
    rig.edhtop16.status = 503
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


def test_an_unreadable_deck_is_skipped_and_counted(rig: Rig) -> None:
    rig.archidekt.broken = {13668856}
    peers = rig.gather().peers.data
    assert peers is not None
    assert [p.deck.id for p in peers.decks] == [5553433, 7432856]
    assert peers.skipped == 1


def test_a_commander_edhtop16_does_not_know_is_marked_unknown(rig: Rig) -> None:
    deck = ellivere_deck()
    other = DeckCard("Nobody, the Missing", 1, ("Commander",), True, "x", 9, 9, "tst", "1", "Normal")  # noqa: FBT003 — positional like the model
    lists = rig.gather(deck=Deck(deck.id, deck.name, deck.owner, 5, deck.updated_at, (other,))).lists.data
    assert lists is not None
    assert not lists.known
    assert lists.entries == ()


def test_zero_peer_decks_and_meta_commanders_ask_nothing(rig: Rig) -> None:
    sources = rig.sources()
    quiet = MetaSources(
        sources.edhtop16, sources.archidekt, sources.user, MetaSettings(peer_decks=0, meta_commanders=0)
    )
    data = gather(ellivere_deck(), quiet, rig.cache, now=lambda: NOW, refresh=False)
    assert rig.archidekt.requests == []
    assert data.field.data == ()
    assert len(rig.edhtop16.requests) == 1


def test_offline_reads_only_the_cache_whatever_its_age(rig: Rig) -> None:
    empty = gather(ellivere_deck(), rig.sources(), rig.cache, now=lambda: NOW, refresh=False, offline=True)
    assert empty.lists.source is AnswerSource.UNAVAILABLE
    assert rig.edhtop16.requests == []
    assert rig.archidekt.requests == []
    rig.gather()
    asked = len(rig.edhtop16.requests)
    later = NOW + timedelta(days=3)
    old = gather(ellivere_deck(), rig.sources(), rig.cache, now=lambda: later, refresh=False, offline=True)
    assert old.lists.source is AnswerSource.STALE
    assert old.lists.data is not None
    assert len(rig.edhtop16.requests) == asked
