"""Local card database built from Scryfall bulk data: gameplay facts, EUR prices and card roles."""

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

from commander.fsutil import new_temp_path, replace_with_retry
from commander.models import DeckCard

log = logging.getLogger(__name__)

# Bump whenever the tables or the meaning of a column change; an old file is rebuilt, never migrated.
SCHEMA_VERSION = 8
CARD_DB_FILE = "cards.sqlite"
# The suffix of a database being built beside the real one; a crash can leave such a file behind.
BUILD_SUFFIX = ".building"
_QUERY_CHUNK = 500
_FRONT_FACE_CHUNK = 100
_FRONT_FACES_QUERY = (
    "SELECT oracle_id, name, mana_cost, cmc, type_line, color_identity, game_changer, commander_legal,"
    " cheapest_cents, edhrec_rank FROM oracle WHERE {} ORDER BY oracle_id"
)
_CARD_QUERIES = {
    "oracle_id": "SELECT oracle_id, name, mana_cost, cmc, type_line, color_identity, game_changer,"
    " commander_legal, cheapest_cents, edhrec_rank FROM oracle WHERE oracle_id IN ({}) ORDER BY oracle_id",
    "name_key": "SELECT oracle_id, name, mana_cost, cmc, type_line, color_identity, game_changer,"
    " commander_legal, cheapest_cents, edhrec_rank FROM oracle WHERE name_key IN ({})"
    " ORDER BY layout = 'front_card', oracle_id",
}
_INFO_COLUMNS = (
    "oracle.oracle_id, name, mana_cost, cmc, type_line, color_identity, game_changer, commander_legal,"
    " cheapest_cents, edhrec_rank"
)
# A card fits when each colour of its identity is in :identity; a NULL :max_cents means no price limit.
_FITS_DECK = """commander_legal = 1 AND edhrec_rank IS NOT NULL
    AND (instr(:identity, 'W') > 0 OR instr(color_identity, 'W') = 0)
    AND (instr(:identity, 'U') > 0 OR instr(color_identity, 'U') = 0)
    AND (instr(:identity, 'B') > 0 OR instr(color_identity, 'B') = 0)
    AND (instr(:identity, 'R') > 0 OR instr(color_identity, 'R') = 0)
    AND (instr(:identity, 'G') > 0 OR instr(color_identity, 'G') = 0)
    AND (:max_cents IS NULL OR cheapest_cents <= :max_cents)"""
# CROSS JOIN keeps oracle as the outer loop, so cards are read in rank order through oracle_rank and a
# caller that stops after a few rows never sorts the whole role (23 ms down to 0.1 ms for ramp).
_CANDIDATE_QUERY = f"""
SELECT {_INFO_COLUMNS}
FROM oracle CROSS JOIN role_member ON role_member.oracle_id = oracle.oracle_id
WHERE role = :role AND {_FITS_DECK}
ORDER BY edhrec_rank, oracle.oracle_id
"""  # noqa: S608 — built from constant fragments and placeholders; every value is bound
COLORS = "WUBRG"
# Joins the faces of a two-faced card in its name, type line and mana cost.
_NAME_FACE_SEPARATOR = " // "
FACE_SEPARATOR = "\n//\n"
# The tags given, grown by every sub-tag; UNION drops repeats, so a cycle in the tag tree still ends.
_SUBTREE_CTE = """WITH RECURSIVE subtree(tag_id) AS (
    SELECT tag_id FROM tag WHERE tag_id IN ({})
    UNION SELECT tag_child.child_id FROM tag_child JOIN subtree ON tag_child.parent_id = subtree.tag_id
)"""
_TAG_COUNT_QUERY = (
    _SUBTREE_CTE.format("?")  # noqa: S608 — built from constant fragments and placeholders; every value is bound
    # CROSS JOIN keeps the subtree first; left alone SQLite scans every tag_member row and probes the subtree.
    + " SELECT count(DISTINCT oracle_id)"
    " FROM subtree CROSS JOIN tag_member ON tag_member.tag_id = subtree.tag_id"
)
_SUBTREE_SLUGS_QUERY = (
    _SUBTREE_CTE.format("SELECT tag_id FROM tag WHERE slug = ?")  # noqa: S608 — built from constant fragments and placeholders; every value is bound
    + " SELECT slug FROM tag WHERE tag_id IN (SELECT tag_id FROM subtree)"
)
# Each card's tags grown by every tag above them; UNION drops repeats, so a cycle in the tag tree still ends.
_LINEAGE_QUERY = """WITH RECURSIVE lineage(oracle_id, tag_id) AS (
    SELECT oracle_id, tag_id FROM tag_member WHERE oracle_id IN ({})
    UNION SELECT lineage.oracle_id, tag_child.parent_id FROM tag_child
        JOIN lineage ON tag_child.child_id = lineage.tag_id
) SELECT lineage.oracle_id, slug FROM lineage JOIN tag ON tag.tag_id = lineage.tag_id"""
_ANCESTORS_QUERY = """WITH RECURSIVE above(tag_id) AS (
    SELECT parent_id FROM tag_child JOIN tag ON tag.tag_id = tag_child.child_id WHERE slug IN ({})
    UNION SELECT tag_child.parent_id FROM tag_child JOIN above ON tag_child.child_id = above.tag_id
) SELECT slug FROM above JOIN tag ON tag.tag_id = above.tag_id"""
MAX_TAG_MATCHES = 50
MAX_NAME_MATCHES = 20
# Enough of the most played lands to find a dual or an untapped twin for any deck (ADR 0024).
MAX_LANDS = 400
_NAME_SEARCH_QUERY = f"""
SELECT {_INFO_COLUMNS} FROM oracle
WHERE commander_legal = 1 AND instr(name_key, :needle) > 0
    AND (instr(:identity, 'W') > 0 OR instr(color_identity, 'W') = 0)
    AND (instr(:identity, 'U') > 0 OR instr(color_identity, 'U') = 0)
    AND (instr(:identity, 'B') > 0 OR instr(color_identity, 'B') = 0)
    AND (instr(:identity, 'R') > 0 OR instr(color_identity, 'R') = 0)
    AND (instr(:identity, 'G') > 0 OR instr(color_identity, 'G') = 0)
ORDER BY instr(name_key, :needle) != 1, edhrec_rank IS NULL, edhrec_rank, name_key
LIMIT :limit
"""  # noqa: S608 — built from constant fragments; every value is bound

# Cards that can lead a deck: legendary creatures (front face) and cards that say they can be a commander.
_COMMANDER_SEARCH_QUERY = f"""
SELECT {_INFO_COLUMNS} FROM oracle
WHERE commander_legal = 1 AND instr(name_key, :needle) > 0
    AND (type_line LIKE 'Legendary%Creature%' OR oracle_text LIKE '%can be your commander%')
ORDER BY instr(name_key, :needle) != 1, edhrec_rank IS NULL, edhrec_rank, name_key
LIMIT :limit
"""  # noqa: S608 — built from constant fragments; every value is bound

# Any legal card, for the second name of a pair: a partner, a Background or whatever else may sit beside one.
_LEGAL_SEARCH_QUERY = f"""
SELECT {_INFO_COLUMNS} FROM oracle
WHERE commander_legal = 1 AND instr(name_key, :needle) > 0
ORDER BY instr(name_key, :needle) != 1, edhrec_rank IS NULL, edhrec_rank, name_key
LIMIT :limit
"""  # noqa: S608 — built from constant fragments; every value is bound

_TAG_SUGGEST_QUERY = """
SELECT t.slug, count(m.oracle_id) AS cards FROM tag t LEFT JOIN tag_member m ON m.tag_id = t.tag_id
WHERE instr(t.slug, :needle) > 0
GROUP BY t.tag_id
ORDER BY instr(t.slug, :needle) != 1, cards DESC, t.slug
LIMIT :limit
"""

# Nonbasic lands that fit a deck, most played first; the caller keeps the ones whose front face is a land.
_LAND_QUERY = f"""
SELECT oracle.oracle_id, name, mana_cost, cmc, type_line, oracle_text, produced, rarity, layout,
    cheapest_cents
FROM oracle JOIN card_mana ON card_mana.oracle_id = oracle.oracle_id
WHERE {_FITS_DECK} AND instr(type_line, 'Land') > 0 AND type_line NOT LIKE 'Basic %'
ORDER BY edhrec_rank, oracle.oracle_id
LIMIT :limit
"""  # noqa: S608 — built from constant fragments and placeholders; every value is bound
_CARD_MANA_QUERY = """
SELECT o.oracle_id, o.name, o.mana_cost, o.cmc, o.type_line, o.oracle_text, m.produced, m.rarity, o.layout
FROM oracle o JOIN card_mana m ON m.oracle_id = o.oracle_id
WHERE o.name_key IN ({})
ORDER BY o.layout = 'front_card', o.oracle_id
"""

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
    edhrec_rank INTEGER,
    oracle_text TEXT NOT NULL,
    -- Casefolded keywords between bars, like |flying|discover|, so one instr() finds a whole keyword.
    keywords TEXT NOT NULL,
    power TEXT,
    toughness TEXT,
    -- The card's own colours (front face's for a two-faced card), unlike its colour identity.
    colors TEXT NOT NULL,
    layout TEXT NOT NULL,
    cheapest_cents INTEGER
);
CREATE TABLE card_mana (
    oracle_id TEXT PRIMARY KEY,
    produced TEXT NOT NULL,
    rarity TEXT NOT NULL
);
CREATE TABLE printing (
    scryfall_id TEXT PRIMARY KEY,
    oracle_id TEXT NOT NULL,
    set_code TEXT NOT NULL,
    collector_number TEXT NOT NULL,
    eur_cents INTEGER,
    eur_foil_cents INTEGER,
    regular INTEGER NOT NULL
);
CREATE TABLE role_member (role TEXT NOT NULL, oracle_id TEXT NOT NULL, PRIMARY KEY (role, oracle_id))
    WITHOUT ROWID;
CREATE TABLE role_available (role TEXT PRIMARY KEY);
CREATE TABLE tag (tag_id TEXT PRIMARY KEY, slug TEXT NOT NULL);
CREATE TABLE tag_child (parent_id TEXT NOT NULL, child_id TEXT NOT NULL, PRIMARY KEY (parent_id, child_id))
    WITHOUT ROWID;
CREATE TABLE tag_member (tag_id TEXT NOT NULL, oracle_id TEXT NOT NULL, PRIMARY KEY (tag_id, oracle_id))
    WITHOUT ROWID;
"""

# Made after the rows are in, which is quicker than keeping each index up to date through every insert.
_INDEXES = (
    "CREATE INDEX oracle_name ON oracle (name_key)",
    "CREATE INDEX oracle_rank ON oracle (edhrec_rank, oracle_id)",
    "CREATE INDEX printing_oracle ON printing (oracle_id)",
    "CREATE INDEX role_member_card ON role_member (oracle_id)",
    "CREATE INDEX tag_slug ON tag (slug)",
    "CREATE INDEX tag_member_card ON tag_member (oracle_id)",
)


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


def front_face(text: str) -> str:
    """The front face of a two-faced card's name, type line or mana cost; one-faced text comes back as is."""
    return text.split(_NAME_FACE_SEPARATOR, maxsplit=1)[0]


def front_name(name: str) -> str:
    return front_face(name)


def front_type(type_line: str) -> str:
    return front_face(type_line)


def name_keys(name: str) -> frozenset[str]:
    """The casefolded names a card answers to: its full name and, for two faces, the front face's."""
    key = name.casefold()
    return frozenset({key, front_name(key)})


def back_is_land(type_line: str) -> bool:
    """A card that is a spell on its front face and a land on its back (Shatterskull Smashing)."""
    faces = type_line.split(_NAME_FACE_SEPARATOR)
    return len(faces) > 1 and "Land" in faces[-1].split() and "Land" not in faces[0].split()


def has_nonland_face(type_line: str) -> bool:
    """A land with a spell or artifact half (Midgar's Adventure, Balamb Garden's vehicle back)."""
    return any("Land" not in face.split() for face in type_line.split(_NAME_FACE_SEPARATOR))


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
    # How often EDHREC sees the card in decks, 1 = most; None for basics and cards too new to rank.
    edhrec_rank: int | None
    roles: frozenset[Role]

    @property
    def front_type_line(self) -> str:
        return front_type(self.type_line)

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
    # A printing sanctioned play accepts; gold or silver borders, oversized cards and memorabilia are not.
    regular: bool = True


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
    edhrec_rank: int | None
    # Faces of a two-faced card are joined by FACE_SEPARATOR; power and toughness are the front face's.
    oracle_text: str = ""
    keywords: tuple[str, ...] = ()
    power: str | None = None
    toughness: str | None = None
    colors: str = ""
    # The mana it can make, as colour letters and C for colourless ("WU"), from Scryfall's produced_mana.
    produced: str = ""
    rarity: str = ""
    # Scryfall's layout ("normal", "adventure", "split", "modal_dfc"); it decides how a two-part card is cast.
    layout: str = "normal"


@dataclass(slots=True, frozen=True)
class CardMana:
    """What a card does for a mana base: the mana it makes, its rarity, its text (ADR 0019)."""

    oracle_id: str
    name: str
    mana_cost: str
    cmc: float
    type_line: str
    oracle_text: str
    produced: str
    rarity: str
    layout: str = "normal"


@dataclass(slots=True, frozen=True)
class Tag:
    """A Tagger oracle tag: what a card does or is about, such as ramp, stax or blink."""

    id: str
    slug: str
    child_ids: tuple[str, ...]
    oracle_ids: frozenset[str]


@dataclass(slots=True, frozen=True)
class TextGate:
    """Cards that hold a role only when their rules text contains phrase (casefolded)."""

    role: Role
    oracle_ids: frozenset[str]
    phrase: str


@dataclass(slots=True, frozen=True)
class CardText:
    oracle_id: str
    text: str
    keywords: tuple[str, ...]
    power: str | None
    toughness: str | None
    colors: str
    # Slugs of the tags put on the card itself, not their parents.
    tags: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class Theme:
    """Cards that match any one of these: a type word, a keyword or a tag (by id, sub-tags included)."""

    types: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    tag_ids: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.types or self.keywords or self.tag_ids)


NO_THEME = Theme()


@dataclass(slots=True, frozen=True)
class TagCount:
    slug: str
    # Cards with the tag or any of its sub-tags.
    cards: int


@dataclass(slots=True, frozen=True)
class ResolvedCard:
    card: DeckCard
    info: CardInfo | None
    printing: Printing | None


@dataclass(slots=True, eq=False)
class _BuildAnswers:
    """Counts that depend only on one build of the card database and cost a table scan to work out."""

    stamp: tuple[object, ...]
    legal_cards: int | None = None
    tag_sizes: dict[str, int] = field(default_factory=dict[str, int])
    theme_sizes: dict[Theme, int] = field(default_factory=dict[Theme, int])


# The newest build's answers per database file, so a rebuilt file replaces its old entry.
_answers: dict[Path, _BuildAnswers] = {}
_answers_lock = threading.Lock()


class CardDb:
    """Read-only view of a built card database."""

    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise CardDataError(f"no card data at {path}; run `cmdr sync` to download it")
        resolved = path.resolve()
        try:
            stat = resolved.stat()
            self._db = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
        except (OSError, sqlite3.Error) as error:
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
        self._file_stamp = (resolved, stat.st_mtime_ns, stat.st_size)

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
            "SELECT scryfall_id, oracle_id, set_code, collector_number, eur_cents, eur_foil_cents, regular"
            " FROM printing WHERE scryfall_id IN ({})",
            scryfall_ids,
        )
        return {row[0]: _printing(row) for row in rows}

    def cards(self, oracle_ids: Iterable[str]) -> dict[str, CardInfo]:
        return {info.oracle_id: info for info in self._cards_where("oracle_id", oracle_ids)}

    def cards_named(self, names: Iterable[str]) -> dict[str, CardInfo]:
        """Cards by name, matched ignoring case like `card_named`: a two-faced card answers to its full name
        or its front face. Keys are the casefolded names as asked."""
        asked = {name.casefold() for name in names}
        found: dict[str, CardInfo] = {}
        for info in self._cards_where("name_key", asked):
            # Real cards come before theme cards ("front_card" objects reuse real names), then by oracle id,
            # so a name shared by several cards resolves the same way every time.
            found.setdefault(info.name.casefold(), info)
        # A front face is the start of a "front // back" name: an index range per name, in chunks to keep the
        # statement small.
        fronts = sorted(key for key in asked - found.keys() if " // " not in key)
        for start in range(0, len(fronts), _FRONT_FACE_CHUNK):
            chunk = fronts[start : start + _FRONT_FACE_CHUNK]
            ranges = " OR ".join("(name_key >= ? AND name_key < ?)" for _ in chunk)
            # "!" sorts just above the space of " // ", so the range holds every name starting "front // ".
            bounds = [bound for key in chunk for bound in (key + " // ", key + " //!")]
            for info in self._infos(self._query(_FRONT_FACES_QUERY.format(ranges), bounds)):
                found.setdefault(front_name(info.name.casefold()), info)
        return found

    def card_named(self, name: str) -> CardInfo | None:
        """One card by its full name or, for cards with two faces, its front face; case is ignored."""
        key = name.casefold()
        return self.cards_named([key]).get(key)

    def printings_of(self, oracle_id: str) -> list[Printing]:
        """Every printing of a card, ordered by set and collector number."""
        rows = self._query(
            "SELECT scryfall_id, oracle_id, set_code, collector_number, eur_cents, eur_foil_cents, regular"
            " FROM printing WHERE oracle_id = ? ORDER BY set_code, collector_number",
            [oracle_id],
        )
        return [_printing(row) for row in rows]

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

    def candidates(
        self,
        role: Role,
        identity: str,
        *,
        exclude: Collection[str],
        max_cents: int | None = None,
    ) -> Iterator[CardInfo]:
        """Ranked, legal nonland cards with a role that fit a colour identity, the most played first.

        Lazy, so a caller can stop once it has the ones it keeps. exclude holds oracle ids to skip, such
        as the deck's own cards; a max_cents limit leaves out unpriced cards too.
        """
        params = {"role": role.value, "identity": identity, "max_cents": max_cents}
        try:
            for row in self._db.execute(_CANDIDATE_QUERY, params):
                if row[0] in exclude:
                    continue
                info = self._infos([row])[0]
                if not info.is_land:
                    yield info
        except sqlite3.Error as error:
            raise CardDataError(f"card data is unreadable ({error}); run `cmdr sync --force`") from error

    def search_names(self, text: str, identity: str, *, limit: int = MAX_NAME_MATCHES) -> list[CardInfo]:
        """Legal cards in the identity whose name holds text: names starting with it first, then by play."""
        needle = text.strip().casefold()
        if not needle:
            return []
        params = {"needle": needle, "identity": identity, "limit": max(1, min(limit, MAX_NAME_MATCHES))}
        return self._infos(self._query_named(_NAME_SEARCH_QUERY, params))

    def search_commanders(self, text: str, *, limit: int = MAX_NAME_MATCHES) -> list[CardInfo]:
        """Legal commanders whose name holds text: names starting with it first, then by play."""
        needle = text.strip().casefold()
        if not needle:
            return []
        params = {"needle": needle, "limit": max(1, min(limit, MAX_NAME_MATCHES))}
        return self._infos(self._query_named(_COMMANDER_SEARCH_QUERY, params))

    def search_legal(self, text: str, *, limit: int = MAX_NAME_MATCHES) -> list[CardInfo]:
        """Commander-legal cards of any type whose name holds text, ranked like `search_commanders`."""
        needle = text.strip().casefold()
        if not needle:
            return []
        params = {"needle": needle, "limit": max(1, min(limit, MAX_NAME_MATCHES))}
        return self._infos(self._query_named(_LEGAL_SEARCH_QUERY, params))

    def theme_candidates(
        self, theme: Theme, identity: str, *, exclude: Collection[str], max_cents: int | None = None
    ) -> Iterator[CardInfo]:
        """Like candidates, for the cards matching a theme instead of a role; nothing for an empty theme."""
        if theme.empty:
            return
        cte, condition, params = _theme_sql(theme)
        sql = (
            f"{cte} SELECT {_INFO_COLUMNS} FROM oracle WHERE {_FITS_DECK} AND ({condition})"  # noqa: S608 — built from constant fragments and placeholders; every value is bound
            " ORDER BY edhrec_rank, oracle_id"
        )
        try:
            for row in self._db.execute(sql, {**params, "identity": identity, "max_cents": max_cents}):
                if row[0] in exclude:
                    continue
                info = self._infos([row])[0]
                if not info.is_land:
                    yield info
        except sqlite3.Error as error:
            raise CardDataError(f"card data is unreadable ({error}); run `cmdr sync --force`") from error

    def matching(self, theme: Theme, oracle_ids: Iterable[str]) -> frozenset[str]:
        """The given cards that match the theme."""
        if theme.empty:
            return frozenset()
        cte, condition, params = _theme_sql(theme)
        unique = sorted(set(oracle_ids))
        found: set[str] = set()
        for start in range(0, len(unique), _QUERY_CHUNK):
            chunk = {f"o{i}": key for i, key in enumerate(unique[start : start + _QUERY_CHUNK])}
            placeholders = ", ".join(f":{name}" for name in chunk)
            where = f"oracle_id IN ({placeholders}) AND ({condition})"
            sql = f"{cte} SELECT oracle_id FROM oracle WHERE {where}"  # noqa: S608 — built from constant fragments and placeholders; every value is bound
            found.update(row[0] for row in self._query_named(sql, {**params, **chunk}))
        return frozenset(found)

    def theme_size(self, theme: Theme) -> int:
        """How many Commander-legal cards match the theme; 0 for an empty one."""
        if theme.empty:
            return 0
        answers = self._build_answers()
        if (size := answers.theme_sizes.get(theme)) is None:
            cte, condition, params = _theme_sql(theme)
            sql = f"{cte} SELECT count(*) FROM oracle WHERE commander_legal = 1 AND ({condition})"  # noqa: S608 — built from constant fragments and placeholders; every value is bound
            size = answers.theme_sizes[theme] = int(self._query_named(sql, params)[0][0])
        return size

    def legal_cards(self) -> int:
        answers = self._build_answers()
        if answers.legal_cards is None:
            rows = self._query("SELECT count(*) FROM oracle WHERE commander_legal = 1")
            answers.legal_cards = int(rows[0][0])
        return answers.legal_cards

    def _build_answers(self) -> _BuildAnswers:
        """The cached counts of the build this connection reads; a rebuilt file starts with none."""
        stamp = (*self._file_stamp, self.meta())
        with _answers_lock:
            found = _answers.get(self._file_stamp[0])
            if found is None or found.stamp != stamp:
                found = _answers[self._file_stamp[0]] = _BuildAnswers(stamp)
            return found

    def tag_lineage(self, oracle_ids: Iterable[str]) -> dict[str, frozenset[str]]:
        """Each card's tag slugs with every tag above them, as a card in combat-ramp is in ramp too."""
        found: dict[str, set[str]] = {}
        for oracle_id, slug in self._select_in(_LINEAGE_QUERY, oracle_ids):
            found.setdefault(oracle_id, set()).add(slug)
        return {key: frozenset(slugs) for key, slugs in found.items()}

    def tag_ancestors(self, slugs: Iterable[str]) -> frozenset[str]:
        """Every tag above these (ramp above combat-ramp); slugs Scryfall does not have add nothing."""
        return frozenset(row[0] for row in self._select_in(_ANCESTORS_QUERY, slugs))

    def tag_ids(self, slugs: Iterable[str]) -> dict[str, str]:
        """Tag ids by slug; slugs Scryfall does not have are left out."""
        rows = self._select_in("SELECT slug, tag_id FROM tag WHERE slug IN ({})", slugs)
        return dict(rows)

    def tag_subtree(self, slug: str) -> frozenset[str]:
        """The tag and every tag below it, by slug; empty when Scryfall does not have it."""
        return frozenset(row[0] for row in self._query(_SUBTREE_SLUGS_QUERY, [slug]))

    def tag_sizes(self, slugs: Iterable[str]) -> dict[str, int]:
        """How many cards each tag holds, sub-tags included; slugs Scryfall does not have are left out."""
        asked = list(dict.fromkeys(slugs))
        known = self._build_answers().tag_sizes
        for slug, tag_id in self.tag_ids(slug for slug in asked if slug not in known).items():
            known[slug] = int(self._query(_TAG_COUNT_QUERY, [tag_id])[0][0])
        return {slug: known[slug] for slug in asked if slug in known}

    def suggest_tags(self, text: str, *, limit: int = MAX_TAG_MATCHES) -> list[TagCount]:
        """Tags whose slug holds text, quick enough to run on each keystroke: slugs starting with it
        first, then by the cards tagged directly (sub-tags not counted, unlike find_tags)."""
        needle = text.strip().casefold()
        if not needle:
            return []
        rows = self._query_named(_TAG_SUGGEST_QUERY, {"needle": needle, "limit": max(1, limit)})
        return [TagCount(slug, int(cards)) for slug, cards in rows]

    def find_tags(self, text: str) -> list[TagCount]:
        """Tags whose slug contains text, most cards first, at most MAX_TAG_MATCHES."""
        needle = text.strip().casefold()
        if not needle:
            return []
        rows = self._query("SELECT tag_id, slug FROM tag WHERE instr(slug, ?) > 0", [needle])
        sizes = self.tag_sizes(slug for _, slug in rows)
        counts = [TagCount(slug, cards) for slug, cards in sizes.items()]
        counts.sort(key=lambda t: (-t.cards, t.slug))
        return counts[:MAX_TAG_MATCHES]

    def card_mana(self, names: Iterable[str]) -> dict[str, CardMana]:
        """What each named card does for a mana base, by casefolded name; unknown names are left out."""
        rows = self._select_in(_CARD_MANA_QUERY, [name.casefold() for name in names])
        found: dict[str, CardMana] = {}
        for row in rows:
            # Rows come real cards first, so a theme card sharing the name never replaces its card.
            found.setdefault(row[1].casefold(), CardMana(*row))
        return found

    def lands(
        self, identity: str, *, max_cents: int | None = None, limit: int = MAX_LANDS
    ) -> list[tuple[CardMana, int | None]]:
        """Legal nonbasic lands in a colour identity with their cheapest price, the most played first."""
        params = {"identity": identity, "max_cents": max_cents, "limit": limit}
        return [(CardMana(*row[:9]), row[9]) for row in self._query_named(_LAND_QUERY, params)]

    def roles(self, oracle_ids: Iterable[str]) -> dict[str, frozenset[Role]]:
        """Each card's roles by oracle id, from its tags, for cards the tags know even before the card
        data does (a preview); cards with no role are left out."""
        return self._roles(oracle_ids)

    def card_text(self, oracle_id: str) -> CardText | None:
        return self.card_texts([oracle_id]).get(oracle_id)

    def card_texts(self, oracle_ids: Iterable[str]) -> dict[str, CardText]:
        """Rules text, keywords and tags by oracle id; unknown ids are left out."""
        ids = set(oracle_ids)
        rows = self._select_in(
            "SELECT oracle_id, oracle_text, keywords, power, toughness, colors FROM oracle"
            " WHERE oracle_id IN ({})",
            ids,
        )
        slugs: dict[str, list[str]] = {}
        for oracle_id, slug in self._select_in(
            "SELECT oracle_id, slug FROM tag JOIN tag_member ON tag.tag_id = tag_member.tag_id"
            " WHERE oracle_id IN ({}) ORDER BY slug",
            ids,
        ):
            slugs.setdefault(oracle_id, []).append(slug)
        return {
            oracle_id: CardText(
                oracle_id,
                text,
                tuple(k for k in keywords.split("|") if k),
                power,
                toughness,
                colors,
                tuple(slugs.get(oracle_id, ())),
            )
            for oracle_id, text, keywords, power, toughness, colors in rows
        }

    def _query_named(self, sql: str, params: Mapping[str, object]) -> list[Any]:
        try:
            return self._db.execute(sql, params).fetchall()
        except sqlite3.Error as error:
            raise CardDataError(f"card data is unreadable ({error}); run `cmdr sync --force`") from error

    def _cards_where(self, column: Literal["oracle_id", "name_key"], keys: Iterable[str]) -> list[CardInfo]:
        return self._infos(self._select_in(_CARD_QUERIES[column], keys))

    def _infos(self, rows: Sequence[Any]) -> list[CardInfo]:
        roles = self._roles({row[0] for row in rows})
        return [_row_info(row, roles.get(row[0], frozenset())) for row in rows]

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


def _theme_sql(theme: Theme) -> tuple[str, str, dict[str, object]]:
    """The WITH clause, the condition on oracle and the named parameters that match a theme."""
    params: dict[str, object] = {}
    parts: list[str] = []
    for index, word in enumerate(theme.types):
        params[f"type{index}"] = f" {word.casefold()} "
        # Spaces around each word of the type line, so "Elf" never matches "Elfball" or "Self".
        parts.append(
            f"instr(' ' || lower(replace(replace(type_line, '—', ' '), '//', ' ')) || ' ', :type{index}) > 0"
        )
    for index, keyword in enumerate(theme.keywords):
        params[f"keyword{index}"] = f"|{keyword.casefold()}|"
        parts.append(f"instr(keywords, :keyword{index}) > 0")
    cte = ""
    if theme.tag_ids:
        names = [f":tag{index}" for index in range(len(theme.tag_ids))]
        params.update({name[1:]: tag_id for name, tag_id in zip(names, theme.tag_ids, strict=True)})
        cte = _SUBTREE_CTE.format(", ".join(names))
        parts.append(
            "oracle.oracle_id IN"
            " (SELECT tag_member.oracle_id"
            " FROM subtree CROSS JOIN tag_member ON tag_member.tag_id = subtree.tag_id)"
        )
    return cte, " OR ".join(parts), params


def _row_info(row: Sequence[Any], roles: frozenset[Role]) -> CardInfo:
    return CardInfo(
        oracle_id=row[0],
        name=row[1],
        mana_cost=row[2],
        cmc=row[3],
        type_line=row[4],
        color_identity=row[5],
        game_changer=bool(row[6]),
        commander_legal=bool(row[7]),
        cheapest_cents=row[8],
        edhrec_rank=row[9],
        roles=roles,
    )


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
    *,
    tags: Mapping[str, Tag] | None = None,
    gates: Sequence[TextGate] = (),
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Build a fresh database next to dest and swap it in; returns the number of printings written.

    Another process (a CLI sync beside the app) may be building or reading the same database: each build
    has its own temp file, and a swap blocked by a reader is retried before giving up with the old data kept.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = new_temp_path(dest.parent, dest.name, BUILD_SUFFIX)
    try:
        count = _fill(temp, cards, role_members, meta, tags=tags or {}, gates=gates)
        replace_with_retry(temp, dest, sleep=sleep)
    except sqlite3.Error as error:
        temp.unlink(missing_ok=True)
        raise CardDataError(f"cannot build card data in {dest.parent}: {error}") from error
    except PermissionError as error:
        temp.unlink(missing_ok=True)
        log.exception("cannot replace %s", dest)
        raise CardDataError(
            f"{dest} is in use by another program; the previous card data is kept, try again shortly"
        ) from error
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return count


def _fill(
    path: Path,
    cards: Iterable[ParsedCard],
    role_members: Mapping[Role, frozenset[str]],
    meta: CardDataMeta,
    *,
    tags: Mapping[str, Tag],
    gates: Sequence[TextGate],
) -> int:
    db = sqlite3.connect(path)
    try:
        # Built whole and swapped in by rename, and a failed build deletes the file: no journal, no syncing.
        db.execute("PRAGMA journal_mode = OFF")
        db.execute("PRAGMA synchronous = OFF")
        db.executescript(_SCHEMA)
        count = 0
        oracle: dict[str, tuple[object, ...]] = {}
        mana: dict[str, tuple[str, str, str]] = {}
        # Cheapest regular printing first; a card with no priced regular printing falls back to any.
        cheapest: dict[str, int] = {}
        cheapest_any: dict[str, int] = {}
        gated = {oracle_id for gate in gates for oracle_id in gate.oracle_ids}
        gated_text: dict[str, str] = {}
        with db:
            for batch in _batches(cards, 5000):
                db.executemany(
                    "INSERT OR REPLACE INTO printing VALUES (?, ?, ?, ?, ?, ?, ?)", _printing_rows(batch)
                )
                count += len(batch)
                for card in batch:
                    oracle_id = card.printing.oracle_id
                    oracle.setdefault(oracle_id, _oracle_row(card))
                    if oracle_id in gated:
                        gated_text.setdefault(oracle_id, card.oracle_text.casefold())
                    mana.setdefault(oracle_id, (oracle_id, card.produced, card.rarity))
                    prices = (card.printing.eur_cents, card.printing.eur_foil_cents)
                    _keep_lowest(cheapest_any, oracle_id, *prices)
                    if card.printing.regular:
                        _keep_lowest(cheapest, oracle_id, *prices)
            db.executemany(
                "INSERT INTO oracle VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (*row, cheapest.get(oracle_id, cheapest_any.get(oracle_id)))
                    for oracle_id, row in oracle.items()
                ),
            )
            db.executemany("INSERT INTO card_mana VALUES (?, ?, ?)", mana.values())
            db.executemany(
                "INSERT INTO role_member VALUES (?, ?)",
                (
                    (role.value, oracle_id)
                    for role, members in _ungate(role_members, gates, gated_text).items()
                    for oracle_id in members
                ),
            )
            db.executemany("INSERT INTO role_available VALUES (?)", ((role.value,) for role in role_members))
            _insert_tags(db, tags)
            db.executemany(
                "INSERT INTO meta VALUES (?, ?)",
                [
                    ("cards_updated_at", meta.cards_updated_at),
                    ("tags_updated_at", meta.tags_updated_at),
                    ("built_at", meta.built_at),
                ],
            )
            for statement in _INDEXES:
                db.execute(statement)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        db.close()
    return count


def _printing_rows(batch: list[ParsedCard]) -> Iterator[tuple[object, ...]]:
    for card in batch:
        p = card.printing
        yield (
            p.scryfall_id,
            p.oracle_id,
            p.set_code,
            p.collector_number,
            p.eur_cents,
            p.eur_foil_cents,
            int(p.regular),
        )


def _ungate(
    role_members: Mapping[Role, frozenset[str]], gates: Sequence[TextGate], texts: Mapping[str, str]
) -> dict[Role, frozenset[str]]:
    """Each role's members plus its gated cards whose text holds the gate's phrase."""
    members = dict(role_members)
    for gate in gates:
        if gate.role in members:
            passed = {o for o in gate.oracle_ids if gate.phrase.casefold() in texts.get(o, "")}
            members[gate.role] = members[gate.role] | passed
    return members


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
        card.edhrec_rank,
        card.oracle_text,
        "".join(f"|{keyword.casefold()}" for keyword in card.keywords) + "|" if card.keywords else "",
        card.power,
        card.toughness,
        card.colors,
        card.layout,
    )


def _insert_tags(db: sqlite3.Connection, tags: Mapping[str, Tag]) -> None:
    db.executemany("INSERT INTO tag VALUES (?, ?)", ((tag.id, tag.slug) for tag in tags.values()))
    db.executemany(
        "INSERT OR IGNORE INTO tag_child VALUES (?, ?)",
        ((tag.id, child) for tag in tags.values() for child in tag.child_ids if child in tags),
    )
    db.executemany(
        "INSERT INTO tag_member VALUES (?, ?)",
        ((tag.id, oracle_id) for tag in tags.values() for oracle_id in tag.oracle_ids),
    )


def _printing(row: Sequence[Any]) -> Printing:
    return Printing(*row[:6], regular=bool(row[6]))


def _keep_lowest(lowest: dict[str, int], key: str, *prices: int | None) -> None:
    low = _lowest(*prices, lowest.get(key))
    if low is not None:
        lowest[key] = low


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
