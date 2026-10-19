"""Changes to a deck's profile.toml from the app, keeping the owner's comments and layout."""

import logging
import re
import tomllib
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import cast

import tomlkit
from tomlkit.exceptions import ParseError
from tomlkit.items import Array, Table

from commander.deckprofile import (
    GRACE_BLOCK,
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

log = logging.getLogger(__name__)


def save_settings(
    folder: Path, deck: Deck, *, adjacent: Sequence[Sequence[str]], ignore: Sequence[str], tag_search: bool
) -> Profile:
    """Set the adjacent commanders ([ideas] commanders), the tag search ([ideas] tag_search) and the ignored
    themes ([themes] ignore).

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
    _table(doc, "ideas")["tag_search"] = tag_search
    _table(doc, "themes")["ignore"] = _array(_once(name.strip() for name in ignore if name.strip()))
    profile = parse_profile(cast("dict[str, object]", doc.unwrap()), str(path))
    write_text_atomic(path, tomlkit.dumps(doc))
    return profile


def ensure_grace_table(folder: Path) -> bool:
    """Add the empty [grace] table (ADR 0039) to the folder's profile.toml when it has none; whether it did.

    The table goes before [ideas], and the comments written directly above that table, or at the end. Every
    other byte stays as it was, line ends included. A missing profile is not created, and one that does not
    parse is logged and left alone.
    """
    path = profile_path(folder)
    try:
        text = read_profile_text(path)
        if text is None:
            return False
        before = parse_profile(migrate_profile(tomllib.loads(text), path), str(path))
        if "grace" in tomllib.loads(text):
            return False
        # The file as it is, with any byte order mark the check above dropped.
        original = path.read_bytes().decode("utf-8")
        added = _checked(_with_grace_table(original), before, path)
    except (ProfileError, tomllib.TOMLDecodeError) as error:
        log.warning("%s left as it is, without a [grace] table: %s", path, error)
        return False
    write_text_atomic(path, added)
    return True


def _checked(added: str, before: Profile, path: Path) -> str:
    """The text with the table added, once it is seen to read as the same profile."""
    if parse_profile(migrate_profile(tomllib.loads(added), path), str(path)) != before:
        raise ProfileError(f"{path}: adding the [grace] table would change the profile")
    return added


_IDEAS_HEADER = re.compile(r"^[ \t]*\[[ \t]*ideas[ \t]*\][ \t]*(?:#.*)?\r?$", re.MULTILINE)


def _with_grace_table(text: str) -> str:
    eol = "\r\n" if "\r\n" in text else "\n"
    block = GRACE_BLOCK.replace("\n", eol)
    header = _IDEAS_HEADER.search(text)
    if header is None:
        tail = text if text.endswith("\n") or not text else text + eol
        return tail + (eol if tail.strip() else "") + block
    start = header.start()
    # The comments directly above the table belong to it, so the new table goes above them.
    while start > 0:
        previous = text.rfind("\n", 0, start - 1) + 1
        if not text[previous:start].lstrip().startswith("#"):
            break
        start = previous
    gap = "" if start == 0 or text[:start].endswith(eol + eol) else eol
    return text[:start] + gap + block + eol + text[start:]


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
