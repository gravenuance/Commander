import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from commander import syncrun
from commander.carddb import CardDataMeta
from commander.config import MetaSettings
from commander.net import NetworkError
from commander.scryfall import CardRefresh, RefreshStatus
from commander.store import LABELS_FILE, Workspace
from commander.sync import SyncResult
from commander.syncrun import CardState, SyncOptions, SyncServices, run_sync
from conftest import REFRESH_TOKEN, FakeArchidekt, Json, MemoryTokenStore

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
