"""Changes to a deck's profile.toml from the app, keeping the owner's comments and layout."""

import codecs
import logging
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import cast

import tomlkit
from tomlkit.exceptions import ParseError
from tomlkit.items import Array, KeyType, SingleKey, Table

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
    folder: Path,
    deck: Deck,
    *,
    adjacent: Sequence[Sequence[str]],
    ignore: Sequence[str],
    tag_search: bool,
    grace: Mapping[str, float] | None = None,
) -> Profile:
    """Set the adjacent commanders ([ideas] commanders), the tag search ([ideas] tag_search), the ignored
    themes ([themes] ignore) and, unless `grace` is None, the grace cards ([grace], ADR 0039).

    A deck without a profile gets the commented template first. Everything else in the file, comments
    included, stays as it was. The result is checked as `read_profile` would before it is written, so a bad
    value raises ProfileError and leaves the file alone.
    """
    path = profile_path(folder)
    source, existing = _editable(path, deck)
    if grace is not None:
        source = _with_grace_cards(source, _named(grace), existing.grace, path)
    doc = _parse(source, path)
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
    write_text_atomic(path, _bom(path) + tomlkit.dumps(doc))
    return profile


def save_finishers(folder: Path, deck: Deck, *, added: Sequence[str], removed: Sequence[str]) -> Profile:
    """Set the cards played as finishers beside the Tagger-tagged ones ([goldfish] finishers) and the tagged
    ones that are not ([goldfish] not_finishers), ADR 0054. Everything else in the file, comments included,
    stays as it was; a deck without a profile gets the commented template first. A bad value raises
    ProfileError and leaves the file alone."""
    path = profile_path(folder)
    source, _ = _editable(path, deck)
    doc = _parse(source, path)
    table = _table(doc, "goldfish")
    table["finishers"] = _array(_once(name.strip() for name in added if name.strip()))
    table["not_finishers"] = _array(_once(name.strip() for name in removed if name.strip()))
    profile = parse_profile(cast("dict[str, object]", doc.unwrap()), str(path))
    write_text_atomic(path, _bom(path) + tomlkit.dumps(doc))
    return profile


def _editable(path: Path, deck: Deck) -> tuple[str, Profile]:
    """The profile's text, or the template for a deck without one, and what it reads as. The file as it
    stands must be a profile before it is edited, so a wrong shape is an error here."""
    text = read_profile_text(path)
    source = profile_template(deck) if text is None else text
    existing = parse_profile(
        migrate_profile(cast("dict[str, object]", _parse(source, path).unwrap()), path), str(path)
    )
    return source, existing


def _bom(path: Path) -> str:
    """The byte order mark the file starts with, which `read_profile_text` drops and a write must put back."""
    try:
        with path.open("rb") as handle:
            return "\ufeff" if handle.read(3) == codecs.BOM_UTF8 else ""
    except OSError:
        return ""


def _parse(source: str, path: Path) -> tomlkit.TOMLDocument:
    try:
        return tomlkit.parse(source)
    except ParseError as error:
        raise ProfileError(f"cannot read {path}: {error}") from error


def _named(grace: Mapping[str, float]) -> dict[str, float]:
    """The grace cards by trimmed name, blank names left out."""
    return {name.strip(): factor for name, factor in grace.items() if name.strip()}


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
        bom = _bom(path)
        added = bom + _checked(_with_grace_table(text), before, path)
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


_GRACE_HEADER = re.compile(
    r"""^[ \t]*\[[ \t]*(?:grace|"grace"|'grace')[ \t]*\][ \t]*(?:#.*)?\r?$""", re.MULTILINE
)


def _with_grace_cards(
    source: str, wanted: Mapping[str, float], current: Mapping[str, float], path: Path
) -> str:
    """The text with the [grace] table holding exactly `wanted`; the text itself when it already does.

    Only the table's own lines are edited, so the comments below its entries, which belong to the next table,
    stay where they are and new cards land above them.
    """
    if wanted == dict(current):
        return source
    if _GRACE_HEADER.search(source) is None:
        if "grace" in tomllib.loads(source):
            raise ProfileError(f"{path}: write grace as a [grace] table to edit it in the app")
        if not wanted:
            return source
        source = _with_grace_table(source)
    eol = "\r\n" if "\r\n" in source else "\n"
    lines = source.splitlines(keepends=True)
    start, end = _grace_lines(lines)
    section = "".join(lines[start:end])
    if not section.endswith("\n"):
        section += eol
    doc = tomlkit.parse(section)
    table = cast("Table", doc["grace"])
    for name in [name for name in table if name not in wanted]:
        del table[name]
    for name, factor in wanted.items():
        value = int(factor) if factor.is_integer() else factor
        if name not in table:
            # Quoted like the template's example, whatever the name looks like.
            table.append(SingleKey(name, KeyType.Basic), value)
        elif float(cast("float", table[name])) != factor:
            table[name] = value
    edited = tomlkit.dumps(doc).replace("\r\n", "\n").replace("\n", eol)
    return "".join(lines[:start]) + edited + "".join(lines[end:])


def _grace_lines(lines: Sequence[str]) -> tuple[int, int]:
    """The line range of the [grace] table: its header to its last entry, and the comments directly below.

    Comments that run on to the next table's header belong to that table, as in `_with_grace_table`.
    """
    start = next(index for index, line in enumerate(lines) if _GRACE_HEADER.match(line.rstrip("\n")))
    end = start + 1
    for index in range(start + 1, len(lines)):
        text = lines[index].strip()
        if text.startswith("["):
            break
        if text and not text.startswith("#"):
            end = index + 1
    below = end
    while below < len(lines) and lines[below].strip().startswith("#"):
        below += 1
    touches_next_table = below < len(lines) and lines[below].strip().startswith("[")
    return start, end if touches_next_table else below


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
