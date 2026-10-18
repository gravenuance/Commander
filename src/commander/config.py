"""Settings resolved from flags, then environment, then the config file."""

import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import tomlkit
from tomlkit.exceptions import ParseError
from tomlkit.items import Table

from commander.analysis import ANALYZED_ROLES
from commander.carddb import Role
from commander.fsutil import write_text_atomic

CONFIG_VERSION = 1
ENV_USER = "CMDR_USER"
ENV_WORKSPACE = "CMDR_WORKSPACE"
ENV_CONFIG = "CMDR_CONFIG"
ENV_CACHE = "CMDR_CACHE_DIR"
_USERNAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")
# TOML keys for the roles a target can be set for: "board wipe" is written board_wipe.
TARGET_KEYS: Mapping[str, Role] = {role.value.replace(" ", "_"): role for role in ANALYZED_ROLES}
MAX_TARGET = 99


@dataclass(slots=True, frozen=True)
class MetaSettings:
    """How `cmdr meta` samples tournament and Archidekt data; set in the config's [meta] table."""

    # Only events and deck updates this many days back count.
    window_days: int = 60
    # Smaller events say little about a list; EDHTop16 itself defaults to 16 players.
    min_event_size: int = 16
    # Most the most-viewed recent Archidekt decks read in full per commander; 0 reads none.
    peer_decks: int = 60
    # Most-played commanders whose lists are searched for answers to the deck's key cards.
    meta_commanders: int = 10
    # Best-placed lists read per meta commander.
    meta_lists: int = 30


# Each [meta] key with its smallest and largest allowed value; the window can't exceed the API's three months.
META_LIMITS: Mapping[str, tuple[int, int]] = {
    "window_days": (7, 90),
    "min_event_size": (0, 1000),
    "peer_decks": (0, 60),
    "meta_commanders": (0, 25),
    "meta_lists": (1, 50),
}


class ConfigError(Exception):
    """Settings are missing or invalid; the message says how to fix it."""


@dataclass(slots=True, frozen=True)
class FileConfig:
    user: str | None
    workspace: Path | None
    # How many cards of a role a deck should have, overriding suggest's defaults.
    targets: Mapping[Role, int] = field(default_factory=dict[Role, int])
    meta: MetaSettings = field(default_factory=MetaSettings)
    # Each account's deck folder, by casefolded username, so switching accounts switches folders.
    workspaces: Mapping[str, Path] = field(default_factory=dict[str, Path])


def default_config_path(env: Mapping[str, str]) -> Path:
    appdata = env.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / ".config"
    return base / "Commander" / "config.toml"


def cache_dir(env: Mapping[str, str]) -> Path:
    if env.get(ENV_CACHE):
        return Path(env[ENV_CACHE])
    local = env.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / ".cache"
    return base / "Commander"


def config_path(flag: str | None, env: Mapping[str, str]) -> Path:
    if flag:
        return Path(flag)
    if env.get(ENV_CONFIG):
        return Path(env[ENV_CONFIG])
    return default_config_path(env)


def read_config(path: Path) -> FileConfig:
    try:
        # utf-8-sig drops the byte-order mark Windows PowerShell 5.1 puts at the start of UTF-8 files.
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return FileConfig(None, None)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read config {path}: {error}") from error
    return _parse(_migrate(raw, path), path)


def _parse(raw: Mapping[str, object], path: Path) -> FileConfig:
    user = raw.get("user")
    workspace = raw.get("workspace")
    if user is not None and not isinstance(user, str):
        raise ConfigError(f"{path}: 'user' must be a string")
    if workspace is not None and not isinstance(workspace, str):
        raise ConfigError(f"{path}: 'workspace' must be a string")
    targets = parse_targets(raw.get("targets"), str(path))
    meta = parse_meta(raw.get("meta"), str(path))
    workspaces = _parse_workspaces(raw.get("workspaces"), path)
    return FileConfig(user, Path(workspace) if workspace else None, targets, meta, workspaces)


def _parse_workspaces(table: object, path: Path) -> dict[str, Path]:
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: 'workspaces' must be a table of usernames and deck folders")
    folders: dict[str, Path] = {}
    for user, folder in cast("dict[str, object]", table).items():
        validate_user(user)
        if not isinstance(folder, str) or not folder:
            raise ConfigError(f"{path}: workspaces.{user} must be a deck folder")
        folders[user.casefold()] = Path(folder)
    return folders


def switch_account(path: Path, user: str, *, documents: Path) -> Path:
    """Make user the config's account and return its deck folder, keeping the rest of the file as it is.

    The account signed in so far keeps its folder under [workspaces]. An account seen before gets its
    folder back; a new one gets a sibling of the current folder named decks-<user>, or one under
    documents/Commander when there is none.
    """
    validate_user(user)
    doc = _edit(path)
    folders = _workspaces_table(doc)
    current = _remember_current(doc, folders)
    known = _text(cast("dict[str, object]", folders.unwrap()), user.casefold())
    if known:
        folder = Path(known)
    elif current:
        folder = Path(current).parent / f"decks-{user}"
    else:
        folder = documents / "Commander" / f"decks-{user}"
    doc["user"] = user
    doc["workspace"] = str(folder)
    folders[user.casefold()] = str(folder)
    _save(path, doc)
    return folder


def forget_account(path: Path) -> None:
    """Sign the config out: no account until the next sign-in, every account's folder still remembered."""
    doc = _edit(path)
    _remember_current(doc, _workspaces_table(doc))
    if "user" in doc:
        del doc["user"]
    _save(path, doc)


def _remember_current(doc: tomlkit.TOMLDocument, folders: Table) -> str | None:
    """Keep the signed-in account's deck folder under [workspaces]; returns that folder."""
    plain = cast("dict[str, object]", doc.unwrap())
    user, current = _text(plain, "user"), _text(plain, "workspace")
    if user and current:
        folders[user.casefold()] = current
    return current


def _text(table: Mapping[str, object], key: str) -> str | None:
    value = table.get(key)
    return value if isinstance(value, str) and value else None


def _edit(path: Path) -> tomlkit.TOMLDocument:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        text = f"version = {CONFIG_VERSION}\n"
    except OSError as error:
        raise ConfigError(f"cannot read config {path}: {error}") from error
    try:
        return tomlkit.parse(text)
    except ParseError as error:
        raise ConfigError(f"cannot read config {path}: {error}") from error


def _workspaces_table(doc: tomlkit.TOMLDocument) -> Table:
    if "workspaces" not in doc:
        doc.add(tomlkit.nl())
        doc.add("workspaces", tomlkit.table())
    table = doc["workspaces"]
    if not isinstance(table, Table):
        raise ConfigError("the config's 'workspaces' must be a table of usernames and deck folders")
    return table


def _save(path: Path, doc: tomlkit.TOMLDocument) -> None:
    # Checked as read_config would read it, so a bad value never reaches the file.
    _parse(_migrate(cast("dict[str, object]", doc.unwrap()), path), path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, tomlkit.dumps(doc))


def write_config(path: Path, user: str, workspace: Path) -> None:
    validate_user(user)
    text = (
        f"version = {CONFIG_VERSION}\n"
        f"user = {_toml_string(user)}\n"
        f"workspace = {_toml_string(str(workspace))}\n"
    )
    write_text_atomic(path, text)


def resolve_user(flag: str | None, env: Mapping[str, str], file: FileConfig) -> str:
    user = flag or env.get(ENV_USER) or file.user
    if not user:
        raise ConfigError(f"no Archidekt username: pass --user, set {ENV_USER}, or run `cmdr init`")
    validate_user(user)
    return user


def resolve_workspace(flag: str | None, env: Mapping[str, str], file: FileConfig) -> Path:
    if flag:
        return Path(flag)
    if env.get(ENV_WORKSPACE):
        return Path(env[ENV_WORKSPACE])
    if file.workspace is not None:
        return file.workspace
    raise ConfigError(f"no deck folder: pass --workspace, set {ENV_WORKSPACE}, or run `cmdr init`")


def validate_user(user: str) -> None:
    if not user or len(user) > 64 or not set(user) <= _USERNAME_CHARS:  # noqa: PLR2004 — generous cap
        raise ConfigError(f"invalid Archidekt username: {user!r}")


def environment() -> Mapping[str, str]:
    return os.environ


def _migrate(raw: dict[str, object], path: Path) -> dict[str, object]:
    # Migrations from older config versions go here, on the raw table, before reading fields.
    version = raw.get("version", CONFIG_VERSION)
    if version != CONFIG_VERSION:
        raise ConfigError(f"{path}: config version {version} is not supported (expected {CONFIG_VERSION})")
    return raw


def parse_targets(table: object, path: str) -> dict[Role, int]:
    """A [targets] table of a config or profile file, checked; path names the file in messages."""
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: 'targets' must be a table, like [targets] with ramp = 10")
    targets: dict[Role, int] = {}
    # tomllib gives str keys; the values are checked below.
    for key, value in cast("dict[str, object]", table).items():
        role = TARGET_KEYS.get(key)
        if role is None:
            raise ConfigError(f"{path}: unknown role {key!r} in [targets]; use {', '.join(TARGET_KEYS)}")
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_TARGET:
            raise ConfigError(f"{path}: targets.{key} must be a whole number from 0 to {MAX_TARGET}")
        targets[role] = value
    return targets


def parse_meta(table: object, path: str) -> MetaSettings:
    """The config's [meta] table, checked; keys left out keep their defaults."""
    if table is None:
        return MetaSettings()
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: 'meta' must be a table, like [meta] with window_days = 60")
    values: dict[str, int] = {}
    for key, value in cast("dict[str, object]", table).items():
        limits = META_LIMITS.get(key)
        if limits is None:
            raise ConfigError(f"{path}: unknown key {key!r} in [meta]; use {', '.join(META_LIMITS)}")
        low, high = limits
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise ConfigError(f"{path}: meta.{key} must be a whole number from {low} to {high}")
        values[key] = value
    return MetaSettings(**values)


def _toml_string(value: str) -> str:
    # A JSON string literal is also a valid TOML basic string, escapes included.
    return json.dumps(value, ensure_ascii=False)
