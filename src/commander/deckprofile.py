"""Deck profiles: what one deck wants from suggestions, kept by hand as profile.toml (ADR 0005)."""

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from commander.carddb import CardDb, Role, Theme
from commander.config import ConfigError, parse_targets
from commander.fsutil import write_text_atomic
from commander.models import Deck

PROFILE_FILE = "profile.toml"
PROFILE_VERSION = 1
MAX_PROFILE_BYTES = 64 * 1024
MAX_LIST = 100
MAX_TEXT = 200
MAX_CARD_EUR = 100_000
_TOP_KEYS = frozenset(
    {"version", "archetype", "bracket_ceiling", "max_card_eur", "targets", "themes", "exclude", "keep"}
)
_THEME_KEYS = frozenset({"types", "keywords", "tags"})
_THEMES_KEYS = _THEME_KEYS | {"ignore"}
_EXCLUDE_KEYS = _THEME_KEYS | {"cards"}
_KEEP_KEYS = frozenset({"cards", "categories"})


class ProfileError(ConfigError):
    """A profile.toml is unreadable or invalid; the message names the file and the key."""


@dataclass(slots=True, frozen=True)
class ThemeSpec:
    """A theme as written in the profile; tags are Tagger slugs, resolved to ids against the card data."""

    types: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.types or self.keywords or self.tags)


@dataclass(slots=True, frozen=True)
class Profile:
    archetype: str = ""
    # Ideas that would lift the deck above this bracket are left out; None leaves them in, marked.
    bracket_ceiling: int | None = None
    max_card_cents: int | None = None
    targets: Mapping[Role, int] = field(default_factory=dict[Role, int])
    # Ideas matching a theme come first; deck cards matching one come last among cuts.
    themes: ThemeSpec = ThemeSpec()
    # Cards matching these, or named here, are never suggested.
    exclude: ThemeSpec = ThemeSpec()
    exclude_cards: tuple[str, ...] = ()
    keep_cards: tuple[str, ...] = ()
    # Cards filed under one of these Archidekt categories are never offered as cuts.
    keep_categories: tuple[str, ...] = ()
    # Themes read from the deck's Archidekt deck tags or categories, by name, to leave out.
    ignore_themes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.bracket_ceiling is not None and not 1 <= self.bracket_ceiling <= 5:  # noqa: PLR2004 — brackets 1..5
            raise ValueError(f"bracket_ceiling must be 1-5, got {self.bracket_ceiling}")
        if self.max_card_cents is not None and self.max_card_cents < 0:
            raise ValueError(f"max_card_cents must not be negative, got {self.max_card_cents}")


@dataclass(slots=True, frozen=True)
class ResolvedThemes:
    """A profile's themes and exclusions against the card data, with the tag slugs it does not know."""

    themes: Theme
    exclude: Theme
    unknown_tags: tuple[str, ...]


def profile_path(folder: Path) -> Path:
    return folder / PROFILE_FILE


def read_profile(folder: Path) -> Profile:
    """The deck's profile, or an empty one when the folder has none."""
    path = profile_path(folder)
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_PROFILE_BYTES + 1)
    except FileNotFoundError:
        return Profile()
    except OSError as error:
        raise ProfileError(f"cannot read {path}: {error}") from error
    if len(data) > MAX_PROFILE_BYTES:
        raise ProfileError(f"{path} is over {MAX_PROFILE_BYTES:,} bytes, too big for a profile")
    try:
        raw = tomllib.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ProfileError(f"cannot read {path}: {error}") from error
    return parse_profile(_migrate(raw, path), str(path))


def parse_profile(raw: Mapping[str, object], where: str) -> Profile:
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ProfileError(f"{where}: unknown key {unknown[0]!r}; use {', '.join(sorted(_TOP_KEYS))}")
    themes = _table(raw, "themes", _THEMES_KEYS, where)
    exclude = _table(raw, "exclude", _EXCLUDE_KEYS, where)
    keep = _table(raw, "keep", _KEEP_KEYS, where)
    return Profile(
        archetype=_text(raw.get("archetype"), "archetype", where) or "",
        bracket_ceiling=_whole(raw.get("bracket_ceiling"), "bracket_ceiling", where, 1, 5),
        max_card_cents=_euro_cents(raw.get("max_card_eur"), where),
        targets=_targets(raw.get("targets"), where),
        themes=_theme(themes, "themes", where),
        exclude=_theme(exclude, "exclude", where),
        exclude_cards=_strings(exclude, "cards", "exclude", where),
        keep_cards=_strings(keep, "cards", "keep", where),
        keep_categories=_strings(keep, "categories", "keep", where),
        ignore_themes=_strings(themes, "ignore", "themes", where),
    )


def resolve_themes(profile: Profile, db: CardDb) -> ResolvedThemes:
    slugs = {*profile.themes.tags, *profile.exclude.tags}
    ids = db.tag_ids(slugs)
    unknown = tuple(sorted(slug for slug in slugs if slug not in ids))
    return ResolvedThemes(_resolve(profile.themes, ids), _resolve(profile.exclude, ids), unknown)


def write_profile_template(folder: Path, deck: Deck) -> Path:
    """Write a commented, empty profile; refuses to replace one that exists."""
    path = profile_path(folder)
    if path.exists():
        raise ProfileError(f"{path} already exists; edit it instead")
    write_text_atomic(path, profile_template(deck))
    return path


def profile_template(deck: Deck) -> str:
    commanders = " and ".join(deck.commanders) or "no commander"
    return f"""\
# What {deck.name} ({commanders}) wants from `cmdr suggest`. Every key is optional.
version = {PROFILE_VERSION}

# What the deck is for, in a few words.
archetype = ""

# Leave out ideas that would lift the deck above this bracket (1-5).
# bracket_ceiling = {deck.bracket or 3}

# Leave out cards dearer than this, in EUR, and cards with no price.
# max_card_eur = 20

# Cards per role, overriding the defaults; 0 when the deck needs none, such as draw from the commander.
[targets]
# draw = 0

# Ideas matching a theme come first, and the deck's matching cards come last among cuts.
# types: type-line words like "Dinosaur"; keywords like "Discover"; tags: `cmdr tags <text>` finds them.
# Themes are read from the deck's Archidekt deck tags and categories too (`cmdr profile` lists them);
# ignore: the deck tags or categories, by name, whose themes to leave out.
[themes]
types = []
keywords = []
tags = []
ignore = []

# Never suggest cards matching these, or named here.
[exclude]
types = []
keywords = []
tags = []
cards = []

# Never offer these as cuts: cards by name, or every card under these Archidekt categories.
[keep]
cards = []
categories = []
"""


def _targets(table: object, where: str) -> dict[Role, int]:
    try:
        return parse_targets(table, where)
    except ConfigError as error:
        raise ProfileError(str(error)) from None


def _resolve(spec: ThemeSpec, ids: Mapping[str, str]) -> Theme:
    return Theme(spec.types, spec.keywords, tuple(ids[slug] for slug in spec.tags if slug in ids))


def _migrate(raw: dict[str, object], path: Path) -> dict[str, object]:
    # Migrations from older profile versions go here, on the raw table, before reading fields.
    version = raw.get("version", PROFILE_VERSION)
    if version != PROFILE_VERSION:
        raise ProfileError(f"{path}: profile version {version} is not supported (expected {PROFILE_VERSION})")
    return raw


def _table(raw: Mapping[str, object], key: str, keys: frozenset[str], where: str) -> Mapping[str, object]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProfileError(f"{where}: '{key}' must be a table, like [{key}]")
    table = cast("dict[str, object]", value)
    unknown = sorted(set(table) - keys)
    if unknown:
        raise ProfileError(f"{where}: unknown key {unknown[0]!r} in [{key}]; use {', '.join(sorted(keys))}")
    return table


def _theme(table: Mapping[str, object], name: str, where: str) -> ThemeSpec:
    return ThemeSpec(
        _strings(table, "types", name, where),
        _strings(table, "keywords", name, where),
        tuple(slug.casefold() for slug in _strings(table, "tags", name, where)),
    )


def _strings(table: Mapping[str, object], key: str, section: str, where: str) -> tuple[str, ...]:
    value = table.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ProfileError(f'{where}: {section}.{key} must be a list, like {key} = ["..."]')
    items = cast("Sequence[object]", value)
    if len(items) > MAX_LIST:
        raise ProfileError(f"{where}: {section}.{key} holds over {MAX_LIST} entries")
    found: list[str] = []
    for index, item in enumerate(items):
        text = _text(item, f"{section}.{key}[{index}]", where)
        if not text:
            raise ProfileError(f"{where}: {section}.{key}[{index}] is empty")
        found.append(text)
    return tuple(found)


def _text(value: object, key: str, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value.strip()) > MAX_TEXT:
        raise ProfileError(f"{where}: {key} must be text of at most {MAX_TEXT} characters")
    return value.strip()


def _whole(value: object, key: str, where: str, lowest: int, highest: int) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not lowest <= value <= highest:
        raise ProfileError(f"{where}: {key} must be a whole number from {lowest} to {highest}")
    return value


def _euro_cents(value: object, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= MAX_CARD_EUR:
        raise ProfileError(f"{where}: max_card_eur must be an amount in EUR from 0 to {MAX_CARD_EUR:,}")
    return round(value * 100)
