"""Deck profiles: what one deck wants from suggestions, kept by hand as profile.toml (ADR 0005)."""

import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from commander.carddb import CardDb, Role, Theme, name_keys
from commander.config import ConfigError, parse_targets
from commander.fsutil import write_text_atomic
from commander.models import Deck

PROFILE_FILE = "profile.toml"
# The name ideas give the themes a profile.toml names itself.
PROFILE_THEME = "profile"
PROFILE_VERSION = 1
MAX_PROFILE_BYTES = 64 * 1024
MAX_LIST = 100
MAX_TEXT = 200
MAX_CARD_EUR = 100_000
# Each adjacent commander costs an Archidekt search and a few deck reads per day (ADR 0015).
MAX_ADJACENT = 8
# A commander and its partner.
MAX_GROUP = 2
_TOP_KEYS = frozenset(
    {
        "version",
        "archetype",
        "bracket_ceiling",
        "max_card_eur",
        "targets",
        "themes",
        "exclude",
        "keep",
        "key_cards",
        "ideas",
    }
)
_THEME_KEYS = frozenset({"types", "keywords", "tags"})
_THEMES_KEYS = _THEME_KEYS | {"ignore"}
_EXCLUDE_KEYS = _THEME_KEYS | {"cards"}
_KEEP_KEYS = frozenset({"cards", "categories"})
_IDEAS_KEYS = frozenset({"commanders"})
_KEY_CARDS_KEYS = frozenset({"ignore"})


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
    # Cards found as key cards (ADR 0017) that are not, by name.
    ignore_keys: tuple[str, ...] = ()
    # Other commanders, alone or with a partner, whose recent Archidekt decks also give ideas (ADR 0015).
    adjacent: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        if self.bracket_ceiling is not None and not 1 <= self.bracket_ceiling <= 5:  # noqa: PLR2004 — brackets 1..5
            raise ValueError(f"bracket_ceiling must be 1-5, got {self.bracket_ceiling}")
        if self.max_card_cents is not None and self.max_card_cents < 0:
            raise ValueError(f"max_card_cents must not be negative, got {self.max_card_cents}")

    def keeps(self, name: str, categories: Iterable[str] = ()) -> bool:
        """Whether the profile keeps a deck card, by its name (either face) or one of its categories."""
        if {c.casefold() for c in categories} & {c.casefold() for c in self.keep_categories}:
            return True
        keys = name_keys(name)
        return any(keys & name_keys(kept) for kept in self.keep_cards)


@dataclass(slots=True, frozen=True)
class ResolvedThemes:
    """A profile's themes and exclusions against the card data, with the tag slugs it does not know."""

    themes: Theme
    exclude: Theme
    unknown_tags: tuple[str, ...]
    # Each theme by the name ideas show it under: "profile" for the profile's own, a deck tag or category.
    labelled: tuple[tuple[str, Theme], ...] = ()


def profile_path(folder: Path) -> Path:
    return folder / PROFILE_FILE


def read_profile(folder: Path) -> Profile:
    """The deck's profile, or an empty one when the folder has none."""
    path = profile_path(folder)
    text = read_profile_text(path)
    if text is None:
        return Profile()
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ProfileError(f"cannot read {path}: {error}") from error
    return parse_profile(migrate_profile(raw, path), str(path))


def read_profile_text(path: Path) -> str | None:
    """The profile file's text, None when there is no file; one too big or not UTF-8 is a ProfileError."""
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_PROFILE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ProfileError(f"cannot read {path}: {error}") from error
    if len(data) > MAX_PROFILE_BYTES:
        raise ProfileError(f"{path} is over {MAX_PROFILE_BYTES:,} bytes, too big for a profile")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ProfileError(f"cannot read {path}: {error}") from error


def parse_profile(raw: Mapping[str, object], where: str) -> Profile:
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ProfileError(f"{where}: unknown key {unknown[0]!r}; use {', '.join(sorted(_TOP_KEYS))}")
    themes = _table(raw, "themes", _THEMES_KEYS, where)
    exclude = _table(raw, "exclude", _EXCLUDE_KEYS, where)
    keep = _table(raw, "keep", _KEEP_KEYS, where)
    ideas = _table(raw, "ideas", _IDEAS_KEYS, where)
    key_cards = _table(raw, "key_cards", _KEY_CARDS_KEYS, where)
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
        ignore_keys=_strings(key_cards, "ignore", "key_cards", where),
        adjacent=_commander_groups(ideas.get("commanders"), where),
    )


def resolve_themes(profile: Profile, db: CardDb) -> ResolvedThemes:
    slugs = {*profile.themes.tags, *profile.exclude.tags}
    ids = db.tag_ids(slugs)
    unknown = tuple(sorted(slug for slug in slugs if slug not in ids))
    themes = resolve_spec(profile.themes, ids)
    labelled = () if themes.empty else ((PROFILE_THEME, themes),)
    return ResolvedThemes(themes, resolve_spec(profile.exclude, ids), unknown, labelled)


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

# Key cards, which `cmdr meta` checks the meta's answers against, are found each time (`cmdr meta` says
# why); the cards kept above are key too. ignore: cards by name that are found but are not key.
[key_cards]
ignore = []

# Commanders that play like this one: their recent Archidekt decks at this deck's bracket give `sync`
# ideas too. A partner pair goes in its own list:
# commanders = ["The Gitrog Monster", ["Tevesh Szat, Doom of Fools", "Kodama of the East Tree"]]
[ideas]
commanders = []
"""


def _targets(table: object, where: str) -> dict[Role, int]:
    try:
        return parse_targets(table, where)
    except ConfigError as error:
        raise ProfileError(str(error)) from None


def resolve_spec(spec: ThemeSpec, ids: Mapping[str, str]) -> Theme:
    """The spec against the card data: tag slugs become ids, and slugs it lacks are left out."""
    return Theme(spec.types, spec.keywords, tuple(ids[slug] for slug in spec.tags if slug in ids))


def migrate_profile(raw: dict[str, object], path: Path) -> dict[str, object]:
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


def _commander_groups(value: object, where: str) -> tuple[tuple[str, ...], ...]:
    key = "ideas.commanders"
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ProfileError(f'{where}: {key} must be a list, like commanders = ["..."]')
    items = cast("Sequence[object]", value)
    if len(items) > MAX_ADJACENT:
        raise ProfileError(f"{where}: {key} holds over {MAX_ADJACENT} commanders")
    groups: list[tuple[str, ...]] = []
    for index, item in enumerate(items):
        if isinstance(item, list):
            pair = cast("list[object]", item)
            group = _strings({"pair": pair}, "pair", f"{key}[{index}]", where)
        else:
            text = _text(item, f"{key}[{index}]", where)
            group = (text,) if text else ()
        if not 1 <= len(group) <= MAX_GROUP:
            raise ProfileError(f"{where}: {key}[{index}] must be a commander or a list of two partners")
        groups.append(group)
    return tuple(groups)


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
