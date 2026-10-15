from pathlib import Path

import pytest

from commander.carddb import Role
from commander.config import (
    ConfigError,
    FileConfig,
    config_path,
    read_config,
    resolve_user,
    resolve_workspace,
    write_config,
)

FILE = FileConfig(user="file-user", workspace=Path("from-file"))
NO_FILE = FileConfig(None, None)


def test_flag_beats_environment_beats_file() -> None:
    env = {"CMDR_USER": "env-user", "CMDR_WORKSPACE": "from-env"}
    assert resolve_user("flag-user", env, FILE) == "flag-user"
    assert resolve_user(None, env, FILE) == "env-user"
    assert resolve_user(None, {}, FILE) == "file-user"
    assert resolve_workspace("from-flag", env, FILE) == Path("from-flag")
    assert resolve_workspace(None, env, FILE) == Path("from-env")
    assert resolve_workspace(None, {}, FILE) == Path("from-file")


def test_missing_settings_say_how_to_fix() -> None:
    with pytest.raises(ConfigError, match="pass --user"):
        resolve_user(None, {}, NO_FILE)
    with pytest.raises(ConfigError, match="pass --workspace"):
        resolve_workspace(None, {}, NO_FILE)


@pytest.mark.parametrize("user", ["", "a b", "../x", "x" * 65, "a&b"])
def test_rejects_invalid_usernames(user: str) -> None:
    with pytest.raises(ConfigError):
        resolve_user(user or None, {"CMDR_USER": user} if user else {}, NO_FILE)


def test_config_path_order() -> None:
    env = {"APPDATA": r"C:\Users\x\AppData\Roaming", "CMDR_CONFIG": "env.toml"}
    assert config_path("flag.toml", env) == Path("flag.toml")
    assert config_path(None, env) == Path("env.toml")
    assert config_path(None, {"APPDATA": r"C:\Roaming"}) == Path(r"C:\Roaming") / "Commander" / "config.toml"


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "Commander" / "config.toml"
    workspace = Path(r'D:\Decks "main"\x')
    write_config(path, "MAKKIS", workspace)
    assert read_config(path) == FileConfig("MAKKIS", workspace)


def test_missing_file_is_empty_config(tmp_path: Path) -> None:
    assert read_config(tmp_path / "none.toml") == NO_FILE


def test_broken_file_is_a_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("user = ", encoding="utf-8")
    with pytest.raises(ConfigError, match="cannot read config"):
        read_config(path)


def test_wrong_types_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("version = 1\nuser = 5\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="'user' must be a string"):
        read_config(path)


def test_newer_config_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('version = 2\nuser = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="version 2 is not supported"):
        read_config(path)


def test_targets_are_read_by_role(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('version = 1\nuser = "x"\n\n[targets]\nramp = 12\nboard_wipe = 0\n', encoding="utf-8")
    assert read_config(path).targets == {Role.RAMP: 12, Role.BOARD_WIPE: 0}


def test_no_targets_table_means_no_overrides(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('version = 1\nuser = "x"\n', encoding="utf-8")
    assert read_config(path).targets == {}


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        (
            "[targets]\nlands = 36\n",
            r"unknown role 'lands' in \[targets\]; use ramp, draw, removal, board_wipe",
        ),
        ('[targets]\n"board wipe" = 3\n', "unknown role 'board wipe'"),
        ("[targets]\nextra_turn = 1\n", "unknown role 'extra_turn'"),
        ("[targets]\nramp = -1\n", "targets.ramp must be a whole number from 0 to 99"),
        ("[targets]\nramp = 100\n", "from 0 to 99"),
        ("[targets]\nramp = 9.5\n", "from 0 to 99"),
        ("[targets]\nramp = true\n", "from 0 to 99"),
        ("targets = 10\n", "'targets' must be a table"),
    ],
)
def test_bad_targets_are_rejected(tmp_path: Path, toml: str, message: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text("version = 1\n" + toml, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        read_config(path)


def test_a_byte_order_mark_is_accepted(tmp_path: Path) -> None:
    # Windows PowerShell 5.1 writes one with Set-Content -Encoding utf8.
    path = tmp_path / "config.toml"
    path.write_bytes(b"\xef\xbb\xbf" + b'version = 1\nuser = "MAKKIS"\n')

    assert read_config(path).user == "MAKKIS"
