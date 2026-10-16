from pathlib import Path

import pytest

from commander.carddb import Role
from commander.config import (
    ConfigError,
    FileConfig,
    MetaSettings,
    config_path,
    forget_account,
    read_config,
    resolve_user,
    resolve_workspace,
    switch_account,
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


def test_meta_settings_default_and_read_from_the_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("version = 1\n[meta]\nwindow_days = 30\npeer_decks = 0\n", encoding="utf-8")
    assert read_config(path).meta == MetaSettings(window_days=30, peer_decks=0)
    assert MetaSettings() == MetaSettings(60, 16, 10, 10, 30)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("meta = 3", "'meta' must be a table"),
        ("[meta]\nwindow = 30", r"unknown key 'window' in \[meta\]"),
        ("[meta]\nwindow_days = 91", "meta.window_days must be a whole number from 7 to 90"),
        ("[meta]\nmeta_lists = 0", "meta.meta_lists must be a whole number from 1 to 50"),
        ("[meta]\npeer_decks = true", "meta.peer_decks must be a whole number from 0 to 25"),
    ],
)
def test_bad_meta_settings_are_refused(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f"version = 1\n{body}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        read_config(path)


OWNER = """\
version = 1
user = "MAKKIS"
workspace = "D:/Projects/Commander/decks"

# Wider window for the meta
[meta]
window_days = 90
"""


def test_a_new_account_gets_a_sibling_folder_and_the_rest_of_the_file_stays(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(OWNER, encoding="utf-8")
    folder = switch_account(path, "Friend", documents=tmp_path / "docs")
    assert folder == Path("D:/Projects/Commander/decks-Friend")
    config = read_config(path)
    assert (config.user, config.workspace) == ("Friend", folder)
    assert config.workspaces == {"makkis": Path("D:/Projects/Commander/decks"), "friend": folder}
    text = path.read_text(encoding="utf-8")
    assert "# Wider window for the meta" in text
    assert config.meta.window_days == 90


def test_switching_back_gives_an_account_its_own_folder_again(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(OWNER, encoding="utf-8")
    switch_account(path, "Friend", documents=tmp_path / "docs")
    # Usernames compare without case, as Archidekt's do.
    assert switch_account(path, "makkis", documents=tmp_path / "docs") == Path("D:/Projects/Commander/decks")
    assert read_config(path).user == "makkis"


def test_the_first_account_without_a_config_gets_a_folder_in_documents(tmp_path: Path) -> None:
    path = tmp_path / "Commander" / "config.toml"
    folder = switch_account(path, "Friend", documents=tmp_path / "docs")
    assert folder == tmp_path / "docs" / "Commander" / "decks-Friend"
    assert read_config(path).workspace == folder


def test_signing_out_forgets_the_account_but_not_its_folder(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(OWNER, encoding="utf-8")
    forget_account(path)
    config = read_config(path)
    assert config.user is None
    assert config.workspaces == {"makkis": Path("D:/Projects/Commander/decks")}
    assert switch_account(path, "MAKKIS", documents=tmp_path) == Path("D:/Projects/Commander/decks")


def test_an_invalid_username_leaves_the_config_alone(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(OWNER, encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid Archidekt username"):
        switch_account(path, "no spaces", documents=tmp_path)
    assert path.read_text(encoding="utf-8") == OWNER


def test_a_bad_workspaces_table_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(OWNER + "[workspaces]\nfriend = 3\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"workspaces.friend must be a deck folder"):
        read_config(path)
