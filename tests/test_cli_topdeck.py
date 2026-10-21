"""`cmdr topdeck`: saving, checking and forgetting the TopDeck API key."""

import io
from pathlib import Path

import keyring.errors
import pytest

from conftest import TOPDECK_KEY, TOPDECK_SERVICE, FakeArchidekt, MemoryKeyring
from test_cli_apply import Terminal, run

SECRET = (TOPDECK_SERVICE, "api-key")


@pytest.fixture
def archidekt() -> FakeArchidekt:
    return FakeArchidekt()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def empty_keyring(memory_keyring: MemoryKeyring) -> MemoryKeyring:
    memory_keyring.secrets.clear()
    return memory_keyring


def test_the_key_is_asked_for_checked_with_topdeck_and_saved(
    home: Path, archidekt: FakeArchidekt, empty_keyring: MemoryKeyring
) -> None:
    result = run(["topdeck"], home, archidekt, stdin=Terminal(), password=TOPDECK_KEY)
    assert (result.code, result.err) == (0, "")
    assert result.prompts == ["TopDeck API key: "]
    assert result.out == (
        "TopDeck key saved in Windows Credential Manager (TopDeck accepted it)\n"
        "Tournament data from TopDeck.gg, https://topdeck.gg\n"
    )
    assert empty_keyring.secrets == {SECRET: TOPDECK_KEY}
    [body] = archidekt.topdeck.requests
    assert body["participantMin"] == 100_000
    assert TOPDECK_KEY not in result.out


def test_a_key_from_stdin_is_trimmed(
    home: Path, archidekt: FakeArchidekt, empty_keyring: MemoryKeyring
) -> None:
    result = run(
        ["topdeck", "--key-stdin"], home, archidekt, stdin=io.BytesIO(f"  {TOPDECK_KEY}\r\n".encode())
    )
    assert result.code == 0, result.err
    assert result.prompts == []
    assert empty_keyring.secrets == {SECRET: TOPDECK_KEY}


def test_a_key_topdeck_refuses_is_not_saved(
    home: Path, archidekt: FakeArchidekt, empty_keyring: MemoryKeyring
) -> None:
    result = run(["topdeck"], home, archidekt, stdin=Terminal(), password="not-the-key")
    assert result.code == 1
    assert result.err == "cmdr: TopDeck refused the key (HTTP 401); check it in the TopDeck panel\n"
    assert empty_keyring.secrets == {}
    assert "not-the-key" not in result.out + result.err


def test_no_check_saves_without_asking_topdeck(
    home: Path, archidekt: FakeArchidekt, empty_keyring: MemoryKeyring
) -> None:
    result = run(["topdeck", "--no-check"], home, archidekt, stdin=Terminal(), password="any-key")
    assert result.code == 0, result.err
    assert archidekt.topdeck.requests == []
    assert result.out.startswith("TopDeck key saved in Windows Credential Manager\n")
    assert empty_keyring.secrets == {SECRET: "any-key"}


@pytest.mark.parametrize("key", ["", "two words", "kéy"])
def test_a_key_that_could_not_be_sent_is_refused(
    home: Path, archidekt: FakeArchidekt, empty_keyring: MemoryKeyring, key: str
) -> None:
    result = run(["topdeck", "--no-check"], home, archidekt, stdin=Terminal(), password=key)
    assert result.code == 1
    assert result.err.startswith("cmdr: an API key is ")
    assert empty_keyring.secrets == {}


def test_without_a_terminal_the_key_must_come_from_stdin(
    home: Path, archidekt: FakeArchidekt, empty_keyring: MemoryKeyring
) -> None:
    result = run(["topdeck"], home, archidekt)
    assert (result.code, result.err) == (2, "cmdr: no terminal to ask for the key on; pass --key-stdin\n")
    assert empty_keyring.secrets == {}


def test_remove_forgets_the_key(home: Path, archidekt: FakeArchidekt, memory_keyring: MemoryKeyring) -> None:
    first = run(["topdeck", "--remove"], home, archidekt)
    second = run(["topdeck", "--remove"], home, archidekt)
    assert (first.code, first.out) == (0, "removed the saved TopDeck key\n")
    assert (second.code, second.out) == (0, "no TopDeck key saved\n")
    assert memory_keyring.secrets == {}


def test_a_secret_store_that_refuses_is_reported(
    home: Path, archidekt: FakeArchidekt, empty_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    def locked(_service: str, _username: str, _password: str) -> None:
        raise keyring.errors.PasswordSetError("locked")

    monkeypatch.setattr(empty_keyring, "set_password", locked)
    result = run(["topdeck", "--no-check"], home, archidekt, stdin=Terminal(), password="a-key")
    assert result.code == 1
    assert result.err.startswith("cmdr: could not save the TopDeck key: ")


def test_the_help_names_where_the_free_key_is_issued(
    home: Path, archidekt: FakeArchidekt, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["topdeck", "--help"], home, archidekt).code == 0
    assert "https://topdeck.gg/developers" in capsys.readouterr().out
