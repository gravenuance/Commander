import json
import os
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from commander.archidekt import ArchidektSearch
from commander.config import MetaSettings
from commander.deckcache import AnswerSource, request_key
from commander.edhtop16 import Edhtop16Client
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
    window_start,
)
from commander.models import Deck, DeckCard
from commander.net import HTTP_OK, Fetcher, Pacer
from commander.spellbook import SpellbookClient
from conftest import DECK_ID, FakeEdhtop16, FakeSpellbook, Json, card_entry, load_fixture

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
ELLIVERE = "Ellivere of the Wild Court"
MAGDA = "Magda, Brazen Outlaw"
KODAMA = "Kodama of the East Tree"
SETTINGS = MetaSettings(window_days=60, min_event_size=16, peer_decks=3, meta_commanders=2, meta_lists=30)


class FakeArchidektSearch:
    """The trimmed real Ellivere search page, and the Magda fixture for any deck read except `broken` ones."""

    def __init__(self) -> None:
        self.broken: set[int] = set()
        # The commander every served deck is led by (the fixture deck's Magda renamed).
        self.commander = ELLIVERE
        # Decks served with Kodama of the East Tree as the commander's partner.
        self.partnered: set[int] = set()
        self.status = 200
        # The search answer; the trimmed real page when None.
        self.page: Json | None = None
        self.requests: list[str] = []
        # The query of each search request.
        self.searches: list[dict[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        if request.url.path == "/api/decks/v3/":
            self.searches.append(dict(request.url.params))
        if self.status != HTTP_OK:
            return httpx.Response(self.status)
        if request.url.path == "/api/decks/v3/":
            return httpx.Response(200, json=self.page or load_fixture("archidekt_search_ellivere.json"))
        deck_id = int(request.url.path.strip("/").split("/")[-1])
        if deck_id in self.broken:
            return httpx.Response(200, json={"id": deck_id})
        deck = load_fixture(f"deck_{DECK_ID}.json")
        for entry in deck["cards"]:
            if entry["card"]["oracleCard"]["name"] == MAGDA:
                entry["card"]["oracleCard"]["name"] = self.commander
        if deck_id in self.partnered:
            deck["cards"].append(card_entry(9_200, KODAMA, ["Commander"]))
        return httpx.Response(200, json=deck)


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
        self.spellbook = FakeSpellbook()
        self.spellbook.combos = load_fixture("spellbook_meta_kinnan.json")
        self.cache = tmp_path / "cache"
        self.now = NOW
        self.settings = SETTINGS

    def sources(self) -> MetaSources:
        return MetaSources(
            Edhtop16Client(Fetcher(self.edhtop16.client(), max_attempts=1), Pacer(0)),
            ArchidektSearch(
                Fetcher(httpx.Client(transport=httpx.MockTransport(self.archidekt.handler)), max_attempts=1),
                Pacer(0),
            ),
            SpellbookClient(Fetcher(self.spellbook.client(), max_attempts=1)),
            Pacer(0),
            "makkis",
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
    # Three decks have 1000 views and five 500: under ten each time, so all twelve are read.
    assert len(peers.decks) == 12
    assert [p.deck.views for p in peers.decks][:3] == [2131, 1847, 1782]
    assert min(p.deck.views for p in peers.decks) == 20


def bracketed(rig: Rig, bracket: int) -> MetaData:
    deck = replace(ellivere_deck(), bracket=bracket)
    return gather_for(deck, rig.sources(), rig.cache, now=lambda: rig.now, refresh=False)


def test_a_bracket_3_deck_reads_brackets_3_and_4_of_archidekt_and_no_tournament_lists(rig: Rig) -> None:
    data = bracketed(rig, 3)
    assert [q["edhBracket"] for q in rig.archidekt.searches] == ["3", "4"]
    assert rig.edhtop16.requests == []
    assert data.lists.source is AnswerSource.UNAVAILABLE
    assert data.peers.source is AnswerSource.FETCHED


def test_a_bracket_4_deck_reads_brackets_4_and_5_of_archidekt_and_the_commanders_tournament_lists(
    rig: Rig,
) -> None:
    data = bracketed(rig, 4)
    assert [q["edhBracket"] for q in rig.archidekt.searches] == ["4", "5"]
    # Only the commander's own lists: not the most-played commanders, nor Spellbook.
    assert len(rig.edhtop16.requests) == 1
    assert rig.spellbook.combo_bodies == []
    assert data.lists.source is AnswerSource.FETCHED
    assert data.lists.data is not None
    assert len(data.lists.data.entries) == 3
    assert data.peers.source is AnswerSource.FETCHED
    assert data.field.source is AnswerSource.UNAVAILABLE


def test_a_bracket_4_deck_offline_asks_nothing(rig: Rig) -> None:
    deck = replace(ellivere_deck(), bracket=4)
    data = gather_for(deck, rig.sources(), rig.cache, now=lambda: NOW, refresh=False, offline=True)
    assert (rig.edhtop16.requests, rig.archidekt.requests) == ([], [])
    assert (data.lists.source, data.peers.source) == (AnswerSource.UNAVAILABLE, AnswerSource.UNAVAILABLE)
    assert data.lists.error == "not fetched yet"


def test_a_bracket_5_deck_reads_only_bracket_5_of_archidekt(rig: Rig) -> None:
    bracketed(rig, 5)
    assert [q["edhBracket"] for q in rig.archidekt.searches] == ["5"]


def test_a_search_that_finds_no_decks_gives_an_empty_answer_not_an_error(rig: Rig) -> None:
    rig.archidekt.page = {"count": 0, "next": None, "results": []}
    peers = rig.gather().peers
    assert peers.source is AnswerSource.FETCHED
    assert peers.data == PeerLists((), 0)


def test_a_cache_from_before_the_view_bars_reads_as_not_fetched(rig: Rig) -> None:
    rig.gather()
    path = min((rig.cache / CACHE_FOLDER).glob("archidekt-*"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["key"] = request_key(
        {"commander": ELLIVERE, "bracket": 5, "decks": 3, "windowDays": 60, "user": "makkis"}
    )
    path.write_text(json.dumps(raw), encoding="utf-8")
    old = gather(ellivere_deck(), rig.sources(), rig.cache, now=lambda: NOW, refresh=False, offline=True)
    assert old.peers.source is AnswerSource.UNAVAILABLE
    assert old.peers.error == "not fetched yet"


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
    asked = (len(rig.edhtop16.requests), len(rig.archidekt.requests))
    data = rig.gather()
    assert (len(rig.edhtop16.requests), len(rig.archidekt.requests)) > asked
    assert {data.lists.source, data.field.source, data.peers.source} == {AnswerSource.FETCHED}
    assert data.since == date(2026, 9, 15)


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
        sources.edhtop16,
        sources.archidekt,
        sources.spellbook,
        sources.spellbook_pacer,
        sources.user,
        MetaSettings(peer_decks=0, meta_commanders=0),
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


def test_combos_are_looked_for_in_each_top_commanders_core_cards(rig: Rig) -> None:
    data = rig.gather()
    combos = data.combos.data
    assert combos is not None
    # Kraum and Tymna have no lists in the fake, so only Kinnan's core is asked about.
    assert [(c.commander, [d.id for d in c.combos]) for c in combos] == [
        ("Kinnan, Bonder Prodigy", ["513-5034--46", "129-4131", "4222-5168"])
    ]
    # The meta's cores first, then the deck's own list, whose combos the meta's answers are checked against.
    request, own = rig.spellbook.combo_bodies
    assert request["commanders"] == [{"card": "Kinnan, Bonder Prodigy", "quantity": 1}]
    assert own["commanders"] == [{"card": "Ellivere of the Wild Court", "quantity": 1}]
    assert data.own_combos.source is AnswerSource.FETCHED
    # Every card of the two lists is in at least half of them, so all are core.
    assert {"card": "Basalt Monolith", "quantity": 1} in request["main"]
    assert {"card": "Circle of Dreams Druid", "quantity": 1} in request["main"]
    again = rig.gather()
    assert again.combos.source is AnswerSource.CACHED
    assert again.own_combos.source is AnswerSource.CACHED
    assert len(rig.spellbook.combo_bodies) == 2


def test_combos_without_meta_lists_are_unavailable(rig: Rig) -> None:
    rig.edhtop16.status = 403
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
    # The 3rd, 5th and 11th most viewed pair Magda with Kodama; all twelve hits are read to find them.
    rig.archidekt.commander = MAGDA
    rig.archidekt.partnered = {7432856, 23074747, 26605393}
    rig.settings = replace(SETTINGS, peer_decks=12)
    found = adjacent(rig, (MAGDA, KODAMA))
    assert [p.deck.id for p in found.decks] == [7432856, 23074747, 26605393]
    assert found.errors == ()


def test_a_lone_commander_leaves_out_decks_that_pair_it_with_a_partner(rig: Rig) -> None:
    # The 3rd most viewed pairs Magda with Kodama; its off-colour cards would skew a lone Magda's meta.
    rig.archidekt.commander = MAGDA
    rig.archidekt.partnered = {7432856}
    found = adjacent(rig, (MAGDA,))
    assert [p.deck.id for p in found.decks] == [5553433, 13668856]


def test_adjacent_groups_pool_their_decks_once_each(rig: Rig) -> None:
    # The fake search answers every commander with the same decks, so the second group adds none.
    found = adjacent(rig, (ELLIVERE,), (ELLIVERE,))
    assert [p.deck.id for p in found.decks] == [5553433, 13668856, 7432856]


def test_an_adjacent_group_that_cannot_be_read_is_named(rig: Rig) -> None:
    rig.archidekt.status = 500
    found = adjacent(rig, (MAGDA,))
    assert found.decks == ()
    [(name, why)] = found.errors
    assert name == MAGDA
    assert "HTTP 500" in why


def test_a_future_dated_and_a_malformed_entry_leave_the_commander_lists_readable(rig: Rig) -> None:
    page = load_fixture("edhtop16_entries_ellivere.json")
    node_edges = page["data"]["commander"]["entries"]["edges"]
    future = json.loads(json.dumps(node_edges[0]))
    future["node"]["tournament"].update(
        {"TID": "future-test-event", "tournamentDate": "2030-10-26T10:00:00.000Z"}
    )
    broken = json.loads(json.dumps(node_edges[0]))
    broken["node"]["decklistItems"][0]["spice"] = None
    node_edges[:0] = [future, broken]
    rig.edhtop16.pages[ELLIVERE] = [page]

    lists = rig.gather().lists.data

    assert lists is not None
    assert [e.tournament.held_on for e in lists.entries] == [
        date(2026, 10, 4),
        date(2026, 9, 13),
        date(2026, 8, 16),
    ]
