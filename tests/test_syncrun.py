import copy
import json
import logging
import shutil
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from commander import syncrun
from commander.archidekt import parse_deck
from commander.board import (
    Actor,
    BoardCard,
    BoardError,
    BoardService,
    Origin,
    Status,
    read_activity,
    read_board,
)
from commander.carddb import CARD_DB_FILE, CardDataMeta, CardDb
from commander.config import MetaSettings
from commander.deckcache import Answer, AnswerSource
from commander.deckprofile import Profile, profile_template, read_profile
from commander.deckreads import DeckReads
from commander.fetchpriority import FetchPriority
from commander.ideas import gather_idea_pools, rejudge_ideas, relabel_legacy_ideas
from commander.metadata import (
    TOURNAMENT_BRACKETS,
    AdjacentDecks,
    MetaData,
    MetaSources,
    start_gather_for,
    start_tournament_refresh,
)
from commander.models import Deck
from commander.net import NetworkError
from commander.scryfall import CardRefresh, RefreshStatus
from commander.store import LABELS_FILE, StoredDeck, Workspace
from commander.sync import SyncResult
from commander.syncrun import CardState, SyncOptions, SyncOutcome, SyncServices, run_sync
from conftest import REFRESH_TOKEN, FakeArchidekt, Json, MemoryTokenStore, put_on_sideboard
from test_cli_apply import run
from test_meta import entry, meta_data, peer
from test_metadata import MAGDA, Rig

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
DOWNLOADED = CardRefresh(RefreshStatus.UPDATED, CardDataMeta("2026-10-15T09:00:00Z", "t", "b"), 12_345)


def services(tmp_path: Path, fake: FakeArchidekt, *, signed_in: bool = False) -> SyncServices:
    tokens = MemoryTokenStore({"tester": REFRESH_TOKEN} if signed_in else {})
    return SyncServices(fake.client, tokens, tmp_path / "cache", lambda: NOW)


def test_a_failed_deck_sync_waits_for_the_card_download_and_logs_its_status(
    tmp_path: Path, raw_deck: Json, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    started, finished = threading.Event(), threading.Event()

    def download(*_: object, **__: object) -> CardRefresh:
        started.wait(timeout=30)
        finished.set()
        return DOWNLOADED

    def failing_sync(*_: object, **__: object) -> list[SyncResult]:
        started.set()
        raise NetworkError("Archidekt is down")

    monkeypatch.setattr(syncrun, "refresh_card_db", download)
    monkeypatch.setattr(syncrun, "sync_decks", failing_sync)

    with (
        caplog.at_level(logging.INFO, logger="commander.syncrun"),
        pytest.raises(NetworkError, match="Archidekt is down"),
    ):
        run_sync(
            services(tmp_path, fake),
            Workspace(tmp_path / "decks"),
            "TESTER",
            MetaSettings(),
            SyncOptions(ideas=False),
            on_progress=lambda _: None,
        )

    # The shared client closes when run_sync unwinds, so the download must be over by then.
    assert finished.is_set()
    assert "card data: updated, Scryfall data of 2026-10-15, 12,345 printings" in caplog.messages


def test_labels_that_cannot_be_saved_are_a_warning_not_a_crash(tmp_path: Path, raw_deck: Json) -> None:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    fake.labels = [{"id": 1, "color": "#fa0d11", "name": "Don't Have"}]
    workspace = Workspace(tmp_path / "decks")
    # A folder where the labels file belongs makes the atomic write fail with an OSError.
    (workspace.root / LABELS_FILE).mkdir(parents=True)

    outcome = run_sync(
        services(tmp_path, fake, signed_in=True),
        workspace,
        "TESTER",
        MetaSettings(),
        SyncOptions(cards=False, ideas=False),
        on_progress=lambda _: None,
    )

    assert [w for w in outcome.warnings if w.startswith("card labels not saved: ")]
    assert outcome.cards.state is CardState.SKIPPED
    assert [r.deck_id for r in outcome.results] == [raw_deck["id"]]


DECK_IDS = [101, 102, 103]


def three_decks(tmp_path: Path, raw_deck: Json, card_db_file: Path) -> FakeArchidekt:
    """Three decks named Alpha, Beta and Gamma, which sort in that order in the workspace."""
    fake = FakeArchidekt()
    for deck_id, name in zip(DECK_IDS, ("Alpha", "Beta", "Gamma"), strict=True):
        deck = copy.deepcopy(raw_deck)
        deck["id"], deck["name"] = deck_id, name
        fake.put(deck)
    (tmp_path / "cache").mkdir(exist_ok=True)
    shutil.copy(card_db_file, tmp_path / "cache" / CARD_DB_FILE)
    return fake


def judge_nothing(*_: object, **__: object) -> list[BoardCard]:
    return []


def stub_gather(monkeypatch: pytest.MonkeyPatch, gather: Callable[..., object]) -> None:
    """Replace the meta reads of the ideas phase with `gather`, called as each deck's reads start; what it
    returns is that deck's meta, handed over when the merge asks for it."""

    def start(*args: object, **kwargs: object) -> Callable[[], object]:
        data = gather(*args, **kwargs)
        return lambda: data

    monkeypatch.setattr(syncrun, "start_gather_for", start)


def stub_meta(
    monkeypatch: pytest.MonkeyPatch, after_gather: Callable[[int], None] = lambda _: None
) -> list[int]:
    """Replace the network reads of the ideas phase; the ids of the decks gathered for come back in order."""
    gathered: list[int] = []

    def gather(deck: Deck, *_: object, **__: object) -> MetaData:
        gathered.append(deck.id)
        after_gather(deck.id)
        return cast(
            "MetaData",
            SimpleNamespace(
                lists=SimpleNamespace(data=1, error=None, key_problem=False), peers=SimpleNamespace(data=1)
            ),
        )

    def sources(*_: object, **__: object) -> object:
        return object()

    def compile_nothing(*_: object, **__: object) -> dict[Origin, list[BoardCard]]:
        return {}

    def no_refresh(*_: object, **__: object) -> None:
        return None

    def no_pools(*_: object, **__: object) -> AdjacentDecks:
        return AdjacentDecks((), ())

    monkeypatch.setattr(syncrun, "meta_sources", sources)
    monkeypatch.setattr(syncrun, "start_tournament_refresh", no_refresh)
    stub_gather(monkeypatch, gather)
    monkeypatch.setattr(syncrun, "gather_idea_pools", no_pools)
    monkeypatch.setattr(syncrun, "compile_ideas", compile_nothing)
    monkeypatch.setattr(syncrun, "rejudge_ideas", judge_nothing)
    monkeypatch.setattr(syncrun, "relabel_legacy_ideas", judge_nothing)
    return gathered


def write_views(tmp_path: Path, scores: dict[int, tuple[float, str]]) -> None:
    decks = {str(i): {"score": score, "at": at} for i, (score, at) in scores.items()}
    (tmp_path / "cache" / "views.json").write_text(
        json.dumps({"version": 1, "decks": decks}), encoding="utf-8"
    )


# Decayed to NOW: 103 scores 3, 101 scores 1.5 and 102 scores 1 (4 after two half-lives), and 3 > 1.5 > 1.
VIEWS = {
    101: (1.5, "2026-10-15T12:00:00Z"),
    102: (4.0, "2026-08-16T12:00:00Z"),
    103: (3.0, "2026-10-15T12:00:00Z"),
}


def sync_ideas(tmp_path: Path, fake: FakeArchidekt, priority: FetchPriority | None = None) -> None:
    run_sync(
        services(tmp_path, fake),
        Workspace(tmp_path / "decks"),
        "TESTER",
        MetaSettings(),
        SyncOptions(cards=False),
        priority=priority,
        on_progress=lambda _: None,
    )


def test_ideas_come_in_the_workspace_order_when_no_deck_was_viewed(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = three_decks(tmp_path, raw_deck, card_db_file)
    gathered = stub_meta(monkeypatch)
    sync_ideas(tmp_path, fake)
    assert gathered == [101, 102, 103]


def test_ideas_come_for_the_most_viewed_decks_first(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = three_decks(tmp_path, raw_deck, card_db_file)
    write_views(tmp_path, VIEWS)
    gathered = stub_meta(monkeypatch)
    sync_ideas(tmp_path, fake)
    assert gathered == [103, 101, 102]


def test_decks_with_the_same_score_keep_the_workspace_order(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = three_decks(tmp_path, raw_deck, card_db_file)
    write_views(tmp_path, {103: (2.0, "2026-10-15T12:00:00Z")})
    gathered = stub_meta(monkeypatch)
    sync_ideas(tmp_path, fake)
    assert gathered == [103, 101, 102]


def test_the_selected_deck_goes_first_and_the_rest_follow_by_score(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = three_decks(tmp_path, raw_deck, card_db_file)
    write_views(tmp_path, VIEWS)
    gathered = stub_meta(monkeypatch)
    sync_ideas(tmp_path, fake, FetchPriority(selected=102))
    assert gathered == [102, 103, 101]


def test_a_selected_deck_that_is_not_in_the_workspace_changes_nothing(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = three_decks(tmp_path, raw_deck, card_db_file)
    write_views(tmp_path, VIEWS)
    gathered = stub_meta(monkeypatch)
    sync_ideas(tmp_path, fake, FetchPriority(selected=999))
    assert gathered == [103, 101, 102]


def test_a_deck_opened_while_another_is_fetched_goes_next_and_each_deck_is_fetched_once(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = three_decks(tmp_path, raw_deck, card_db_file)
    write_views(tmp_path, VIEWS)
    priority = FetchPriority(selected=103)

    def opened_during_the_first_fetch(deck_id: int) -> None:
        if deck_id == 103:
            priority.prefer(102)

    gathered = stub_meta(monkeypatch, opened_during_the_first_fetch)
    sync_ideas(tmp_path, fake, priority)
    # 103 finishes first; 102, opened meanwhile, goes before 101, which scores higher.
    assert gathered == [103, 102, 101]


def test_a_deck_opened_after_it_was_fetched_is_not_fetched_again(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = three_decks(tmp_path, raw_deck, card_db_file)
    priority = FetchPriority()

    def reopened_after_it_was_fetched(deck_id: int) -> None:
        if deck_id == 102:
            priority.prefer(101)

    gathered = stub_meta(monkeypatch, reopened_after_it_was_fetched)
    sync_ideas(tmp_path, fake, priority)
    assert gathered == [101, 102, 103]


def test_cmdr_sync_fetches_ideas_in_view_order_without_a_selected_deck(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = three_decks(tmp_path, raw_deck, card_db_file)
    write_views(tmp_path, VIEWS)
    gathered = stub_meta(monkeypatch)
    assert run(["sync", "--no-cards", "--workspace", str(tmp_path / "decks")], tmp_path, fake).code == 0
    assert gathered == [103, 101, 102]


def sync_states(tmp_path: Path, fake: FakeArchidekt) -> list[tuple[int, str]]:
    events: list[tuple[int, str]] = []
    run_sync(
        services(tmp_path, fake),
        Workspace(tmp_path / "decks"),
        "TESTER",
        MetaSettings(),
        SyncOptions(cards=False),
        on_deck=lambda deck_id, state: events.append((deck_id, state.value)),
        on_progress=lambda _: None,
    )
    return events


def test_each_deck_is_queued_then_fetching_then_done_in_the_order_it_is_fetched(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = three_decks(tmp_path, raw_deck, card_db_file)
    write_views(tmp_path, VIEWS)
    stub_meta(monkeypatch)
    assert sync_states(tmp_path, fake) == [
        (103, "queued"),
        (101, "queued"),
        (102, "queued"),
        (103, "fetching"),
        (103, "done"),
        (101, "fetching"),
        (101, "done"),
        (102, "fetching"),
        (102, "done"),
    ]


def test_a_deck_whose_ideas_fail_is_incomplete_and_the_others_carry_on(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = three_decks(tmp_path, raw_deck, card_db_file)
    stub_meta(monkeypatch)

    def gather_pools(stored: StoredDeck, *_: object, warn: Callable[[str], None], **__: object) -> object:
        if stored.deck.id == 102:
            warn("a source was unavailable")
        return AdjacentDecks((), ())

    def compile_ideas(stored: StoredDeck, *_: object, **__: object) -> object:
        if stored.deck.id == 101:
            raise BoardError("board.json is damaged")
        return {}

    monkeypatch.setattr(syncrun, "gather_idea_pools", gather_pools)
    monkeypatch.setattr(syncrun, "compile_ideas", compile_ideas)
    final = dict(sync_states(tmp_path, fake))
    assert final == {101: "incomplete", 102: "incomplete", 103: "done"}


def test_decks_whose_tournament_lists_hit_a_key_problem_are_incomplete_and_the_sync_warns_once(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = three_decks(tmp_path, raw_deck, card_db_file)
    stub_meta(monkeypatch)
    refused = Answer[object](
        None, AnswerSource.UNAVAILABLE, None, "TopDeck refused the key (HTTP 401)", key_problem=True
    )

    def gather(*_: object, **__: object) -> object:
        return SimpleNamespace(lists=refused, peers=SimpleNamespace(data=1))

    stub_gather(monkeypatch, gather)
    states: dict[int, str] = {}
    outcome = run_sync(
        services(tmp_path, fake),
        Workspace(tmp_path / "decks"),
        "TESTER",
        MetaSettings(),
        SyncOptions(cards=False),
        on_deck=lambda deck_id, state: states.__setitem__(deck_id, state.value),
        on_progress=lambda _: None,
    )
    assert states == {101: "incomplete", 102: "incomplete", 103: "incomplete"}
    warning = (
        "tournament lists skipped: missing or refused TopDeck key; "
        "add one in the app's TopDeck panel or with `cmdr topdeck`"
    )
    assert [w for w in outcome.warnings if "TopDeck key" in w] == [warning]


def test_a_deck_below_bracket_4_merges_without_waiting_for_topdeck_and_one_at_bracket_4_does_wait(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = three_decks(tmp_path, raw_deck, card_db_file)
    for deck_id, bracket in zip(DECK_IDS, (4, 3, 3), strict=True):
        fake.decks[deck_id]["edhBracket"] = bracket
    stub_meta(monkeypatch)
    topdeck: Future[None] = Future()
    released = threading.Event()
    merged: list[int] = []
    # What the bracket 4 deck's meta read saw: whether TopDeck had been released by the time it could go on.
    waited_for_release: list[bool] = []

    def start_gather(deck: Deck, *_: object, **__: object) -> Callable[[], object]:
        def finish() -> object:
            if deck.bracket in TOURNAMENT_BRACKETS:
                waited_for_release.append(released.wait(timeout=2))
            return SimpleNamespace(
                lists=SimpleNamespace(data=1, error=None, key_problem=False), peers=SimpleNamespace(data=1)
            )

        return finish

    def compile_ideas(stored: StoredDeck, *_: object, **__: object) -> dict[Origin, list[BoardCard]]:
        merged.append(stored.deck.id)
        if {102, 103} <= set(merged) and not released.is_set():
            released.set()
            topdeck.set_result(None)
        return {}

    def start_topdeck(*_: object, **__: object) -> Future[None]:
        return topdeck

    monkeypatch.setattr(syncrun, "start_tournament_refresh", start_topdeck)
    monkeypatch.setattr(syncrun, "start_gather_for", start_gather)
    monkeypatch.setattr(syncrun, "compile_ideas", compile_ideas)
    sync_ideas(tmp_path, fake)
    # The two decks below bracket 4 merge while TopDeck is still out; the bracket 4 deck goes on after it.
    assert merged == [102, 103, 101]
    assert waited_for_release == [True]


def test_an_error_mid_sync_waits_for_topdeck_before_the_client_closes_and_logs_what_topdeck_hit(
    tmp_path: Path,
    raw_deck: Json,
    card_db_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = three_decks(tmp_path, raw_deck, card_db_file)
    fake.decks[DECK_IDS[0]]["edhBracket"] = 4
    stub_meta(monkeypatch)
    clients: list[httpx.Client] = []
    failing, logged = threading.Event(), threading.Event()
    open_when_topdeck_ended: list[bool] = []

    def load(**_: object) -> None:
        failing.wait(timeout=30)
        open_when_topdeck_ended.append(not clients[-1].is_closed)
        raise RuntimeError("TopDeck's thread broke")

    def sources(*_: object, **__: object) -> object:
        return SimpleNamespace(settings=MetaSettings(), tournaments=SimpleNamespace(load=load))

    def start_topdeck(*args: Any, **kwargs: Any) -> Future[None]:  # noqa: ANN401 - passed on as it came
        future = start_tournament_refresh(*args, **kwargs)
        assert future is not None
        # Callbacks run in the order added, so this one says the failure was logged.
        future.add_done_callback(lambda _: logged.set())
        return future

    def start_gather(deck: Deck, *_: object, **__: object) -> Callable[[], object]:
        if deck.id == DECK_IDS[1]:
            failing.set()
            raise RuntimeError("the second deck's reads broke")
        return lambda: SimpleNamespace(
            lists=SimpleNamespace(data=1, error=None, key_problem=False), peers=SimpleNamespace(data=1)
        )

    def open_client() -> httpx.Client:
        clients.append(fake.client())
        return clients[-1]

    monkeypatch.setattr(syncrun, "meta_sources", sources)
    monkeypatch.setattr(syncrun, "start_tournament_refresh", start_topdeck)
    monkeypatch.setattr(syncrun, "start_gather_for", start_gather)
    with caplog.at_level(logging.WARNING, logger="commander.metadata"):
        with pytest.raises(RuntimeError, match="second deck's reads broke"):
            run_sync(
                replace(services(tmp_path, fake), client_factory=open_client),
                Workspace(tmp_path / "decks"),
                "TESTER",
                MetaSettings(),
                SyncOptions(cards=False),
                on_progress=lambda _: None,
            )
        assert logged.wait(timeout=30)
    assert open_when_topdeck_ended == [True]
    [record] = [r for r in caplog.records if r.name == "commander.metadata"]
    assert record.levelno == logging.WARNING
    assert "TopDeck" in record.getMessage()
    assert record.exc_info is not None
    assert str(record.exc_info[1]) == "TopDeck's thread broke"


def test_a_sync_prunes_the_cached_deck_reads_once_after_all_the_decks_are_read(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = three_decks(tmp_path, raw_deck, card_db_file)
    stub_meta(monkeypatch)
    rig = Rig(tmp_path)
    rig.archidekt.commander = MAGDA
    merged: list[int] = []
    pruned: list[list[int]] = []
    prune = DeckReads.prune

    def counting_prune(self: DeckReads, *, now: datetime, window_start: datetime) -> int:
        pruned.append(list(merged))
        return prune(self, now=now, window_start=window_start)

    def compile_ideas(stored: StoredDeck, *_: object, **__: object) -> dict[Origin, list[BoardCard]]:
        merged.append(stored.deck.id)
        return {}

    def real_sources(*_: object, **__: object) -> MetaSources:
        return rig.sources()

    # The reads of the decks are the real ones, over the fake services of the metadata tests.
    monkeypatch.setattr(syncrun, "meta_sources", real_sources)
    monkeypatch.setattr(syncrun, "start_gather_for", start_gather_for)
    monkeypatch.setattr(syncrun, "gather_idea_pools", gather_idea_pools)
    monkeypatch.setattr(syncrun, "compile_ideas", compile_ideas)
    monkeypatch.setattr(DeckReads, "prune", counting_prune)
    sync_ideas(tmp_path, fake)
    assert merged == DECK_IDS
    # Once, with every merge over: another deck's peers are still being read until then.
    assert pruned == [DECK_IDS]


def populated(tmp_path: Path, raw_deck: Json, card_db_file: Path) -> tuple[FakeArchidekt, StoredDeck]:
    """A fake Archidekt holding the fixture deck, its Maybeboard and one Sideboard card, and the workspace
    with that deck saved but no sync run yet."""
    put_on_sideboard(raw_deck, 7_000_000_001, "Chaos Warp")
    fake = FakeArchidekt()
    fake.put(raw_deck)
    (tmp_path / "cache").mkdir(exist_ok=True)
    shutil.copy(card_db_file, tmp_path / "cache" / CARD_DB_FILE)
    deck = parse_deck(raw_deck)
    return fake, StoredDeck(Workspace(tmp_path / "decks").save(deck), deck)


def sync_with_lists(
    tmp_path: Path, fake: FakeArchidekt, monkeypatch: pytest.MonkeyPatch, data: MetaData | None
) -> None:
    """A sync whose ideas phase reads `data` (no read at all for None) and compiles no new ideas."""
    stub_meta(monkeypatch)

    def gather(*_: object, **__: object) -> MetaData | None:
        return data

    stub_gather(monkeypatch, gather)
    monkeypatch.setattr(syncrun, "rejudge_ideas", rejudge_ideas)
    monkeypatch.setattr(syncrun, "relabel_legacy_ideas", relabel_legacy_ideas)
    sync_ideas(tmp_path, fake)


def lists_with(card: str, playing: int, of: int = 10) -> MetaData:
    """`of` recent top-cut lists, `playing` of which run `card`; all run a Mountain."""
    return meta_data(
        [
            entry(i, 40, 16, ["Mountain", *([card] if i <= playing else [])], date(2026, 10, 10))
            for i in range(1, of + 1)
        ]
    )


def test_a_sync_ignores_the_maybeboard_and_sideboard_and_writes_nothing_to_archidekt(
    tmp_path: Path, raw_deck: Json, card_db_file: Path
) -> None:
    fake, stored = populated(tmp_path, raw_deck, card_db_file)

    outcome = run_sync(
        services(tmp_path, fake),
        Workspace(tmp_path / "decks"),
        "TESTER",
        MetaSettings(),
        SyncOptions(cards=False, ideas=False),
        on_progress=lambda _: None,
    )

    assert not outcome.warnings
    assert read_board(stored.folder).cards == ()
    assert read_activity(stored.folder) == []
    assert fake.writes == []
    # The deck copy still mirrors Archidekt, Maybeboard included.
    saved = Workspace(tmp_path / "decks").load_all()[0].deck
    assert sorted(c.name for c in saved.cards if not c.in_deck) == [
        "Chaos Warp",
        "High-Speed Hoverbike",
        "Tempestra, Dame of Games",
    ]


def test_a_sync_adds_the_empty_grace_table_to_each_profile_that_lacks_one(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake, stored = populated(tmp_path, raw_deck, card_db_file)
    profile = stored.folder / "profile.toml"
    profile.write_text("version = 1\n\n[ideas]\ncommanders = []\n", encoding="utf-8")

    def sync(options: SyncOptions) -> None:
        run_sync(
            services(tmp_path, fake),
            Workspace(tmp_path / "decks"),
            "TESTER",
            MetaSettings(),
            options,
            on_progress=lambda _: None,
        )

    # A dry run writes nothing.
    sync(SyncOptions(cards=False, ideas=False, dry_run=True))
    assert "[grace]" not in profile.read_text(encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="commander.syncrun"):
        sync(SyncOptions(cards=False, ideas=False))
    text = profile.read_text(encoding="utf-8")
    assert text.index("\n[grace]\n") < text.index("\n[ideas]\n")
    assert [r.getMessage() for r in caplog.records if "[grace]" in r.getMessage()] == [
        f"{stored.folder.name}: added an empty [grace] table to profile.toml"
    ]
    # A second sync finds it there.
    sync(SyncOptions(cards=False, ideas=False))
    assert profile.read_text(encoding="utf-8") == text


def test_a_sync_gives_a_deck_without_a_profile_the_template_with_its_empty_grace_table(
    tmp_path: Path, raw_deck: Json, card_db_file: Path
) -> None:
    fake, stored = populated(tmp_path, raw_deck, card_db_file)
    profile = stored.folder / "profile.toml"

    def sync(options: SyncOptions) -> None:
        run_sync(
            services(tmp_path, fake),
            Workspace(tmp_path / "decks"),
            "TESTER",
            MetaSettings(),
            options,
            on_progress=lambda _: None,
        )

    sync(SyncOptions(cards=False, ideas=False, dry_run=True))
    assert not profile.exists()
    sync(SyncOptions(cards=False, ideas=False))
    assert profile.read_text(encoding="utf-8") == profile_template(stored.deck)
    assert read_profile(stored.folder) == Profile()
    # A second sync leaves the file as it is.
    profile.write_text(profile.read_text(encoding="utf-8") + "# mine\n", encoding="utf-8")
    sync(SyncOptions(cards=False, ideas=False))
    assert profile.read_text(encoding="utf-8").endswith("# mine\n")


def sync_profiles(tmp_path: Path, fake: FakeArchidekt) -> SyncOutcome:
    return run_sync(
        services(tmp_path, fake),
        Workspace(tmp_path / "decks"),
        "TESTER",
        MetaSettings(),
        SyncOptions(cards=False, ideas=False),
        on_progress=lambda _: None,
    )


def test_two_folders_for_an_untracked_deck_do_not_stop_the_other_decks_getting_their_profile(
    tmp_path: Path, raw_deck: Json, card_db_file: Path
) -> None:
    fake, stored = populated(tmp_path, raw_deck, card_db_file)
    # These sort before the tracked deck's folder, so an abort at them would skip it.
    (tmp_path / "decks" / "1000-first").mkdir()
    (tmp_path / "decks" / "1000-second").mkdir()

    outcome = sync_profiles(tmp_path, fake)

    assert (stored.folder / "profile.toml").read_text(encoding="utf-8") == profile_template(stored.deck)
    assert not (tmp_path / "decks" / "1000-first" / "profile.toml").exists()
    assert not (tmp_path / "decks" / "1000-second" / "profile.toml").exists()
    assert not outcome.warnings


def test_a_profile_that_appears_during_the_sync_is_a_warning_and_not_a_failed_sync(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, stored = populated(tmp_path, raw_deck, card_db_file)
    profile = stored.folder / "profile.toml"
    write_template = syncrun.write_profile_template

    def written_by_someone_else(folder: Path, deck: Deck) -> Path:
        profile.write_text("version = 1\n", encoding="utf-8")
        return write_template(folder, deck)

    monkeypatch.setattr(syncrun, "write_profile_template", written_by_someone_else)

    outcome = sync_profiles(tmp_path, fake)

    assert profile.read_text(encoding="utf-8") == "version = 1\n"
    assert [w for w in outcome.warnings if "[grace]" in w] == [
        f"deck {stored.deck.id}: [grace] table not added: {profile} already exists; edit it instead"
    ]


def seed_board(stored: StoredDeck, card_db_file: Path) -> dict[str, str]:
    """A board with two open cards from the old Maybeboard pick-up and one of each kind that must stay as it
    is; the ids by card name."""
    ids: dict[str, str] = {}
    with CardDb(card_db_file) as db:
        service = BoardService(stored.folder, stored.deck, db, now=lambda: NOW)
        for name, source in (
            ("High-Speed Hoverbike", Actor.ARCHIDEKT),
            ("Tempestra, Dame of Games", Actor.ARCHIDEKT),
            ("Lightning Bolt", Actor.YOU),
            ("Arcane Signet", Actor.ARCHIDEKT),
            ("Chaos Warp", Actor.ARCHIDEKT),
        ):
            ids[name] = service.propose(name, source, reason="seeded").id
        service.decide(ids["Arcane Signet"], Status.ADD, Actor.YOU, choice="Disruptor Flute")
        service.decide(ids["Chaos Warp"], Status.REJECTED, Actor.YOU, note="no thanks")
    return ids


def test_an_idea_from_the_old_pick_up_that_fails_the_idea_test_is_turned_down_and_a_passing_one_stays(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, stored = populated(tmp_path, raw_deck, card_db_file)
    ids = seed_board(stored, card_db_file)

    # The Hoverbike is in 9 of 10 recent lists, Tempestra in none.
    sync_with_lists(tmp_path, fake, monkeypatch, lists_with("High-Speed Hoverbike", 9))

    board = read_board(stored.folder)
    hoverbike, tempestra = (
        board.find(ids["High-Speed Hoverbike"]),
        board.find(ids["Tempestra, Dame of Games"]),
    )
    assert hoverbike is not None
    assert tempestra is not None
    assert hoverbike.status is Status.OPEN
    assert (tempestra.status, tempestra.note, tempestra.rejected_share) == (
        Status.REJECTED,
        "no longer makes the cut",
        0.0,
    )
    turned_down = [
        a for a in read_activity(stored.folder) if a.action == "turned down" and a.actor is Actor.CMDR
    ]
    assert [(a.card, a.detail) for a in turned_down] == [
        ("Tempestra, Dame of Games", "no longer makes the cut")
    ]
    assert fake.writes == []


def test_a_passing_idea_of_the_old_pick_up_is_relabelled_as_a_compiled_one(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, stored = populated(tmp_path, raw_deck, card_db_file)
    ids = seed_board(stored, card_db_file)

    sync_with_lists(tmp_path, fake, monkeypatch, lists_with("High-Speed Hoverbike", 9))

    board = read_board(stored.folder)
    hoverbike = board.find(ids["High-Speed Hoverbike"])
    assert hoverbike is not None
    assert (hoverbike.status, hoverbike.source, hoverbike.origin) == (Status.OPEN, Actor.CMDR, Origin.META)
    assert hoverbike.reason == (
        f"in 9 of 10 top-cut {stored.deck.short_name} lists on TopDeck since 2026-08-16"
    )
    # The cards that were turned down, queued, or by someone else keep their source and reason.
    kept = {
        name: (c.source, c.origin, c.reason)
        for name in ("Tempestra, Dame of Games", "Lightning Bolt", "Arcane Signet", "Chaos Warp")
        if (c := board.find(ids[name])) is not None
    }
    assert kept == {
        "Tempestra, Dame of Games": (Actor.ARCHIDEKT, None, "seeded"),
        "Lightning Bolt": (Actor.YOU, None, "seeded"),
        "Arcane Signet": (Actor.ARCHIDEKT, None, "seeded"),
        "Chaos Warp": (Actor.ARCHIDEKT, None, "seeded"),
    }


def test_the_old_ideas_are_left_alone_when_no_list_was_read(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, stored = populated(tmp_path, raw_deck, card_db_file)
    seed_board(stored, card_db_file)
    before = (stored.folder / "board.json").read_text(encoding="utf-8")

    sync_with_lists(tmp_path, fake, monkeypatch, meta_data([]))

    assert (stored.folder / "board.json").read_text(encoding="utf-8") == before


def test_only_open_cards_from_the_old_pick_up_are_judged(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, stored = populated(tmp_path, raw_deck, card_db_file)
    ids = seed_board(stored, card_db_file)

    # Nothing in the lists but Mountains, so every card fails the test.
    sync_with_lists(tmp_path, fake, monkeypatch, lists_with("Mountain", 10))

    board = read_board(stored.folder)
    states = {name: (c.status, c.source) for name, id_ in ids.items() if (c := board.find(id_)) is not None}
    assert states == {
        "High-Speed Hoverbike": (Status.REJECTED, Actor.ARCHIDEKT),
        "Tempestra, Dame of Games": (Status.REJECTED, Actor.ARCHIDEKT),
        # Another source, a card queued to add, and a card already turned down: as they were.
        "Lightning Bolt": (Status.OPEN, Actor.YOU),
        "Arcane Signet": (Status.ADD, Actor.ARCHIDEKT),
        "Chaos Warp": (Status.REJECTED, Actor.ARCHIDEKT),
    }
    chaos_warp = board.find(ids["Chaos Warp"])
    assert chaos_warp is not None
    assert chaos_warp.note == "no thanks"


def sync_outcome(
    tmp_path: Path,
    fake: FakeArchidekt,
    monkeypatch: pytest.MonkeyPatch,
    data: MetaData,
    pools: AdjacentDecks | None = None,
) -> SyncOutcome:
    """A sync whose ideas phase reads `data` and `pools` and compiles no new ideas."""
    stub_meta(monkeypatch)

    def read_data(*_: object, **__: object) -> MetaData:
        return data

    def read_pools(*_: object, **__: object) -> AdjacentDecks:
        return pools or AdjacentDecks((), ())

    stub_gather(monkeypatch, read_data)
    monkeypatch.setattr(syncrun, "gather_idea_pools", read_pools)
    monkeypatch.setattr(syncrun, "rejudge_ideas", rejudge_ideas)
    monkeypatch.setattr(syncrun, "relabel_legacy_ideas", relabel_legacy_ideas)
    return run_sync(
        services(tmp_path, fake),
        Workspace(tmp_path / "decks"),
        "TESTER",
        MetaSettings(),
        SyncOptions(cards=False),
        on_progress=lambda _: None,
    )


def status_of(folder: Path, card_id: str) -> Status:
    card = read_board(folder).find(card_id)
    assert card is not None
    return card.status


def test_a_sync_turns_down_a_compiled_idea_that_no_longer_passes_and_reports_it(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, stored = populated(tmp_path, raw_deck, card_db_file)
    with CardDb(card_db_file) as db:
        service = BoardService(stored.folder, stored.deck, db, now=lambda: NOW)
        weak = service.propose("Tempestra, Dame of Games", Actor.CMDR, origin=Origin.META)
        strong = service.propose("High-Speed Hoverbike", Actor.CMDR, origin=Origin.META)

    outcome = sync_outcome(tmp_path, fake, monkeypatch, lists_with("High-Speed Hoverbike", 9))

    assert outcome.turned_down == {stored.deck.short_name: ["Tempestra, Dame of Games"]}
    assert (status_of(stored.folder, weak.id), status_of(stored.folder, strong.id)) == (
        Status.REJECTED,
        Status.OPEN,
    )


def test_a_sync_judges_an_alike_idea_only_when_every_commander_group_was_read(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, stored = populated(tmp_path, raw_deck, card_db_file)
    (stored.folder / "profile.toml").write_text(
        'version = 1\n[ideas]\ncommanders = ["The Gitrog Monster"]\n', encoding="utf-8"
    )
    with CardDb(card_db_file) as db:
        signet = BoardService(stored.folder, stored.deck, db, now=lambda: NOW).propose(
            "Arcane Signet", Actor.CMDR, origin=Origin.ADJACENT
        )
    # Arcane Signet is in 1 of 4 pooled decks, under the half a staple needs.
    pool = tuple(peer(i, ["Arcane Signet"] if i == 1 else []) for i in range(1, 5))
    data = lists_with("High-Speed Hoverbike", 9)

    partial = sync_outcome(tmp_path, fake, monkeypatch, data, AdjacentDecks(pool, (), (), groups_failed=True))
    assert (partial.turned_down, status_of(stored.folder, signet.id)) == ({}, Status.OPEN)

    whole = sync_outcome(tmp_path, fake, monkeypatch, data, AdjacentDecks(pool, ()))
    assert whole.turned_down == {stored.deck.short_name: ["Arcane Signet"]}


HELD_LATER = ("Disruptor Flute", "Battered Golem")


def test_a_sync_closes_the_open_ideas_the_main_deck_holds_and_keeps_the_others(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, stored = populated(tmp_path, raw_deck, card_db_file)
    # Proposed while the deck lacked the first two; a save or the user on Archidekt put them in since.
    earlier = replace(stored.deck, cards=tuple(c for c in stored.deck.cards if c.name not in HELD_LATER))
    with CardDb(card_db_file) as db:
        service = BoardService(stored.folder, earlier, db, now=lambda: NOW)
        flute = service.propose("Disruptor Flute", Actor.CMDR, origin=Origin.META)
        golem = service.propose("Battered Golem", Actor.YOU)
        # In the Sideboard, in the Maybeboard, and a basic land the deck can always take more of.
        warp = service.propose("Chaos Warp", Actor.YOU)
        hoverbike = service.propose("High-Speed Hoverbike", Actor.CMDR, origin=Origin.META)
        mountain = service.propose("Mountain", Actor.YOU)

    outcome = sync_outcome(tmp_path, fake, monkeypatch, lists_with("High-Speed Hoverbike", 9))

    cards = {"flute": flute, "golem": golem, "warp": warp, "hoverbike": hoverbike, "mountain": mountain}
    assert {name: status_of(stored.folder, card.id) for name, card in cards.items()} == {
        "flute": Status.APPLIED,
        "golem": Status.APPLIED,
        "warp": Status.OPEN,
        "hoverbike": Status.OPEN,
        "mountain": Status.OPEN,
    }
    closed = read_board(stored.folder).find(flute.id)
    assert closed is not None
    assert closed.note == "already in the deck"
    assert outcome.closed == {stored.deck.short_name: ["Disruptor Flute", "Battered Golem"]}
    assert outcome.turned_down == {}
    [first, second] = [a for a in read_activity(stored.folder) if a.action == "closed"]
    assert (first.actor, first.card, first.detail) == (Actor.CMDR, "Disruptor Flute", "already in the deck")
    assert second.card == "Battered Golem"
    assert fake.writes == []


def test_a_sync_clears_the_count_changes_the_deck_already_meets_and_keeps_the_others(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, stored = populated(tmp_path, raw_deck, card_db_file)
    # Queued while the deck held four Mountains and one Disruptor Flute; it holds six Mountains now.
    earlier = copy.deepcopy(raw_deck)
    for entry_ in earlier["cards"]:
        if entry_["card"]["oracleCard"]["name"] == "Mountain":
            entry_["quantity"] = 4
    with CardDb(card_db_file) as db:
        service = BoardService(stored.folder, parse_deck(earlier), db, now=lambda: NOW)
        service.set_count("Mountain", 6, Actor.YOU)
        service.set_count("Disruptor Flute", 0, Actor.YOU)

    outcome = sync_outcome(tmp_path, fake, monkeypatch, lists_with("High-Speed Hoverbike", 9))

    assert [c.name for c in read_board(stored.folder).counts] == ["Disruptor Flute"]
    assert outcome.counts_cleared == {stored.deck.short_name: ["Mountain 4 → 6"]}
    [entry_] = [a for a in read_activity(stored.folder) if a.action == "count change done"]
    assert (entry_.actor, entry_.card, entry_.detail) == (Actor.CMDR, "Mountain", "4 → 6; the deck holds 6")
    assert fake.writes == []


def test_a_sync_leaves_a_queued_card_the_deck_now_holds_for_review_to_mark_applied(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, stored = populated(tmp_path, raw_deck, card_db_file)
    with CardDb(card_db_file) as db:
        service = BoardService(stored.folder, stored.deck, db, now=lambda: NOW)
        warp = service.propose("Chaos Warp", Actor.YOU)
        service.decide(warp.id, Status.ADD, Actor.YOU, choice="Disruptor Flute")
    # The deck as a later pull finds it: Chaos Warp moved from the Sideboard into a category in the deck.
    moved = copy.deepcopy(raw_deck)
    for entry_ in moved["cards"]:
        if entry_["card"]["oracleCard"]["name"] == "Chaos Warp":
            entry_["categories"] = ["Stax"]
    Workspace(tmp_path / "decks").save(parse_deck(moved))

    outcome = sync_outcome(tmp_path, fake, monkeypatch, lists_with("High-Speed Hoverbike", 9))

    assert status_of(stored.folder, warp.id) is Status.ADD
    assert outcome.closed == {}
