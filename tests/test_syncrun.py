import copy
import json
import logging
import shutil
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from commander import syncrun
from commander.board import BoardCard, Origin
from commander.carddb import CARD_DB_FILE, CardDataMeta
from commander.config import MetaSettings
from commander.fetchpriority import FetchPriority
from commander.metadata import MetaData
from commander.models import Deck
from commander.net import NetworkError
from commander.scryfall import CardRefresh, RefreshStatus
from commander.store import LABELS_FILE, Workspace
from commander.sync import SyncResult
from commander.syncrun import CardState, SyncOptions, SyncServices, run_sync
from conftest import REFRESH_TOKEN, FakeArchidekt, Json, MemoryTokenStore
from test_cli_apply import run

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
