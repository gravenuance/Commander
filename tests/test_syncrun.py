import copy
import json
import logging
import shutil
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

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
from commander.fetchpriority import FetchPriority
from commander.ideas import rejudge_archidekt_ideas
from commander.metadata import MetaData
from commander.models import Deck
from commander.net import NetworkError
from commander.scryfall import CardRefresh, RefreshStatus
from commander.store import LABELS_FILE, StoredDeck, Workspace
from commander.sync import SyncResult
from commander.syncrun import CardState, SyncOptions, SyncServices, run_sync
from conftest import REFRESH_TOKEN, FakeArchidekt, Json, MemoryTokenStore, put_on_sideboard
from test_cli_apply import run
from test_meta import entry, meta_data

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
DOWNLOADED = CardRefresh(RefreshStatus.UPDATED, CardDataMeta("2026-10-15T09:00:00Z", "t", "b"), 12_345)


def services(tmp_path: Path, fake: FakeArchidekt, *, signed_in: bool = False) -> SyncServices:
    tokens = MemoryTokenStore({"makkis": REFRESH_TOKEN} if signed_in else {})
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
            "MAKKIS",
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
        "MAKKIS",
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


def stub_meta(
    monkeypatch: pytest.MonkeyPatch, after_gather: Callable[[int], None] = lambda _: None
) -> list[int]:
    """Replace the network reads of the ideas phase; the ids of the decks gathered for come back in order."""
    gathered: list[int] = []

    def gather(deck: Deck, *_: object, **__: object) -> MetaData:
        gathered.append(deck.id)
        after_gather(deck.id)
        return cast("MetaData", SimpleNamespace(lists=SimpleNamespace(data=1), peers=SimpleNamespace(data=1)))

    def sources(*_: object, **__: object) -> object:
        return object()

    def compile_nothing(*_: object, **__: object) -> dict[Origin, list[BoardCard]]:
        return {}

    monkeypatch.setattr(syncrun, "meta_sources", sources)
    monkeypatch.setattr(syncrun, "gather_for", gather)
    monkeypatch.setattr(syncrun, "compile_ideas_with_adjacent", compile_nothing)
    monkeypatch.setattr(syncrun, "rejudge_archidekt_ideas", judge_nothing)
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
        "MAKKIS",
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
        "MAKKIS",
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

    def compile_ideas(stored: StoredDeck, *_: object, warn: Callable[[str], None], **__: object) -> object:
        if stored.deck.id == 101:
            raise BoardError("board.json is damaged")
        if stored.deck.id == 102:
            warn("a source was unavailable")
        return {}

    monkeypatch.setattr(syncrun, "compile_ideas_with_adjacent", compile_ideas)
    final = dict(sync_states(tmp_path, fake))
    assert final == {101: "incomplete", 102: "incomplete", 103: "done"}


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

    monkeypatch.setattr(syncrun, "gather_for", gather)
    monkeypatch.setattr(syncrun, "rejudge_archidekt_ideas", rejudge_archidekt_ideas)
    sync_ideas(tmp_path, fake)


def lists_with(card: str, playing: int, of: int = 10) -> MetaData:
    """`of` recent top-cut lists, `playing` of which run `card`; all run a Mountain."""
    return meta_data(
        [
            entry(i, 40, 16, [("Mountain", 0), *([(card, 0)] if i <= playing else [])], date(2026, 10, 10))
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
        "MAKKIS",
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
