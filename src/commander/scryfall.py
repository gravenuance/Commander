"""Scryfall bulk data: find the daily files, download them and turn them into the local card database.

Scryfall asks for bulk files instead of per-card API calls for lookups like these, and the files live on
data.scryfall.io, which has no rate limit. The API itself is called once per refresh, for the file list.
"""

import gzip
import json
import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path

from commander.carddb import (
    CARD_DB_FILE,
    COLORS,
    CardDataMeta,
    ParsedCard,
    Printing,
    Role,
    read_meta,
    write_card_db,
)
from commander.jsonshape import (
    ShapeError,
    as_list,
    as_object,
    opt_int,
    opt_str,
    req_list,
    req_object,
    req_str,
    str_list,
)
from commander.net import Fetcher

log = logging.getLogger(__name__)

API_BASE = "https://api.scryfall.com"
FILE_HOST_PREFIX = "https://data.scryfall.io/"
CARDS_FILE = "default_cards"
TAGS_FILE = "oracle_tags"
MAX_BULK_BYTES = 512 * 1024 * 1024
MAX_LINE_CHARS = 1024 * 1024
MAX_BAD_FRACTION = 0.01
MIN_CARDS = 10_000
MIN_TAGS = 100
# Tokens, emblems, art cards and Planechase/Archenemy pieces never sit in a Commander deck.
_NON_CARD_LAYOUTS = frozenset(
    {"token", "double_faced_token", "emblem", "art_series", "vanguard", "scheme", "planar", "augment", "host"}
)

# Gold-bordered World Championship decks and silver-bordered Un-cards are not tournament printings.
_IRREGULAR_BORDERS = frozenset({"gold", "silver"})


@dataclass(slots=True, frozen=True)
class RoleTag:
    """A Tagger tag and its sub-tags, minus excluded subtrees; ids are stable, slugs may change."""

    slug: str
    tag_id: str
    exclude: tuple[str, ...] = ()


ROLE_TAGS: Mapping[Role, RoleTag] = {
    Role.RAMP: RoleTag("ramp", "2f3e4ad7-5e60-41b4-bdbc-653f16869cf6"),
    Role.DRAW: RoleTag("draw", "b6448c45-ce65-4848-aa98-2151e4e07437"),
    Role.REMOVAL: RoleTag("removal", "444f824c-f910-4530-9dbe-ede7a84cd7f9"),
    Role.BOARD_WIPE: RoleTag("sweeper", "3fb7e4fd-5304-4120-b7c4-8a89f70ad3f0"),
    # Land tutors (Farseek, Rampant Growth) are ramp, not the tutoring that brackets care about.
    Role.TUTOR: RoleTag(
        "tutor", "c768d2ec-3264-4a90-a98f-bea8467857d3", exclude=("cc2644e4-57c6-46f7-a69c-0cd82dbf2e9d",)
    ),
    Role.PROTECTION: RoleTag("protection", "6e2cdc7c-b02c-4b59-a171-f93723721b79"),
    Role.RECURSION: RoleTag("recursion", "82b824ad-648f-467f-a190-2e0fa9a795d2"),
    Role.COUNTERSPELL: RoleTag("counterspell", "690fc968-48ba-4854-a948-3db6bf19d3a9"),
    Role.EXTRA_TURN: RoleTag("extra-turn", "03b17ebf-f5d3-4063-bfd4-1ae156a16a8f"),
    Role.MASS_LAND_DENIAL: RoleTag("mass-land-denial", "cd12a44c-1aee-4ece-b8ea-3eb118ef0230"),
}


class ScryfallError(Exception):
    """Scryfall data could not be fetched or made sense of; the existing card data is kept."""


@dataclass(slots=True, frozen=True)
class BulkFile:
    kind: str
    updated_at: str
    url: str
    size: int | None


@dataclass(slots=True, frozen=True)
class Tag:
    id: str
    child_ids: tuple[str, ...]
    oracle_ids: frozenset[str]


class RefreshStatus(Enum):
    CURRENT = "current"
    UPDATED = "updated"


@dataclass(slots=True, frozen=True)
class CardRefresh:
    status: RefreshStatus
    meta: CardDataMeta
    printings: int | None = None


class ScryfallClient:
    def __init__(self, fetcher: Fetcher, api_base: str = API_BASE) -> None:
        self._fetcher = fetcher
        self._api_base = api_base.rstrip("/")

    def bulk_files(self) -> dict[str, BulkFile]:
        raw = self._fetcher.get_json(f"{self._api_base}/bulk-data")
        try:
            return parse_bulk_listing(raw)
        except ShapeError as error:
            raise ScryfallError(f"unexpected bulk data list from Scryfall: {error}") from error

    def download(self, file: BulkFile, dest: Path) -> int:
        if file.size is not None and file.size > MAX_BULK_BYTES:
            raise ScryfallError(
                f"Scryfall {file.kind} is {file.size // 1024 // 1024} MB, over the size limit"
            )
        return self._fetcher.download(file.url, dest, max_bytes=MAX_BULK_BYTES)


def refresh_card_db(
    client: ScryfallClient,
    folder: Path,
    *,
    now: Callable[[], datetime],
    force: bool = False,
    on_progress: Callable[[str], None] = lambda _: None,
) -> CardRefresh:
    """Rebuild the card database when Scryfall has published newer files (or always, with force)."""
    files = client.bulk_files()
    cards_file, tags_file = _require(files, CARDS_FILE), _require(files, TAGS_FILE)
    db_path = folder / CARD_DB_FILE
    current = read_meta(db_path)
    if (
        not force
        and current is not None
        and current.cards_updated_at == cards_file.updated_at
        and current.tags_updated_at == tags_file.updated_at
    ):
        return CardRefresh(RefreshStatus.CURRENT, current)

    folder.mkdir(parents=True, exist_ok=True)
    cards_gz = folder / f".{CARDS_FILE}.jsonl.gz.part"
    tags_gz = folder / f".{TAGS_FILE}.jsonl.gz.part"
    try:
        size = f" ({cards_file.size // 1024 // 1024} MB)" if cards_file.size else ""
        on_progress(f"downloading Scryfall card data{size}")
        client.download(cards_file, cards_gz)
        client.download(tags_file, tags_gz)
        on_progress("building the card database")
        members = role_members(read_tags(tags_gz))
        meta = CardDataMeta(cards_file.updated_at, tags_file.updated_at, now().isoformat())
        count = write_card_db(db_path, read_cards(cards_gz), members, meta)
    finally:
        cards_gz.unlink(missing_ok=True)
        tags_gz.unlink(missing_ok=True)
    log.info("card data rebuilt: %d printings", count)
    return CardRefresh(RefreshStatus.UPDATED, meta, count)


def parse_bulk_listing(raw: object) -> dict[str, BulkFile]:
    obj = as_object(raw, "bulk-data")
    files: dict[str, BulkFile] = {}
    for index, item in enumerate(req_list(obj, "data", "bulk-data")):
        where = f"bulk-data.data[{index}]"
        entry = as_object(item, where)
        url = opt_str(entry, "jsonl_download_uri", where)
        if url is None:
            continue
        if not url.startswith(FILE_HOST_PREFIX):
            raise ShapeError(f"{where}.jsonl_download_uri: not a Scryfall file URL: {url}")
        kind = req_str(entry, "type", where)
        files[kind] = BulkFile(
            kind, req_str(entry, "updated_at", where), url, opt_int(entry, "compressed_size", where)
        )
    return files


def parse_card(raw: object) -> ParsedCard | None:
    """One bulk card object; None for objects that never go in a deck (tokens, digital-only printings)."""
    card = as_object(raw, "card")
    where = f"card {card.get('id')}"
    if req_str(card, "layout", where) in _NON_CARD_LAYOUTS or card.get("digital") is True:
        return None
    faces = [as_object(face, f"{where}.card_faces") for face in as_list(card.get("card_faces") or [], where)]
    front: Mapping[str, object] = faces[0] if faces else {}
    # Reversible cards keep the oracle id, cost and cmc on each face instead of the card.
    oracle_id = opt_str(card, "oracle_id", where) or opt_str(front, "oracle_id", where)
    if not oracle_id:
        raise ShapeError(f"{where}: no oracle_id")
    type_line = opt_str(card, "type_line", where) or " // ".join(
        req_str(f, "type_line", where) for f in faces
    )
    prices = req_object(card, "prices", where)
    return ParsedCard(
        printing=Printing(
            scryfall_id=req_str(card, "id", where),
            oracle_id=oracle_id,
            set_code=req_str(card, "set", where),
            collector_number=req_str(card, "collector_number", where),
            eur_cents=_cents(prices, "eur", where),
            eur_foil_cents=_cents(prices, "eur_foil", where),
            regular=_regular(card, where),
        ),
        name=req_str(card, "name", where),
        mana_cost=opt_str(card, "mana_cost", where) or opt_str(front, "mana_cost", where) or "",
        cmc=_number(card, "cmc", where) if "cmc" in card else _number(front, "cmc", where),
        type_line=type_line,
        color_identity=_colors(str_list(card, "color_identity", where)),
        game_changer=card.get("game_changer") is True,
        commander_legal=req_object(card, "legalities", where).get("commander") == "legal",
        edhrec_rank=opt_int(card, "edhrec_rank", where),
    )


def read_cards(path: Path) -> Iterator[ParsedCard]:
    """Stream the cards bulk file; raises after the last card when too much of it was unreadable."""
    return _parse_lines(path, parse_card, MIN_CARDS)


def read_tags(path: Path) -> dict[str, Tag]:
    return {tag.id: tag for tag in _parse_lines(path, parse_tag, MIN_TAGS)}


def parse_tag(raw: object) -> Tag | None:
    """An oracle tag; None for art tags."""
    tag = as_object(raw, "tag")
    where = f"tag {tag.get('slug')}"
    if req_str(tag, "type", where) != "oracle":
        return None
    oracle_ids: set[str] = set()
    for index, item in enumerate(req_list(tag, "taggings", where)):
        oracle_id = opt_str(as_object(item, f"{where}.taggings[{index}]"), "oracle_id", where)
        if oracle_id:
            oracle_ids.add(oracle_id)
    return Tag(req_str(tag, "id", where), str_list(tag, "child_ids", where), frozenset(oracle_ids))


def role_members(tags: Mapping[str, Tag]) -> dict[Role, frozenset[str]]:
    """Oracle ids per role, walking each role tag's sub-tags; a role whose tag is gone is left out."""
    members: dict[Role, frozenset[str]] = {}
    for role, spec in ROLE_TAGS.items():
        if spec.tag_id not in tags:
            log.warning("Scryfall no longer has the %r tag; %s cannot be counted", spec.slug, role.value)
            continue
        excluded = _subtree(tags, spec.exclude)
        included = _subtree(tags, (spec.tag_id,)) - excluded
        members[role] = frozenset(oracle_id for tag_id in included for oracle_id in tags[tag_id].oracle_ids)
    return members


def _subtree(tags: Mapping[str, Tag], roots: tuple[str, ...]) -> set[str]:
    seen: set[str] = set()
    stack = [root for root in roots if root in tags]
    while stack:
        tag_id = stack.pop()
        if tag_id in seen:
            continue
        seen.add(tag_id)
        stack.extend(child for child in tags[tag_id].child_ids if child in tags)
    return seen


def _parse_lines[T](path: Path, parse: Callable[[object], T | None], minimum: int) -> Iterator[T]:
    total = bad = 0
    for line_number, line in _lines(path):
        total += 1
        try:
            item = parse(json.loads(line))
        except (json.JSONDecodeError, ShapeError) as error:
            bad += 1
            _log_bad(bad, path, line_number, error)
            continue
        if item is not None:
            yield item
    _check_quality(path, total, bad, minimum)


def _lines(path: Path) -> Iterator[tuple[int, str]]:
    """Non-blank lines of a gzipped JSONL file, refusing absurdly long ones."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as lines:
            line_number = 0
            while line := lines.readline(MAX_LINE_CHARS + 1):
                line_number += 1
                if len(line) > MAX_LINE_CHARS:
                    raise ScryfallError(
                        f"{path.name}: line {line_number} is over {MAX_LINE_CHARS} characters"
                    )
                if line.strip():
                    yield line_number, line
    except (OSError, EOFError, UnicodeDecodeError) as error:
        raise ScryfallError(f"cannot read downloaded {path.name}: {error}") from error


def _log_bad(count: int, path: Path, line_number: int, error: ValueError) -> None:
    if count <= 3:  # noqa: PLR2004 — a few examples are enough to diagnose a format change
        log.warning("%s line %d skipped: %s", path.name, line_number, error)


def _check_quality(path: Path, total: int, bad: int, minimum: int) -> None:
    if total - bad < minimum:
        raise ScryfallError(f"{path.name} has only {total - bad} usable entries; expected at least {minimum}")
    if bad > total * MAX_BAD_FRACTION:
        raise ScryfallError(
            f"{path.name}: {bad} of {total} entries unreadable; Scryfall's format may have changed"
        )


def _require(files: Mapping[str, BulkFile], kind: str) -> BulkFile:
    if kind not in files:
        raise ScryfallError(f"Scryfall no longer lists the {kind} bulk file")
    return files[kind]


def _cents(prices: Mapping[str, object], key: str, where: str) -> int | None:
    text = opt_str(prices, key, where)
    if text is None:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation as error:
        raise ShapeError(f"{where}.prices.{key}: not a price: {text!r}") from error
    if not amount.is_finite() or amount < 0:
        raise ShapeError(f"{where}.prices.{key}: not a price: {text!r}")
    return int((amount * 100).to_integral_value())


def _regular(card: Mapping[str, object], where: str) -> bool:
    """A printing sanctioned play accepts; a missing field counts as the usual value."""
    border = opt_str(card, "border_color", where)
    return (
        border not in _IRREGULAR_BORDERS
        and card.get("oversized") is not True
        and opt_str(card, "set_type", where) != "memorabilia"
    )


def _number(obj: Mapping[str, object], key: str, where: str) -> float:
    value = obj.get(key)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ShapeError(f"{where}.{key}: expected a number")
    return float(value)


def _colors(colors: tuple[str, ...]) -> str:
    unknown = set(colors) - set(COLORS)
    if unknown:
        raise ShapeError(f"unknown colours {sorted(unknown)}")
    return "".join(color for color in COLORS if color in colors)
