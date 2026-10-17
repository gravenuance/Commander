"""Signing in and out of the app, and syncing from it (ADR 0023)."""

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from commander.carddb import CARD_DB_FILE
from commander.config import read_config
from commander.desk import Desk, DeskSettings
from commander.net import Pacer
from conftest import PASSWORD, REFRESH_TOKEN, FakeArchidekt, Json, MemoryTokenStore
from test_desk import NOW

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
        "sync": {"running": False, "progress": "", "summary": None, "failed": False, "warnings": []},
    }
    answer = desk.sign_in(" MAKKIS ", PASSWORD)
    assert (answer["user"], answer["signedIn"]) == ("MAKKIS", True)
    folder = env.documents / "Commander" / "decks-MAKKIS"
    config = read_config(env.config)
    assert (config.user, config.workspace) == ("MAKKIS", folder)
    assert env.tokens.get("makkis") == REFRESH_TOKEN
    status = sync_of(answer)
    assert status["running"] is False
    assert str(status["summary"]).startswith("Updated 1 deck from Archidekt, 1 changed")
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


def test_syncing_needs_an_account(env: Env) -> None:
    assert "no Archidekt username" in str(env.desk().sync()["error"])


def test_no_second_sync_and_no_account_change_while_one_runs(env: Env) -> None:
    desk = env.desk(inline=False)
    answer = desk.sign_in("MAKKIS", PASSWORD)
    assert sync_of(answer)["running"] is True
    desk.sync()
    assert len(env.pending) == 1
    assert desk.sign_in("MAKKIS", PASSWORD) == {"error": "Wait for the update to finish, then sign in."}
    assert desk.sign_out() == {"error": "Wait for the update to finish, then sign out."}
    env.pending.pop()()
    assert sync_of(desk.account())["running"] is False
