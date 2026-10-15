from pathlib import Path

import pytest

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
