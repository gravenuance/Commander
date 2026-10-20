"""Writes beside a running sync: refused while it pulls the decks, let through while it fetches ideas, and
merged per deck so neither loses the other's change (ADR 0042)."""

import logging
import shutil
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest

from commander import desk as desk_module
from commander import syncrun
from commander.board import Actor, BoardCard, Origin, Status, read_board
from commander.carddb import CARD_DB_FILE
from commander.decklocks import DeckLocks
from commander.desk import Desk, DeskSettings
from commander.ideas import compile_ideas
from commander.metadata import MetaData
from commander.net import Pacer
from commander.scryfall import CardRefresh
from commander.store import StoredDeck
from conftest import REFRESH_TOKEN, FakeArchidekt, Json, MemoryTokenStore
from test_cli_apply import run
from test_desk import DECK, DESK_LOG, NOW, first_card_id, logged, probing, reviewed
from test_syncrun import DOWNLOADED, lists_with, stub_meta

# Only a safety net against a hung test: every wait below is released by the other side.
WAIT_SECONDS = 30
FOLDER = "10004639-honestly-though"


@pytest.fixture
def fake(raw_deck: Json) -> FakeArchidekt:
    archidekt = FakeArchidekt()
    archidekt.put(raw_deck)
    return archidekt


@pytest.fixture
def settings(tmp_path: Path, card_db_file: Path, fake: FakeArchidekt) -> DeskSettings:
    cache = tmp_path / "cache"
    cache.mkdir()
    shutil.copy(card_db_file, cache / CARD_DB_FILE)
    workspace = tmp_path / "decks"
    assert run(["sync", "--no-cards", "--no-ideas", "--workspace", str(workspace)], tmp_path, fake).code == 0
    fake.requests.clear()
    return DeskSettings(workspace, cache / CARD_DB_FILE, "MAKKIS", config=tmp_path / "config.toml")


def make_desk(
    settings: DeskSettings, fake: FakeArchidekt, background: Callable[[Callable[[], None]], object]
) -> Desk:
    return Desk(
        settings,
        client_factory=fake.client,
        token_store=MemoryTokenStore({"makkis": REFRESH_TOKEN}),
        now=lambda: NOW,
        pacer_factory=lambda _: Pacer(0),
        background=background,
    )


def no_download(monkeypatch: pytest.MonkeyPatch) -> None:
    def download(*_: object, **__: object) -> CardRefresh:
        return DOWNLOADED

    monkeypatch.setattr(syncrun, "refresh_card_db", download)


def queue_a_save(desk: Desk, card: str = "Arcane Signet") -> str:
    """Queue card for the deck and preview the save; the stamp the save must present."""
    desk.propose(DECK, card)
    desk.decide(DECK, first_card_id(desk), "add", None, "")
    return reviewed(desk)


def board_cards(settings: DeskSettings) -> dict[str, tuple[Actor, Status]]:
    return {card.name: (card.source, card.status) for card in read_board(settings.workspace / FOLDER).cards}


class HeldSync:
    """The desk's sync on a thread of its own, held in the ideas phase's network reads for DECK until
    released, the way a slow Archidekt search holds a real one."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.fetching = threading.Event()
        self.release = threading.Event()
        self.thread: threading.Thread | None = None
        no_download(monkeypatch)
        stub_meta(monkeypatch, self._hold)

    def _hold(self, deck_id: int) -> None:
        if deck_id == DECK:
            self.fetching.set()
            assert self.release.wait(WAIT_SECONDS)

    def start(self, work: Callable[[], None]) -> object:
        self.thread = threading.Thread(target=work, name="test-sync", daemon=True)
        self.thread.start()
        return self.thread

    def finish(self) -> None:
        self.release.set()
        assert self.thread is not None
        self.thread.join(WAIT_SECONDS)
        assert not self.thread.is_alive()


def test_board_edits_and_a_save_go_through_while_the_sync_fetches_ideas(
    settings: DeskSettings, fake: FakeArchidekt, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = HeldSync(monkeypatch)
    desk = make_desk(settings, fake, held.start)
    try:
        desk.sync()
        assert held.fetching.wait(WAIT_SECONDS)
        assert desk.sync_status()["running"] is True
        assert "card" in desk.propose(DECK, "Arcane Signet")
        card_id = first_card_id(desk)
        assert "card" in desk.decide(DECK, card_id, "add", None, "")
        saved = desk.commit(DECK, reviewed(desk))
        assert saved["saved"] is True, saved
        assert fake.card_writes()
        assert desk.sync_status()["running"] is True
    finally:
        held.finish()
    status = desk.sync_status()
    assert (status["running"], status["failed"]) == (False, False)
    assert board_cards(settings)["Arcane Signet"] == (Actor.YOU, Status.APPLIED)


def test_signing_out_still_waits_for_the_whole_sync(
    settings: DeskSettings, fake: FakeArchidekt, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = HeldSync(monkeypatch)
    desk = make_desk(settings, fake, held.start)
    try:
        desk.sync()
        assert held.fetching.wait(WAIT_SECONDS)
        answer = desk.sign_out()
    finally:
        held.finish()
    assert str(answer.get("error", "")).startswith("Wait for the sync to finish, then sign out")


def inline_sync(desk: Desk, pending: list[Callable[[], None]]) -> None:
    desk.sync()
    [start] = pending
    start()
    assert desk.sync_status()["failed"] is False


def ideas_from(monkeypatch: pytest.MonkeyPatch, data: MetaData, during: Callable[[], None]) -> None:
    """The ideas phase reads `data` for each deck, running `during` as it reads; the merge is the real one."""
    stub_meta(monkeypatch)
    no_download(monkeypatch)

    def gather(*_: object, **__: object) -> MetaData:
        during()
        return data

    monkeypatch.setattr(syncrun, "gather_for", gather)
    monkeypatch.setattr(syncrun, "compile_ideas", compile_ideas)


def test_the_ideas_merge_keeps_a_card_the_user_proposed_while_the_ideas_were_fetched(
    settings: DeskSettings, fake: FakeArchidekt, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending: list[Callable[[], None]] = []
    desk = make_desk(settings, fake, pending.append)
    answers: list[Json] = []
    ideas_from(
        monkeypatch, lists_with("Chaos Warp", 9), lambda: answers.append(desk.propose(DECK, "Arcane Signet"))
    )
    inline_sync(desk, pending)
    assert "card" in answers[0], answers
    cards = board_cards(settings)
    assert cards["Arcane Signet"] == (Actor.YOU, Status.OPEN)
    assert cards["Chaos Warp"] == (Actor.CMDR, Status.OPEN)


def test_the_ideas_merge_for_a_deck_saved_while_its_ideas_were_fetched_sees_the_saved_deck(
    settings: DeskSettings, fake: FakeArchidekt, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending: list[Callable[[], None]] = []
    desk = make_desk(settings, fake, pending.append)
    stamp = queue_a_save(desk)
    answers: list[Json] = []
    stub_meta(monkeypatch, lambda _: answers.append(desk.commit(DECK, stamp)))
    no_download(monkeypatch)
    merged_with: list[bool] = []

    def compile_spy(stored: StoredDeck, *_: object, **__: object) -> dict[Origin, list[BoardCard]]:
        merged_with.append(any(card.name == "Arcane Signet" for card in stored.deck.main_cards))
        return {}

    monkeypatch.setattr(syncrun, "compile_ideas", compile_spy)
    inline_sync(desk, pending)
    assert answers[0].get("saved") is True, answers
    assert merged_with == [True]


def propose(desk: Desk) -> Json:
    return desk.propose(DECK, "Arcane Signet")


def commit(desk: Desk) -> Json:
    return desk.commit(DECK, "any")


@pytest.mark.parametrize(
    ("act", "what", "then"), [(propose, "proposal", "propose the card"), (commit, "save", "save")]
)
def test_a_write_while_the_sync_pulls_the_decks_is_still_refused_and_logged(
    settings: DeskSettings,
    fake: FakeArchidekt,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    *,
    act: Callable[[Desk], Json],
    what: str,
    then: str,
) -> None:
    pending: list[Callable[[], None]] = []
    desk = make_desk(settings, fake, pending.append)
    stub_meta(monkeypatch)
    no_download(monkeypatch)
    answers: list[Json] = []
    monkeypatch.setattr(syncrun, "sync_decks", probing(syncrun.sync_decks, lambda: answers.append(act(desk))))
    with caplog.at_level(logging.INFO, logger=DESK_LOG):
        inline_sync(desk, pending)
    assert answers == [{"error": f"Wait for the sync to finish, then {then} (now: Starting the sync)."}]
    assert (
        f"refused the {what} for deck {DECK}: the sync has run for 0s, now at: Starting the sync"
        in logged(caplog)
    )
    assert not fake.card_writes()
    assert "Arcane Signet" not in board_cards(settings)


def test_a_deck_gone_from_the_workspace_while_its_ideas_were_fetched_gets_no_merge_and_a_warning(
    settings: DeskSettings, fake: FakeArchidekt, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending: list[Callable[[], None]] = []
    desk = make_desk(settings, fake, pending.append)
    stub_meta(monkeypatch, lambda _: shutil.rmtree(settings.workspace / FOLDER))
    no_download(monkeypatch)
    merged: list[StoredDeck] = []

    def compile_spy(stored: StoredDeck, *_: object, **__: object) -> dict[Origin, list[BoardCard]]:
        merged.append(stored)
        return {}

    monkeypatch.setattr(syncrun, "compile_ideas", compile_spy)
    inline_sync(desk, pending)
    assert merged == []
    warnings = [str(w) for w in cast("list[object]", desk.sync_status()["warnings"])]
    assert any("ideas not merged: the deck left its folder" in w for w in warnings), warnings


class WatchedLocks(DeckLocks):
    """The desk's deck locks, telling which decks' locks are held at any moment."""

    def __init__(self) -> None:
        super().__init__()
        self.held: list[int] = []

    @contextmanager
    def hold(self, deck_id: int) -> Generator[None]:
        with super().hold(deck_id):
            self.held.append(deck_id)
            try:
                yield
            finally:
                self.held.remove(deck_id)


def test_the_ideas_merge_and_the_users_board_writes_and_save_hold_the_decks_lock(
    settings: DeskSettings, fake: FakeArchidekt, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks = WatchedLocks()
    monkeypatch.setattr(desk_module, "DeckLocks", lambda: locks)
    pending: list[Callable[[], None]] = []
    desk = make_desk(settings, fake, pending.append)
    locked: dict[str, bool] = {}

    def watch(what: str) -> Callable[[], None]:
        return lambda: locked.update({what: DECK in locks.held})

    board_service = desk_module.BoardService
    monkeypatch.setattr(board_service, "propose", probing(board_service.propose, watch("propose")))
    monkeypatch.setattr(desk_module, "save_board", probing(desk_module.save_board, watch("save")))
    stamp = queue_a_save(desk)
    assert desk.commit(DECK, stamp)["saved"] is True
    no_download(monkeypatch)
    stub_meta(monkeypatch)
    monkeypatch.setattr(syncrun, "compile_ideas", probing(syncrun.compile_ideas, watch("merge")))
    inline_sync(desk, pending)
    assert locked == {"propose": True, "save": True, "merge": True}
    assert locks.held == []
