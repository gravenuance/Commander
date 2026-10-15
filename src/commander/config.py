"""Settings resolved from flags, then environment, then the config file."""

import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

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


class ConfigError(Exception):
    """Settings are missing or invalid; the message says how to fix it."""


@dataclass(slots=True, frozen=True)
class FileConfig:
    user: str | None
    workspace: Path | None
    # How many cards of a role a deck should have, overriding suggest's defaults.
    targets: Mapping[Role, int] = field(default_factory=dict[Role, int])


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
    raw = _migrate(raw, path)
    user = raw.get("user")
    workspace = raw.get("workspace")
    if user is not None and not isinstance(user, str):
        raise ConfigError(f"{path}: 'user' must be a string")
    if workspace is not None and not isinstance(workspace, str):
        raise ConfigError(f"{path}: 'workspace' must be a string")
    return FileConfig(user, Path(workspace) if workspace else None, _targets(raw.get("targets"), path))


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


def _targets(table: object, path: Path) -> dict[Role, int]:
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


def _toml_string(value: str) -> str:
    # A JSON string literal is also a valid TOML basic string, escapes included.
    return json.dumps(value, ensure_ascii=False)
