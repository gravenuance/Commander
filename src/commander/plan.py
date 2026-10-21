"""Plan files for `cmdr apply`: which cards to add to and remove from one deck."""

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

MAX_PLAN_BYTES = 1024 * 1024
MAX_CHANGES = 60
MAX_NAME_LENGTH = 200
MAX_QUANTITY = 99
MAX_CATEGORY_LENGTH = 100
_ADD_KEYS = frozenset({"name", "quantity", "set", "category"})
_REMOVE_KEYS = frozenset({"name", "keep", "category"})
_TOP_KEYS = frozenset({"deck", "add", "remove"})

EXAMPLE = """\
deck = "magda"   # deck id, or part of its name as in other commands
add = ["Arcane Signet", {name = "Sol Ring", set = "c21", category = "Ramp"}]
remove = ["Wild Growth", {name = "Mountain", keep = 10}]"""


class PlanError(Exception):
    """The plan file is unreadable or asks for something it cannot; the message names the spot."""


# Both lists say how the deck should end up, not what to do, so a plan applied twice changes nothing more.
@dataclass(slots=True, frozen=True)
class AddCard:
    name: str
    # The deck ends with at least this many copies.
    quantity: int = 1
    set_code: str | None = None
    category: str | None = None


@dataclass(slots=True, frozen=True)
class RemoveCard:
    name: str
    # The deck ends with at most this many copies; 0 removes the card.
    keep: int = 0
    # A category outside the deck, such as the Maybeboard: its entries lose the copies instead (ADR 0052).
    category: str | None = None


@dataclass(slots=True, frozen=True)
class Plan:
    deck: str
    add: tuple[AddCard, ...]
    remove: tuple[RemoveCard, ...]


def read_plan(source: Path | BinaryIO) -> Plan:
    """Read a plan from a file or, for `-`, a binary stream such as stdin."""
    where = str(source) if isinstance(source, Path) else "stdin"
    try:
        if isinstance(source, Path):
            with source.open("rb") as handle:
                data = handle.read(MAX_PLAN_BYTES + 1)
        else:
            data = source.read(MAX_PLAN_BYTES + 1)
        if len(data) > MAX_PLAN_BYTES:
            raise PlanError(f"{where} is over {MAX_PLAN_BYTES:,} bytes, too big for a plan")
        raw = tomllib.loads(data.decode("utf-8-sig"))
    except FileNotFoundError:
        raise PlanError(f"no plan file at {where}") from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PlanError(f"cannot read plan {where}: {error}") from error
    try:
        return parse_plan(raw)
    except PlanError as error:
        raise PlanError(f"{where}: {error}") from None


def parse_plan(raw: dict[str, object]) -> Plan:
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise PlanError(f"unknown key {unknown[0]!r}; a plan has deck, add and remove")
    deck = raw.get("deck")
    if isinstance(deck, int) and not isinstance(deck, bool):
        deck = str(deck)
    if not isinstance(deck, str) or not deck.strip():
        raise PlanError("'deck' must name the deck, by id or part of its name")
    add = tuple(_add(item, f"add[{i}]") for i, item in enumerate(_items(raw, "add")))
    remove = tuple(_remove(item, f"remove[{i}]") for i, item in enumerate(_items(raw, "remove")))
    if not add and not remove:
        raise PlanError("the plan has nothing to add or remove")
    if len(add) + len(remove) > MAX_CHANGES:
        raise PlanError(f"{len(add) + len(remove)} changes; a plan holds at most {MAX_CHANGES}")
    _check_unique([c.name for c in add], "add")
    _check_unique([c.name for c in remove if c.category is None], "remove")
    _check_unique([f"{c.name} on {c.category}" for c in remove if c.category is not None], "remove")
    # Taking a card off the Maybeboard while adding it to the deck is no contradiction.
    both = {c.name.casefold() for c in add} & {c.name.casefold() for c in remove if c.category is None}
    if both:
        raise PlanError(f"{min(both)!r} is in both add and remove")
    return Plan(deck.strip(), add, remove)


def _items(raw: dict[str, object], key: str) -> Sequence[object]:
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise PlanError(f"'{key}' must be a list, like {key} = [\"Sol Ring\"]")
    return cast("list[object]", value)


def _add(item: object, where: str) -> AddCard:
    if isinstance(item, str):
        return AddCard(_name(item, where))
    table = _table(item, where, _ADD_KEYS)
    set_code = _optional_text(table, "set", where, 10)
    return AddCard(
        name=_name(table.get("name"), where),
        quantity=_whole(table, "quantity", where, 1) or 1,
        set_code=set_code.casefold() if set_code else None,
        category=_optional_text(table, "category", where, MAX_CATEGORY_LENGTH),
    )


def _remove(item: object, where: str) -> RemoveCard:
    if isinstance(item, str):
        return RemoveCard(_name(item, where))
    table = _table(item, where, _REMOVE_KEYS)
    return RemoveCard(
        _name(table.get("name"), where),
        _whole(table, "keep", where, 0) or 0,
        _optional_text(table, "category", where, MAX_CATEGORY_LENGTH),
    )


def _table(item: object, where: str, keys: frozenset[str]) -> dict[str, object]:
    if not isinstance(item, dict):
        raise PlanError(f'{where} must be a card name or a table like {{name = "Sol Ring"}}')
    table = cast("dict[str, object]", item)
    unknown = sorted(set(table) - keys)
    if unknown:
        raise PlanError(f"{where}: unknown key {unknown[0]!r}; use {', '.join(sorted(keys))}")
    return table


def _name(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{where}: the card name is missing")
    name = value.strip()
    if len(name) > MAX_NAME_LENGTH:
        raise PlanError(f"{where}: the card name is longer than {MAX_NAME_LENGTH} characters")
    return name


def _whole(table: dict[str, object], key: str, where: str, lowest: int) -> int | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not lowest <= value <= MAX_QUANTITY:
        raise PlanError(f"{where}: {key} must be a whole number from {lowest} to {MAX_QUANTITY}")
    return value


def _optional_text(table: dict[str, object], key: str, where: str, limit: int) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise PlanError(f"{where}: {key} must be text of 1-{limit} characters")
    return value.strip()


def _check_unique(names: Sequence[str], key: str) -> None:
    seen: set[str] = set()
    for name in names:
        if name.casefold() in seen:
            raise PlanError(f"{name!r} is listed twice in {key}")
        seen.add(name.casefold())
