"""Local card database built from Scryfall bulk data: gameplay facts, EUR prices and card roles."""

import logging
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

from commander.models import DeckCard

log = logging.getLogger(__name__)

# Bump whenever the tables or the meaning of a column change; an old file is rebuilt, never migrated.
SCHEMA_VERSION = 1
CARD_DB_FILE = "cards.sqlite"
_QUERY_CHUNK = 500
_CARD_QUERIES = {
    "oracle_id": "SELECT oracle_id, name, mana_cost, cmc, type_line, color_identity, game_changer,"
    " commander_legal, cheapest_cents FROM oracle WHERE oracle_id IN ({}) ORDER BY oracle_id",
    "name_key": "SELECT oracle_id, name, mana_cost, cmc, type_line, color_identity, game_changer,"
    " commander_legal, cheapest_cents FROM oracle WHERE name_key IN ({}) ORDER BY oracle_id",
}

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE oracle (
    oracle_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    mana_cost TEXT NOT NULL,
    cmc REAL NOT NULL,
    type_line TEXT NOT NULL,
    color_identity TEXT NOT NULL,
    game_changer INTEGER NOT NULL,
    commander_legal INTEGER NOT NULL,
    cheapest_cents INTEGER
);
CREATE INDEX oracle_name ON oracle (name_key);
CREATE TABLE printing (
    scryfall_id TEXT PRIMARY KEY,
    oracle_id TEXT NOT NULL,
    set_code TEXT NOT NULL,
    collector_number TEXT NOT NULL,
    eur_cents INTEGER,
    eur_foil_cents INTEGER
);
CREATE TABLE role_member (role TEXT NOT NULL, oracle_id TEXT NOT NULL, PRIMARY KEY (role, oracle_id))
    WITHOUT ROWID;
CREATE TABLE role_available (role TEXT PRIMARY KEY);
"""


class CardDataError(Exception):
    """The card database is missing, outdated or unreadable; the message says how to fix it."""


class Role(Enum):
    RAMP = "ramp"
    DRAW = "draw"
    REMOVAL = "removal"
    BOARD_WIPE = "board wipe"
    TUTOR = "tutor"
    PROTECTION = "protection"
    RECURSION = "recursion"
    COUNTERSPELL = "counterspell"
    EXTRA_TURN = "extra turn"
    MASS_LAND_DENIAL = "mass land denial"


@dataclass(slots=True, frozen=True)
class CardInfo:
    """Facts shared by every printing of a card."""

    oracle_id: str
    name: str
    mana_cost: str
    cmc: float
    type_line: str
    color_identity: str
    game_changer: bool
    commander_legal: bool
    cheapest_cents: int | None
    roles: frozenset[Role]

    @property
    def front_type_line(self) -> str:
        return self.type_line.split(" // ")[0]

    @property
    def is_land(self) -> bool:
        # A modal card with a land back is played as a spell, so only the front face counts.
        return "Land" in self.front_type_line.split()


@dataclass(slots=True, frozen=True)
class Printing:
    scryfall_id: str
    oracle_id: str
    set_code: str
    collector_number: str
    eur_cents: int | None
    eur_foil_cents: int | None


@dataclass(slots=True, frozen=True)
class CardDataMeta:
    cards_updated_at: str
    tags_updated_at: str
    built_at: str


@dataclass(slots=True, frozen=True)
class ParsedCard:
    """One printing from the bulk file, with the gameplay facts of its card."""

    printing: Printing
    name: str
    mana_cost: str
    cmc: float
    type_line: str
    color_identity: str
    game_changer: bool
    commander_legal: bool


@dataclass(slots=True, frozen=True)
class ResolvedCard:
    card: DeckCard
    info: CardInfo | None
    printing: Printing | None


class CardDb:
    """Read-only view of a built card database."""

    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise CardDataError(f"no card data at {path}; run `cmdr sync` to download it")
        try:
            self._db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        except sqlite3.Error as error:
            raise CardDataError(f"cannot open card data {path}: {error}; run `cmdr sync --force`") from error
        try:
            self._query("PRAGMA busy_timeout = 5000")
            version = self._query("PRAGMA user_version")[0][0]
            rows = self._query("SELECT role FROM role_available") if version == SCHEMA_VERSION else []
        except BaseException:
            self._db.close()
            raise
        if version != SCHEMA_VERSION:
            self._db.close()
            raise CardDataError(f"card data {path} is from another version of cmdr; run `cmdr sync`")
        self._available = frozenset(Role(row[0]) for row in rows if _is_role(row[0]))

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        self._db.close()

    @property
    def available_roles(self) -> frozenset[Role]:
        """Roles whose tags were present in the last tag file; the rest cannot be counted."""
        return self._available

    def meta(self) -> CardDataMeta:
        values = dict(self._query("SELECT key, value FROM meta"))
        return CardDataMeta(
            values.get("cards_updated_at", "?"),
            values.get("tags_updated_at", "?"),
            values.get("built_at", "?"),
        )

    def printings(self, scryfall_ids: Iterable[str]) -> dict[str, Printing]:
        rows = self._select_in(
            "SELECT scryfall_id, oracle_id, set_code, collector_number, eur_cents, eur_foil_cents"
            " FROM printing WHERE scryfall_id IN ({})",
            scryfall_ids,
        )
        return {row[0]: Printing(*row) for row in rows}

    def cards(self, oracle_ids: Iterable[str]) -> dict[str, CardInfo]:
        return {info.oracle_id: info for info in self._cards_where("oracle_id", oracle_ids)}

    def cards_named(self, names: Iterable[str]) -> dict[str, CardInfo]:
        """Cards by name, matched ignoring case; keys are the casefolded names."""
        found: dict[str, CardInfo] = {}
        for info in self._cards_where("name_key", {name.casefold() for name in names}):
            # Ordered by oracle id, so a name shared by several cards resolves the same way every time.
            found.setdefault(info.name.casefold(), info)
        return found

    def resolve(self, cards: Sequence[DeckCard]) -> list[ResolvedCard]:
        """Match deck cards by printing, falling back to the card name when the printing is unknown."""
        printings = self.printings(card.scryfall_id for card in cards)
        infos = self.cards(p.oracle_id for p in printings.values())
        by_name = self.cards_named(card.name for card in cards if card.scryfall_id not in printings)
        resolved: list[ResolvedCard] = []
        for card in cards:
            printing = printings.get(card.scryfall_id)
            info = infos.get(printing.oracle_id) if printing else by_name.get(card.name.casefold())
            resolved.append(ResolvedCard(card, info, printing))
        return resolved

    def _cards_where(self, column: Literal["oracle_id", "name_key"], keys: Iterable[str]) -> list[CardInfo]:
        rows = self._select_in(_CARD_QUERIES[column], keys)
        roles = self._roles({row[0] for row in rows})
        return [
            CardInfo(
                oracle_id=row[0],
                name=row[1],
                mana_cost=row[2],
                cmc=row[3],
                type_line=row[4],
                color_identity=row[5],
                game_changer=bool(row[6]),
                commander_legal=bool(row[7]),
                cheapest_cents=row[8],
                roles=roles.get(row[0], frozenset()),
            )
            for row in rows
        ]

    def _roles(self, oracle_ids: Iterable[str]) -> dict[str, frozenset[Role]]:
        found: dict[str, set[Role]] = {}
        rows = self._select_in("SELECT oracle_id, role FROM role_member WHERE oracle_id IN ({})", oracle_ids)
        for oracle_id, role in rows:
            if _is_role(role):
                found.setdefault(oracle_id, set()).add(Role(role))
        return {key: frozenset(value) for key, value in found.items()}

    def _select_in(self, sql: str, keys: Iterable[str]) -> list[Any]:
        unique = sorted(set(keys))
        rows: list[Any] = []
        for start in range(0, len(unique), _QUERY_CHUNK):
            chunk = unique[start : start + _QUERY_CHUNK]
            rows += self._query(sql.format(", ".join("?" * len(chunk))), chunk)
        return rows

    def _query(self, sql: str, params: Sequence[str] = ()) -> list[Any]:
        try:
            return self._db.execute(sql, params).fetchall()
        except sqlite3.Error as error:
            raise CardDataError(f"card data is unreadable ({error}); run `cmdr sync --force`") from error


def read_meta(path: Path) -> CardDataMeta | None:
    """What the database at path was built from, or None when it is missing or unusable."""
    try:
        with CardDb(path) as db:
            return db.meta()
    except CardDataError as error:
        log.info("card data needs a rebuild: %s", error)
        return None


def write_card_db(
    dest: Path,
    cards: Iterable[ParsedCard],
    role_members: Mapping[Role, frozenset[str]],
    meta: CardDataMeta,
) -> int:
    """Build a fresh database next to dest and swap it in; returns the number of printings written."""
    temp = dest.with_name(f".{dest.name}.building")
    temp.unlink(missing_ok=True)
    try:
        count = _fill(temp, cards, role_members, meta)
        temp.replace(dest)
    except sqlite3.Error as error:
        temp.unlink(missing_ok=True)
        raise CardDataError(f"cannot build card data in {dest.parent}: {error}") from error
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return count


def _fill(
    path: Path, cards: Iterable[ParsedCard], role_members: Mapping[Role, frozenset[str]], meta: CardDataMeta
) -> int:
    # Built whole and swapped in by rename, never edited in place, so the default rollback journal is enough.
    db = sqlite3.connect(path)
    try:
        db.executescript(_SCHEMA)
        count = 0
        oracle: dict[str, tuple[object, ...]] = {}
        cheapest: dict[str, int] = {}
        with db:
            for batch in _batches(cards, 5000):
                db.executemany(
                    "INSERT OR REPLACE INTO printing VALUES (?, ?, ?, ?, ?, ?)", _printing_rows(batch)
                )
                count += len(batch)
                for card in batch:
                    oracle_id = card.printing.oracle_id
                    oracle.setdefault(oracle_id, _oracle_row(card))
                    low = _lowest(
                        card.printing.eur_cents, card.printing.eur_foil_cents, cheapest.get(oracle_id)
                    )
                    if low is not None:
                        cheapest[oracle_id] = low
            db.executemany(
                "INSERT INTO oracle VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ((*row, cheapest.get(oracle_id)) for oracle_id, row in oracle.items()),
            )
            db.executemany(
                "INSERT INTO role_member VALUES (?, ?)",
                ((role.value, oracle_id) for role, members in role_members.items() for oracle_id in members),
            )
            db.executemany("INSERT INTO role_available VALUES (?)", ((role.value,) for role in role_members))
            db.executemany(
                "INSERT INTO meta VALUES (?, ?)",
                [
                    ("cards_updated_at", meta.cards_updated_at),
                    ("tags_updated_at", meta.tags_updated_at),
                    ("built_at", meta.built_at),
                ],
            )
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        db.close()
    return count


def _printing_rows(batch: list[ParsedCard]) -> Iterator[tuple[object, ...]]:
    for card in batch:
        p = card.printing
        yield (p.scryfall_id, p.oracle_id, p.set_code, p.collector_number, p.eur_cents, p.eur_foil_cents)


def _oracle_row(card: ParsedCard) -> tuple[object, ...]:
    return (
        card.printing.oracle_id,
        card.name,
        card.name.casefold(),
        card.mana_cost,
        card.cmc,
        card.type_line,
        card.color_identity,
        int(card.game_changer),
        int(card.commander_legal),
    )


def _lowest(*prices: int | None) -> int | None:
    known = [price for price in prices if price is not None]
    return min(known) if known else None


def _batches(cards: Iterable[ParsedCard], size: int) -> Iterator[list[ParsedCard]]:
    batch: list[ParsedCard] = []
    for card in cards:
        batch.append(card)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def _is_role(value: object) -> bool:
    return value in {role.value for role in Role}
