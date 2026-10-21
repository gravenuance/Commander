"""The app's TopDeck panel: whether a key is saved, testing it, saving it and removing it."""

import logging
from pathlib import Path

import pytest

from commander.desk import Desk, DeskSettings
from commander.net import Pacer
from conftest import TOPDECK_KEY, TOPDECK_SERVICE, FakeArchidekt, MemoryKeyring, MemoryTokenStore
from test_desk import NOW

SECRET = (TOPDECK_SERVICE, "api-key")
DEVELOPERS = "https://topdeck.gg/developers"


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.fake = FakeArchidekt()
        cache = tmp_path / "cache"
        cache.mkdir()
        self.desk = Desk(
            DeskSettings(tmp_path / "decks", cache / "cards.sqlite", "TESTER"),
            client_factory=self.fake.client,
            token_store=MemoryTokenStore(),
            now=lambda: NOW,
            pacer_factory=lambda _: Pacer(0),
        )


@pytest.fixture
def env(tmp_path: Path, memory_keyring: MemoryKeyring) -> Env:
    memory_keyring.secrets.clear()
    return Env(tmp_path)


def test_the_panel_knows_whether_a_key_is_saved(env: Env, memory_keyring: MemoryKeyring) -> None:
    assert env.desk.topdeck() == {"hasKey": False, "developers": DEVELOPERS}
    memory_keyring.secrets[SECRET] = TOPDECK_KEY
    assert env.desk.topdeck() == {"hasKey": True, "developers": DEVELOPERS}


def test_a_typed_key_is_tested_without_being_saved(env: Env, memory_keyring: MemoryKeyring) -> None:
    assert env.desk.test_topdeck_key(f"  {TOPDECK_KEY} ") == {"ok": True}
    assert memory_keyring.secrets == {}
    assert env.fake.topdeck.requests == [{"last": 1, "participantMin": 100_000}]


def test_without_a_typed_key_the_saved_one_is_tested(env: Env, memory_keyring: MemoryKeyring) -> None:
    assert env.desk.test_topdeck_key("") == {
        "error": "no TopDeck key saved; add one in the app's TopDeck panel or with `cmdr topdeck`"
    }
    memory_keyring.secrets[SECRET] = TOPDECK_KEY
    assert env.desk.test_topdeck_key("") == {"ok": True}


def test_a_refused_key_says_so_and_is_never_logged(
    env: Env, memory_keyring: MemoryKeyring, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        tested = env.desk.test_topdeck_key("a-wrong-key")
        saved = env.desk.save_topdeck_key("a-wrong-key")
    assert tested == saved == {"error": "TopDeck refused the key (HTTP 401); check it in the TopDeck panel"}
    assert memory_keyring.secrets == {}
    assert "a-wrong-key" not in caplog.text


def test_a_key_is_saved_once_topdeck_accepts_it(env: Env, memory_keyring: MemoryKeyring) -> None:
    answer = env.desk.save_topdeck_key(f" {TOPDECK_KEY}\n")
    assert answer == {"hasKey": True, "developers": DEVELOPERS}
    assert memory_keyring.secrets == {SECRET: TOPDECK_KEY}


def test_a_key_that_could_not_be_sent_is_not_even_tried(env: Env, memory_keyring: MemoryKeyring) -> None:
    answer = env.desk.save_topdeck_key("two words")
    assert answer == {"error": "an API key is one run of plain characters, with no spaces"}
    assert env.fake.topdeck.requests == []
    assert memory_keyring.secrets == {}


def test_a_topdeck_outage_leaves_the_key_unsaved(env: Env, memory_keyring: MemoryKeyring) -> None:
    env.fake.topdeck.status = 500
    answer = env.desk.save_topdeck_key(TOPDECK_KEY)
    assert "500" in str(answer["error"])
    assert memory_keyring.secrets == {}


def test_the_key_can_be_removed(env: Env, memory_keyring: MemoryKeyring) -> None:
    memory_keyring.secrets[SECRET] = TOPDECK_KEY
    assert env.desk.remove_topdeck_key() == {"hasKey": False, "developers": DEVELOPERS}
    assert memory_keyring.secrets == {}
    assert env.desk.remove_topdeck_key() == {"hasKey": False, "developers": DEVELOPERS}
