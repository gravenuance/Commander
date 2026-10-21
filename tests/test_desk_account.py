"""Signing in and out of the app, and syncing from it (ADR 0023)."""

import logging
import shutil
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from commander import desk as desk_module
from commander.carddb import CARD_DB_FILE
from commander.config import read_config
from commander.deckcache import Clock
from commander.desk import Desk, DeskSettings, sync_summary
from commander.metadata import MetaData
from commander.net import Pacer
from commander.sync import SyncResult, SyncStatus
from commander.syncrun import CardState, CardStatus, SyncOutcome
from commander.weightedshare import ShareTable
from conftest import PASSWORD, REFRESH_TOKEN, FakeArchidekt, Json, MemoryTokenStore
from test_desk import NOW, probing

if TYPE_CHECKING:
    from collections.abc import Callable


class Env:
    """A desk with no account yet, a config file under tmp_path, and the fake Archidekt with one deck."""

    def __init__(self, tmp_path: Path, raw_deck: Json, card_db_file: Path) -> None:
        cache = tmp_path / "cache"
        cache.mkdir()
        shutil.copy(card_db_file, cache / CARD_DB_FILE)
        self.fake = FakeArchidekt()
        self.fake.put(raw_deck)
        self.config = tmp_path / "Commander" / "config.toml"
        self.documents = tmp_path / "docs"
        self.tokens = MemoryTokenStore()
        self.pending: list[Callable[[], None]] = []
        self.settings = DeskSettings(
            tmp_path / "no-decks-yet",
            cache / CARD_DB_FILE,
            None,
            config=self.config,
            documents=self.documents,
        )

    def desk(self, *, inline: bool = True) -> Desk:
        # Inline, the sync runs inside sign_in; otherwise it waits in `pending`, as if still running.
        return Desk(
            self.settings,
            client_factory=self.fake.client,
            token_store=self.tokens,
            now=lambda: NOW,
            pacer_factory=lambda _: Pacer(0),
            background=(lambda work: work()) if inline else self.pending.append,
        )


@pytest.fixture
def env(tmp_path: Path, raw_deck: Json, card_db_file: Path) -> Env:
    return Env(tmp_path, raw_deck, card_db_file)


def sync_of(answer: Json) -> Json:
    return cast("Json", answer["sync"])


def test_a_first_sign_in_saves_the_account_gives_it_a_folder_and_syncs(env: Env) -> None:
    desk = env.desk()
    assert desk.account() == {
        "user": None,
        "signedIn": False,
        "sync": {
            "running": False,
            "progress": "",
            "summary": None,
            "failed": False,
            "warnings": [],
            "decks": {},
        },
    }
    answer = desk.sign_in(" MAKKIS ", PASSWORD)
    assert (answer["user"], answer["signedIn"]) == ("MAKKIS", True)
    folder = env.documents / "Commander" / "decks-MAKKIS"
    config = read_config(env.config)
    assert (config.user, config.workspace) == ("MAKKIS", folder)
    assert env.tokens.get("makkis") == REFRESH_TOKEN
    status = sync_of(answer)
    assert status["running"] is False
    assert str(status["summary"]).startswith("Synced 1 deck, 1 changed")
    assert (folder / "10004639-honestly-though" / "deck.json").is_file()
    assert [d["short"] for d in cast("list[Json]", desk.decks()["decks"])] == ["Magda"]


def test_a_refused_login_changes_nothing(env: Env) -> None:
    desk = env.desk()
    answer = desk.sign_in("MAKKIS", "wrong")
    assert "refused the login" in str(answer["error"])
    assert not env.config.exists()
    assert desk.account()["signedIn"] is False


def test_the_password_never_reaches_the_log(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="commander.desk")
    env.desk().sign_in("MAKKIS", PASSWORD)
    assert "bridge call sign_in (arguments withheld)" in caplog.text
    assert PASSWORD not in caplog.text


def test_signing_out_removes_the_login_and_forgets_the_account(env: Env) -> None:
    desk = env.desk()
    desk.sign_in("MAKKIS", PASSWORD)
    answer = desk.sign_out()
    assert (answer["user"], answer["signedIn"]) == (None, False)
    assert env.tokens.get("makkis") is None
    config = read_config(env.config)
    assert config.user is None
    # The folder is remembered for the next sign-in.
    assert config.workspaces == {"makkis": env.documents / "Commander" / "decks-MAKKIS"}


def test_signing_in_again_drops_the_shares_kept_for_the_last_account(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    computed: list[int] = []
    real = desk_module.weighted_shares

    def counting(data: MetaData | None, *, now: Clock, grace: Mapping[str, float]) -> ShareTable:
        computed.append(1)
        return real(data, now=now, grace=grace)

    monkeypatch.setattr(desk_module, "weighted_shares", counting)
    desk = env.desk()
    desk.sign_in("MAKKIS", PASSWORD)
    deck_id = int(cast("list[Json]", desk.decks()["decks"])[0]["id"])

    desk.deck(deck_id)
    desk.deck(deck_id)
    assert len(computed) == 1

    desk.sign_out()
    desk.sign_in("MAKKIS", PASSWORD)
    desk.deck(deck_id)
    assert len(computed) == 2


def test_signing_in_and_out_is_logged_without_the_password(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    desk = env.desk()
    folder = env.documents / "Commander" / "decks-MAKKIS"
    with caplog.at_level(logging.INFO, logger="commander.desk"):
        desk.sign_in("MAKKIS", PASSWORD)
        desk.sign_out()
    assert f"signed in as MAKKIS; decks in {folder}" in caplog.text
    assert f"signed out MAKKIS; decks stay in {folder}" in caplog.text
    assert PASSWORD not in caplog.text
    assert REFRESH_TOKEN not in caplog.text


def test_the_first_sync_after_signing_in_says_who_started_it(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="commander.desk"):
        env.desk(inline=False).sign_in("MAKKIS", PASSWORD)
    assert "sync started for MAKKIS (first sign-in)" in caplog.messages


def test_syncing_needs_an_account(env: Env) -> None:
    assert env.desk().sync() == {"error": "Sign in to Archidekt first."}


def test_no_second_sync_and_no_account_change_while_one_runs(env: Env) -> None:
    desk = env.desk(inline=False)
    answer = desk.sign_in("MAKKIS", PASSWORD)
    assert sync_of(answer)["running"] is True
    assert sync_of(answer)["progress"] == "Starting the sync"
    desk.sync()
    assert len(env.pending) == 1
    assert desk.sign_in("MAKKIS", PASSWORD) == {
        "error": "Wait for the sync to finish, then sign in (now: Starting the sync)."
    }
    assert desk.sign_out() == {
        "error": "Wait for the sync to finish, then sign out (now: Starting the sync)."
    }
    env.pending.pop()()
    assert sync_of(desk.account())["running"] is False


def test_a_sync_asked_for_while_signing_in_or_out_is_refused_and_the_first_sign_in_still_syncs(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.settings = replace(env.settings, user="MAKKIS")
    desk = env.desk(inline=False)
    during: list[Json] = []
    monkeypatch.setattr(
        desk_module, "switch_account", probing(desk_module.switch_account, lambda: during.append(desk.sync()))
    )
    monkeypatch.setattr(
        desk_module, "forget_account", probing(desk_module.forget_account, lambda: during.append(desk.sync()))
    )
    assert sync_of(desk.sign_in("MAKKIS", PASSWORD))["running"] is True
    env.pending.pop()()
    desk.sign_out()
    assert during == [
        {"error": "Wait for the sign-in to finish, then sync."},
        {"error": "Wait for the sign-out to finish, then sync."},
    ]
    assert env.pending == []


def _outcome(statuses: list[SyncStatus], cards: CardStatus) -> SyncOutcome:
    results = [SyncResult(i, f"Deck {i}", status) for i, status in enumerate(statuses)]
    return SyncOutcome(results, cards, None)


SKIPPED = CardStatus(CardState.SKIPPED, "")


def test_the_summary_counts_decks_in_the_singular_and_the_plural() -> None:
    assert sync_summary(_outcome([SyncStatus.UPDATED], SKIPPED)) == "Synced 1 deck, 1 changed."
    assert (
        sync_summary(_outcome([SyncStatus.UPDATED, SyncStatus.UNCHANGED], SKIPPED))
        == "Synced 2 decks, 1 changed."
    )
    removed = sync_summary(_outcome([SyncStatus.UPDATED, SyncStatus.REMOVED], SKIPPED))
    assert removed == "Synced 1 deck, 1 changed, 1 removed."
    two_removed = sync_summary(_outcome([SyncStatus.REMOVED, SyncStatus.REMOVED], SKIPPED))
    assert two_removed == "Synced 0 decks, 0 changed, 2 removed."
    one = sync_summary(_outcome([SyncStatus.FAILED, SyncStatus.UNCHANGED], SKIPPED))
    assert one == "Sync finished with problems: 1 deck failed; see the log."
    two = sync_summary(_outcome([SyncStatus.FAILED, SyncStatus.FAILED], SKIPPED))
    assert two == "Sync finished with problems: 2 decks failed; see the log."


def test_the_summary_names_a_card_data_failure() -> None:
    failed = CardStatus(CardState.FAILED, "Scryfall is down; the previous card data is kept")
    card_only = sync_summary(_outcome([SyncStatus.UNCHANGED], failed))
    assert card_only == (
        "Sync finished with problems: card data failed (Scryfall is down; the previous card data is kept);"
        " see the log."
    )
    both = sync_summary(_outcome([SyncStatus.FAILED], failed))
    assert both.startswith("Sync finished with problems: 1 deck failed, card data failed (")


def test_a_deck_the_app_does_not_have_says_to_press_the_update_button(env: Env) -> None:
    answer = env.desk().deck(1)
    assert answer == {"error": f"deck 1 is not in {env.settings.workspace}; press Sync"}
