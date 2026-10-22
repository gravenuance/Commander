"""The ideas phase asks TopDeck once, in the background, and does the other sources' work meanwhile."""

import logging
import threading
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from commander import syncrun
from commander.board import BoardCard, Origin
from commander.config import MetaSettings
from commander.metadata import AdjacentDecks, MetaSources
from commander.net import Fetcher, Pacer, WaitListener
from commander.store import StoredDeck, Workspace
from commander.syncrun import FetchState, SyncOptions, SyncOutcome, run_sync
from commander.topdeck import TopdeckClient
from commander.topdeckstore import TournamentStore
from conftest import Json
from test_metadata import Rig
from test_syncrun import DECK_IDS, judge_nothing, services, three_decks

WAIT_SECONDS = 30


class Gate:
    """Holds TopDeck's answers until the test opens it, and records the order things happened in."""

    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        self.opened = threading.Event()
        self.asked = threading.Event()
        self.events: list[str] = []
        self._lock = threading.Lock()

    def note(self, event: str) -> None:
        with self._lock:
            self.events.append(event)

    def open(self) -> None:
        self.note("released")
        self.opened.set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.note("topdeck asked")
        self.asked.set()
        assert self.opened.wait(WAIT_SECONDS), "TopDeck was never released"
        return self.rig.topdeck.handler(request)

    def sources(self) -> MetaSources:
        held = TopdeckClient(
            Fetcher(httpx.Client(transport=httpx.MockTransport(self.handler)), max_attempts=1),
            Pacer(0),
            TournamentStore(self.rig.cache),
            self.rig.keys,
        )
        return replace(self.rig.sources(), tournaments=held)


def test_archidekt_is_read_for_every_deck_while_topdeck_is_still_answering_and_topdeck_is_asked_once(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = three_decks(tmp_path, raw_deck, card_db_file)
    rig = Rig(tmp_path)
    gate = Gate(rig)
    pools_read: dict[int, threading.Event] = {deck_id: threading.Event() for deck_id in DECK_IDS}

    def pools(stored: StoredDeck, *_: object, **__: object) -> AdjacentDecks:
        gate.note(f"archidekt {stored.deck.id}")
        pools_read[stored.deck.id].set()
        return AdjacentDecks((), ())

    def merged(stored: StoredDeck, *_: object, **__: object) -> dict[Origin, list[BoardCard]]:
        gate.note(f"merge {stored.deck.id}")
        return {}

    def held_sources(*_: object, **__: object) -> MetaSources:
        return gate.sources()

    monkeypatch.setattr(syncrun, "meta_sources", held_sources)
    monkeypatch.setattr(syncrun, "gather_idea_pools", pools)
    monkeypatch.setattr(syncrun, "compile_ideas", merged)
    monkeypatch.setattr(syncrun, "rejudge_ideas", judge_nothing)
    monkeypatch.setattr(syncrun, "relabel_legacy_ideas", judge_nothing)

    outcomes: list[SyncOutcome] = []

    def sync() -> None:
        outcomes.append(
            run_sync(
                services(tmp_path, fake),
                Workspace(tmp_path / "decks"),
                "TESTER",
                MetaSettings(),
                SyncOptions(cards=False),
                on_progress=lambda _: None,
            )
        )

    worker = threading.Thread(target=sync, daemon=True)
    worker.start()
    try:
        first_two_read = all(pools_read[deck_id].wait(WAIT_SECONDS) for deck_id in DECK_IDS[:2])
        while_waiting = list(gate.events)
        searches_while_waiting = len(rig.archidekt.searches)
    finally:
        gate.open()
        worker.join(WAIT_SECONDS)

    assert first_two_read, while_waiting
    # TopDeck had been asked, and had not answered, when the first two decks' Archidekt reads were in.
    assert while_waiting.count("topdeck asked") == 1
    assert "merge 101" not in while_waiting
    assert searches_while_waiting > 0
    [outcome] = outcomes
    assert not outcome.warnings
    # Each chunk of the window once, not once for every deck.
    spans = [(r["start"], r["end"]) for r in rig.topdeck.requests]
    assert len(spans) == len(set(spans)) == 9
    # The reads end together once TopDeck answers, so the merges come in whatever order they finish.
    merges = sorted(e for e in gate.events if e.startswith("merge"))
    assert merges == ["merge 101", "merge 102", "merge 103"]
    assert gate.events.index("released") < min(gate.events.index(m) for m in merges)


def test_the_progress_line_names_the_429_wait_while_it_lasts_and_drops_it_after(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = three_decks(tmp_path, raw_deck, card_db_file)
    rig = Rig(tmp_path)
    shown: list[str] = []
    shown_while_paused: list[str] = []
    answers = [httpx.Response(429, headers={"Retry-After": "30"})]
    clock = [0.0]

    def topdeck(request: httpx.Request) -> httpx.Response:
        return answers.pop(0) if answers else rig.topdeck.handler(request)

    def pause(seconds: float) -> None:
        shown_while_paused.append(shown[-1])
        clock[0] += seconds

    def sources(*_: object, waits: WaitListener | None = None, **__: object) -> MetaSources:
        held = TopdeckClient(
            Fetcher(httpx.Client(transport=httpx.MockTransport(topdeck)), max_attempts=1),
            Pacer(12.0, sleep=pause, monotonic=lambda: clock[0]),
            TournamentStore(rig.cache),
            rig.keys,
            waits=waits,
        )
        return replace(rig.sources(), tournaments=held)

    def no_pools(*_: object, **__: object) -> AdjacentDecks:
        return AdjacentDecks((), ())

    def merged(*_: object, **__: object) -> dict[Origin, list[BoardCard]]:
        return {}

    monkeypatch.setattr(syncrun, "meta_sources", sources)
    monkeypatch.setattr(syncrun, "gather_idea_pools", no_pools)
    monkeypatch.setattr(syncrun, "compile_ideas", merged)
    monkeypatch.setattr(syncrun, "rejudge_ideas", judge_nothing)
    monkeypatch.setattr(syncrun, "relabel_legacy_ideas", judge_nothing)
    run_sync(
        services(tmp_path, fake),
        Workspace(tmp_path / "decks"),
        "TESTER",
        MetaSettings(),
        SyncOptions(cards=False),
        on_progress=shown.append,
    )

    # The first pause is the 429's 30 seconds; the ones after are the 12 second spacing of the next chunks.
    assert shown_while_paused[0].startswith("TopDeck: waiting 30 s (rate limit)")
    assert not any("waiting" in line for line in shown_while_paused[1:])
    after = shown[shown.index(shown_while_paused[0]) + 1 :]
    assert any(line.startswith("TopDeck: recent tournaments") for line in after)
    assert "waiting" not in shown[-1]


class FullDisk(TournamentStore):
    """A store that cannot be written, as when an indexer holds the file or the disk is full."""

    def save(self, record: object) -> None:  # noqa: ARG002
        raise PermissionError("denied")


def test_a_tournament_store_that_cannot_be_written_is_a_logged_warning_and_every_deck_is_still_merged(
    tmp_path: Path,
    raw_deck: Json,
    card_db_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = three_decks(tmp_path, raw_deck, card_db_file)
    rig = Rig(tmp_path)
    merged_decks: list[int] = []

    def sources(*_: object, **__: object) -> MetaSources:
        broken = TopdeckClient(
            Fetcher(rig.topdeck.client(), max_attempts=1), Pacer(0), FullDisk(rig.cache), rig.keys
        )
        return replace(rig.sources(), tournaments=broken)

    def no_pools(*_: object, **__: object) -> AdjacentDecks:
        return AdjacentDecks((), ())

    def merged(stored: StoredDeck, *_: object, **__: object) -> dict[Origin, list[BoardCard]]:
        merged_decks.append(stored.deck.id)
        return {}

    monkeypatch.setattr(syncrun, "meta_sources", sources)
    monkeypatch.setattr(syncrun, "gather_idea_pools", no_pools)
    monkeypatch.setattr(syncrun, "compile_ideas", merged)
    monkeypatch.setattr(syncrun, "rejudge_ideas", judge_nothing)
    monkeypatch.setattr(syncrun, "relabel_legacy_ideas", judge_nothing)

    with caplog.at_level(logging.WARNING, logger="commander.topdeck"):
        outcome = run_sync(
            services(tmp_path, fake),
            Workspace(tmp_path / "decks"),
            "TESTER",
            MetaSettings(),
            SyncOptions(cards=False),
            on_progress=lambda _: None,
        )

    assert sorted(merged_decks) == sorted(DECK_IDS)
    assert not outcome.failed
    assert "TopDeck events could not be stored: denied" in caplog.text
    # The store is tried once, not once for every deck's read.
    assert len(rig.topdeck.requests) == 1


def test_an_unexpected_error_in_one_decks_read_is_that_decks_warning_and_the_others_are_merged(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = three_decks(tmp_path, raw_deck, card_db_file)
    rig = Rig(tmp_path)
    merged_decks: list[int] = []

    def sources(*_: object, **__: object) -> MetaSources:
        return rig.sources()

    def pools(stored: StoredDeck, *_: object, **__: object) -> AdjacentDecks:
        if stored.deck.id == DECK_IDS[0]:
            raise ZeroDivisionError("a bug in a read")
        return AdjacentDecks((), ())

    def merged(stored: StoredDeck, *_: object, **__: object) -> dict[Origin, list[BoardCard]]:
        merged_decks.append(stored.deck.id)
        return {}

    monkeypatch.setattr(syncrun, "meta_sources", sources)
    monkeypatch.setattr(syncrun, "gather_idea_pools", pools)
    monkeypatch.setattr(syncrun, "compile_ideas", merged)
    monkeypatch.setattr(syncrun, "rejudge_ideas", judge_nothing)
    monkeypatch.setattr(syncrun, "relabel_legacy_ideas", judge_nothing)
    states: dict[int, FetchState] = {}

    outcome = run_sync(
        services(tmp_path, fake),
        Workspace(tmp_path / "decks"),
        "TESTER",
        MetaSettings(),
        SyncOptions(cards=False),
        on_deck=states.__setitem__,
        on_progress=lambda _: None,
    )

    assert sorted(merged_decks) == sorted(DECK_IDS[1:])
    assert states[DECK_IDS[0]] is FetchState.INCOMPLETE
    assert any("a bug in a read" in warning for warning in outcome.warnings), outcome.warnings


class BuggyStore(TournamentStore):
    def save(self, record: object) -> None:  # noqa: ARG002
        raise ZeroDivisionError("a bug in the store")


def test_an_unexpected_error_in_topdecks_thread_is_reported_and_the_sync_still_ends(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = three_decks(tmp_path, raw_deck, card_db_file)
    rig = Rig(tmp_path)

    def sources(*_: object, **__: object) -> MetaSources:
        buggy = TopdeckClient(
            Fetcher(rig.topdeck.client(), max_attempts=1), Pacer(0), BuggyStore(rig.cache), rig.keys
        )
        return replace(rig.sources(), tournaments=buggy)

    def no_pools(*_: object, **__: object) -> AdjacentDecks:
        return AdjacentDecks((), ())

    monkeypatch.setattr(syncrun, "meta_sources", sources)

    def merged(*_: object, **__: object) -> dict[Origin, list[BoardCard]]:
        return {}

    monkeypatch.setattr(syncrun, "gather_idea_pools", no_pools)
    monkeypatch.setattr(syncrun, "compile_ideas", merged)
    monkeypatch.setattr(syncrun, "rejudge_ideas", judge_nothing)
    monkeypatch.setattr(syncrun, "relabel_legacy_ideas", judge_nothing)

    outcome = run_sync(
        services(tmp_path, fake),
        Workspace(tmp_path / "decks"),
        "TESTER",
        MetaSettings(),
        SyncOptions(cards=False),
        on_progress=lambda _: None,
    )

    assert "TopDeck's tournament lists were not fetched: unexpected error" in outcome.warnings
    assert sum("a bug in the store" in warning for warning in outcome.warnings) == len(DECK_IDS)
