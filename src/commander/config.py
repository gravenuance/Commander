"""Settings resolved from flags, then environment, then the config file."""

import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from commander.fsutil import write_text_atomic

CONFIG_VERSION = 1
ENV_USER = "CMDR_USER"
ENV_WORKSPACE = "CMDR_WORKSPACE"
ENV_CONFIG = "CMDR_CONFIG"
_USERNAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")


class ConfigError(Exception):
    """Settings are missing or invalid; the message says how to fix it."""


@dataclass(slots=True, frozen=True)
class FileConfig:
    user: str | None
    workspace: Path | None


def default_config_path(env: Mapping[str, str]) -> Path:
    appdata = env.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / ".config"
    return base / "Commander" / "config.toml"


def config_path(flag: str | None, env: Mapping[str, str]) -> Path:
    if flag:
        return Path(flag)
    if env.get(ENV_CONFIG):
        return Path(env[ENV_CONFIG])
    return default_config_path(env)


def read_config(path: Path) -> FileConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
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
    return FileConfig(user, Path(workspace) if workspace else None)


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


def _toml_string(value: str) -> str:
    # A JSON string literal is also a valid TOML basic string, escapes included.
    return json.dumps(value, ensure_ascii=False)
