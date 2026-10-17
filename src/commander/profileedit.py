"""Changes to a deck's profile.toml from the app, keeping the owner's comments and layout."""

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import cast

import tomlkit
from tomlkit.exceptions import ParseError
from tomlkit.items import Array, Table

from commander.deckprofile import (
    Profile,
    ProfileError,
    migrate_profile,
    parse_profile,
    profile_path,
    profile_template,
    read_profile_text,
)
from commander.fsutil import write_text_atomic
from commander.models import Deck


def save_settings(
    folder: Path, deck: Deck, *, adjacent: Sequence[Sequence[str]], ignore: Sequence[str]
) -> Profile:
    """Set the adjacent commanders ([ideas] commanders) and the ignored themes ([themes] ignore).

    A deck without a profile gets the commented template first. Everything else in the file, comments
    included, stays as it was. The result is checked as `read_profile` would before it is written, so a bad
    value raises ProfileError and leaves the file alone.
    """
    path = profile_path(folder)
    text = read_profile_text(path)
    try:
        doc = tomlkit.parse(profile_template(deck) if text is None else text)
    except ParseError as error:
        raise ProfileError(f"cannot read {path}: {error}") from error
    # The file as it stands must be a profile before it is edited, so a wrong shape is an error here.
    existing = migrate_profile(cast("dict[str, object]", doc.unwrap()), path)
    parse_profile(existing, str(path))
    groups: dict[tuple[str, ...], list[str]] = {}
    for group in adjacent:
        names = [name.strip() for name in group if name.strip()]
        groups.setdefault(tuple(sorted(name.casefold() for name in names)), names)
    _table(doc, "ideas")["commanders"] = _array(
        [group[0] if len(group) == 1 else group for group in groups.values() if group]
    )
    _table(doc, "themes")["ignore"] = _array(_once(name.strip() for name in ignore if name.strip()))
    profile = parse_profile(cast("dict[str, object]", doc.unwrap()), str(path))
    write_text_atomic(path, tomlkit.dumps(doc))
    return profile


def _table(doc: tomlkit.TOMLDocument, key: str) -> Table:
    if key not in doc:
        doc.add(tomlkit.nl())
        doc.add(key, tomlkit.table())
    return cast("Table", doc[key])


def _array(items: Sequence[str | list[str]]) -> Array:
    array = tomlkit.item(list(items))
    # One item a line once there are several, as the template's examples read.
    return array.multiline(len(items) > 1)


def _once(names: Iterable[str]) -> list[str]:
    seen: dict[str, str] = {}
    for name in names:
        seen.setdefault(name.casefold(), name)
    return list(seen.values())
