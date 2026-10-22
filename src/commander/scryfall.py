"""Scryfall bulk data: find the daily files, download them and turn them into the local card database.

Scryfall asks for bulk files instead of per-card API calls for lookups like these, and the files live on
data.scryfall.io, which has no rate limit. The API itself is called for the file list when a rebuild is due,
and for the set list on the days it is not.
"""

import gzip
import json
import logging
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path

from commander.carddb import (
    BUILD_SUFFIX,
    CARD_DB_FILE,
    COLORS,
    FACE_SEPARATOR,
    CardDataMeta,
    ParsedCard,
    Printing,
    Role,
    Tag,
    TextGate,
    read_meta,
    write_card_db,
)
from commander.fsutil import new_temp_path
from commander.jsonshape import (
    ShapeError,
    as_list,
    as_object,
    opt_int,
    opt_str,
    req_bool,
    req_float,
    req_list,
    req_object,
    req_str,
    str_list,
)
from commander.net import HTTP_NOT_FOUND, Fetcher, HttpStatusError, NetworkError

log = logging.getLogger(__name__)

API_BASE = "https://api.scryfall.com"
FILE_HOST_PREFIX = "https://data.scryfall.io/"
CARDS_FILE = "default_cards"
TAGS_FILE = "oracle_tags"
MAX_BULK_BYTES = 512 * 1024 * 1024
DOWNLOAD_SUFFIX = ".jsonl.gz.part"
# A temp file this old is from a run that died; younger ones may belong to a sync still running.
STALE_TEMP_SECONDS = 24 * 3600
MAX_LINE_CHARS = 1024 * 1024
MAX_BAD_FRACTION = 0.01
MIN_CARDS = 10_000
MIN_TAGS = 100
# The card data is rebuilt at this age even when no new set has come out, to refresh the prices.
MAX_DATA_AGE = timedelta(days=7)
# Scryfall's set types that bring new cards to Commander, and so make the card data worth rebuilding the day
# they come out: the main releases (core, expansion), reprint sets with new cards (masters, draft_innovation,
# e.g. Modern Horizons 3), Commander products, and the Universes Beyond "eternal" sets. Left out on purpose,
# so that weekly drops do not defeat the weekly cadence: promo, token, memorabilia (art series), box (Secret
# Lair, Challenger and Game Night products), masterpiece (reprints that come out with their parent set),
# funny (Un-sets, whose silver and acorn cards are not legal in Commander), alchemy (digital only) and the
# old reprint products (duel_deck, from_the_vault, premium_deck, starter, treasure_chest and so on). A
# digital set never counts, whatever its type.
NEW_CARD_SET_TYPES = frozenset({"core", "expansion", "masters", "draft_innovation", "commander", "eternal"})
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

# A board wipe clears creatures. A sweeper whose removal tags name only other permanents (Vandalblast, Back to
# Nature, Armageddon) is one only if its text says so, like Wildfire's "4 damage to each creature".
BOARD_WIPE_PHRASE = "each creature"
# Removal-creature (its sub-tags include nonland and permanent), removal-fight and removal-token.
_CREATURE_REMOVAL = (
    "d91e421d-ff87-4aad-a4bf-5aefc51252b6",
    "c90eca7a-ad86-43d5-902e-05db32614b6c",
    "026b939b-37d2-466c-8b36-103c0e0869eb",
)
# Removal of artifacts, auras, battles, enchantments, equipment, lands, noncreatures, planeswalkers,
# spacecraft and vehicles. Their sub-tags include nonland, which reaches creatures and so keeps a card in.
_NONCREATURE_REMOVAL = (
    "ea1ae714-3500-481b-9bdd-f33c57db8678",
    "e8aefad7-dea9-4c48-80a2-8a4ef5996dea",
    "43a67a5e-2053-4c00-8d73-f56751ad6a01",
    "ecee5d4b-f573-4ec8-b918-ad134873005e",
    "70a43edc-86e9-4ad9-abf8-7feefee35d5d",
    "d2a563f5-66f7-442c-91cd-3e8b6c0fc0c5",
    "3c0ab2a1-034f-4f4d-9bf2-893c52ae32f9",
    "591fb358-e9d7-490e-8c15-24aecbbe697e",
    "f79b019e-fdf5-48e0-a3af-bc0e4c8d8c95",
    "9d6ce7cd-6fb8-4273-b663-d44ec6b16483",
)


class ScryfallError(Exception):
    """Scryfall data could not be fetched or made sense of; the existing card data is kept."""


@dataclass(slots=True, frozen=True)
class BulkFile:
    kind: str
    updated_at: str
    url: str
    size: int | None


class RefreshStatus(Enum):
    CURRENT = "current"
    UPDATED = "updated"


@dataclass(slots=True, frozen=True)
class SetInfo:
    code: str
    name: str
    released_at: date
    set_type: str
    digital: bool
    # How many cards Scryfall has for the set; a set announced but not yet previewed has none.
    card_count: int = 0

    @property
    def brings_new_cards(self) -> bool:
        return not self.digital and self.set_type in NEW_CARD_SET_TYPES


@dataclass(slots=True, frozen=True)
class SetCard:
    """A card from a card search of one set: what a preview of it shows and is matched by (ADR 0055)."""

    scryfall_id: str
    oracle_id: str
    name: str
    mana_cost: str
    type_line: str
    # Faces of a two-faced card are joined by FACE_SEPARATOR.
    oracle_text: str
    keywords: tuple[str, ...]
    color_identity: str
    set_code: str
    set_name: str
    released_at: date
    # Legal, restricted or banned in Commander: a card already out, like a reprint, not a preview.
    already_out: bool


@dataclass(slots=True, frozen=True)
class SearchPage:
    cards: tuple[SetCard, ...]
    next_page: str | None


@dataclass(slots=True, frozen=True)
class CardRefresh:
    status: RefreshStatus
    meta: CardDataMeta
    printings: int | None = None
    # Why the data was rebuilt ("7 days old", "new set: Foundations"); empty when it was not.
    reason: str = ""
    # The day the data is old enough to rebuild, if no new set comes out first; None when it was rebuilt.
    next_due: date | None = None


class ScryfallClient:
    def __init__(self, fetcher: Fetcher, api_base: str = API_BASE) -> None:
        self._fetcher = fetcher
        self._api_base = api_base.rstrip("/")
        # The set list is read once per client, as the card data's check and the preview probe both want it
        # in the same sync, from two threads.
        self._sets: list[SetInfo] | None = None
        self._sets_lock = threading.Lock()

    def bulk_files(self) -> dict[str, BulkFile]:
        raw = self._fetcher.get_json(f"{self._api_base}/bulk-data")
        try:
            return parse_bulk_listing(raw)
        except ShapeError as error:
            raise ScryfallError(f"unexpected bulk data list from Scryfall: {error}") from error

    def sets(self) -> list[SetInfo]:
        with self._sets_lock:
            if self._sets is None:
                raw = self._fetcher.get_json(f"{self._api_base}/sets")
                try:
                    self._sets = parse_sets_listing(raw)
                except ShapeError as error:
                    raise ScryfallError(f"unexpected set list from Scryfall: {error}") from error
            return list(self._sets)

    def set_cards(self, code: str, *, before_each: Callable[[], None], max_pages: int) -> list[SetCard]:
        """Every card of one set by Scryfall's card search, a page at a time with before_each called ahead of
        each request (the caller's pacing); [] for a set Scryfall has no cards for yet. A set with more than
        max_pages pages keeps the cards of those pages, as a probe would rather show some than none."""
        cards: list[SetCard] = []
        url: str | None = f"{self._api_base}/cards/search"
        params: dict[str, str | int] | None = {"q": f"e:{code}", "unique": "cards"}
        for _ in range(max_pages):
            if url is None:
                return cards
            before_each()
            try:
                raw = self._fetcher.get_json(url, params)
            except HttpStatusError as error:
                # Scryfall answers a search that finds nothing with a 404.
                if error.status == HTTP_NOT_FOUND and not cards:
                    return []
                raise
            try:
                page = parse_search_page(raw, self._api_base)
            except ShapeError as error:
                raise ScryfallError(f"unexpected card search answer for set {code}: {error}") from error
            cards.extend(page.cards)
            url, params = page.next_page, None
        if url is not None:
            log.warning("the search for set %s has over %d pages; the rest is left out", code, max_pages)
        return cards

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
    """Rebuild the card database when it is missing, a week old or a new set has come out (or always, with
    force), and otherwise leave it with no download. The update times of Scryfall's files do not matter: they
    change daily, for the prices."""
    db_path = folder / CARD_DB_FILE
    current = read_meta(db_path)
    built = None if current is None else _built_at(current)
    if force:
        reason = "forced"
    elif current is None:
        reason = "no usable card data"
    elif built is None:
        reason = "unreadable build time"
    else:
        due = _due_reason(client, built, now())
        if due is None:
            return CardRefresh(RefreshStatus.CURRENT, current, next_due=built.date() + MAX_DATA_AGE)
        reason = due
    files = client.bulk_files()
    cards_file, tags_file = _require(files, CARDS_FILE), _require(files, TAGS_FILE)

    folder.mkdir(parents=True, exist_ok=True)
    _clear_stale_temp_files(folder, now())
    # Their own names, as a CLI sync and the app may both be refreshing into this folder.
    cards_gz = new_temp_path(folder, CARDS_FILE, DOWNLOAD_SUFFIX)
    tags_gz = new_temp_path(folder, TAGS_FILE, DOWNLOAD_SUFFIX)
    try:
        size = f" ({cards_file.size // 1024 // 1024} MB)" if cards_file.size else ""
        on_progress(f"downloading Scryfall card data{size}")
        client.download(cards_file, cards_gz)
        client.download(tags_file, tags_gz)
        on_progress("building the card database")
        tags = read_tags(tags_gz)
        meta = CardDataMeta(cards_file.updated_at, tags_file.updated_at, now().isoformat())
        count = write_card_db(
            db_path, read_cards(cards_gz), role_members(tags), meta, tags=tags, gates=role_gates(tags)
        )
    finally:
        cards_gz.unlink(missing_ok=True)
        tags_gz.unlink(missing_ok=True)
    log.info("card data rebuilt: %d printings", count)
    return CardRefresh(RefreshStatus.UPDATED, meta, count, reason)


def _built_at(meta: CardDataMeta) -> datetime | None:
    """When the database was built, in UTC; None when the stored time is unreadable."""
    try:
        built = datetime.fromisoformat(meta.built_at)
    except ValueError:
        return None
    return built.astimezone(UTC) if built.tzinfo is not None else built.replace(tzinfo=UTC)


def _due_reason(client: ScryfallClient, built: datetime, now: datetime) -> str | None:
    """Why the card data built at `built` is due for a rebuild, or None when it is not."""
    # By calendar day, as the sync report names the day it is due (`next_due`), whatever hour it was built.
    today = now.astimezone(UTC).date()
    age = today - built.date()
    if age >= MAX_DATA_AGE:
        return f"{age.days} days old"
    new_set = _new_set_since(client, built.date(), today)
    return None if new_set is None else f"new set: {new_set.name}"


def _new_set_since(client: ScryfallClient, built_on: date, today: date) -> SetInfo | None:
    """The first set that brings new cards and came out after the build day, up to today. A failed check
    is only logged: the age rule still brings the data up to date."""
    try:
        sets = client.sets()
    except (NetworkError, ScryfallError) as error:
        log.warning("could not read Scryfall's set list (%s); only the data's age counts", error)
        return None
    released = [s for s in sets if s.brings_new_cards and built_on < s.released_at <= today]
    return min(released, key=lambda s: (s.released_at, s.name), default=None)


def _clear_stale_temp_files(folder: Path, now: datetime) -> None:
    """Delete downloads and half-built databases a crashed run left behind; recent ones may be another's."""
    cutoff = now.timestamp() - STALE_TEMP_SECONDS
    for pattern in (f".*{DOWNLOAD_SUFFIX}", f".*{BUILD_SUFFIX}"):
        for path in folder.glob(pattern):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    log.info("removed the stale temp file %s", path.name)
            except OSError as error:
                log.warning("could not remove the stale temp file %s: %s", path.name, error)


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


def parse_sets_listing(raw: object) -> list[SetInfo]:
    obj = as_object(raw, "sets")
    sets: list[SetInfo] = []
    for index, item in enumerate(req_list(obj, "data", "sets")):
        where = f"sets.data[{index}]"
        entry = as_object(item, where)
        released = opt_str(entry, "released_at", where)
        if released is None:
            continue
        try:
            released_at = date.fromisoformat(released)
        except ValueError:
            raise ShapeError(f"{where}.released_at: not a date: {released!r}") from None
        sets.append(
            SetInfo(
                req_str(entry, "code", where),
                req_str(entry, "name", where),
                released_at,
                req_str(entry, "set_type", where),
                req_bool(entry, "digital", where),
                opt_int(entry, "card_count", where) or 0,
            )
        )
    return sets


def parse_search_page(raw: object, api_base: str = API_BASE) -> SearchPage:
    """One page of a card search; tokens and digital cards are left out. A next page that is not on
    Scryfall's API is refused, so a changed answer can never send the probe elsewhere."""
    obj = as_object(raw, "search")
    cards: list[SetCard] = []
    for index, item in enumerate(req_list(obj, "data", "search")):
        card = parse_set_card(item, f"search.data[{index}]")
        if card is not None:
            cards.append(card)
    next_page = opt_str(obj, "next_page", "search") if obj.get("has_more") is True else None
    if next_page is not None and not next_page.startswith(f"{api_base.rstrip('/')}/cards/search?"):
        raise ShapeError(f"search.next_page: not a Scryfall search URL: {next_page}")
    return SearchPage(tuple(cards), next_page)


def parse_set_card(raw: object, where: str) -> SetCard | None:
    card = as_object(raw, where)
    layout = req_str(card, "layout", where)
    if layout in _NON_CARD_LAYOUTS or card.get("digital") is True:
        return None
    faces = [as_object(face, f"{where}.card_faces") for face in as_list(card.get("card_faces") or [], where)]
    front: Mapping[str, object] = faces[0] if faces else {}
    oracle_id = opt_str(card, "oracle_id", where) or opt_str(front, "oracle_id", where)
    if not oracle_id:
        raise ShapeError(f"{where}: no oracle_id")
    released = req_str(card, "released_at", where)
    try:
        released_at = date.fromisoformat(released)
    except ValueError:
        raise ShapeError(f"{where}.released_at: not a date: {released!r}") from None
    legality = req_object(card, "legalities", where).get("commander")
    return SetCard(
        scryfall_id=req_str(card, "id", where),
        oracle_id=oracle_id,
        name=req_str(card, "name", where),
        mana_cost=opt_str(card, "mana_cost", where) or opt_str(front, "mana_cost", where) or "",
        type_line=opt_str(card, "type_line", where)
        or " // ".join(req_str(f, "type_line", where) for f in faces),
        oracle_text=_oracle_text(card, faces, where),
        keywords=str_list(card, "keywords", where),
        color_identity=_colors(str_list(card, "color_identity", where)),
        set_code=req_str(card, "set", where),
        set_name=req_str(card, "set_name", where),
        released_at=released_at,
        already_out=legality in {"legal", "restricted", "banned"},
    )


def parse_card(raw: object) -> ParsedCard | None:
    """One bulk card object; None for objects that never go in a deck (tokens, digital-only printings)."""
    card = as_object(raw, "card")
    where = f"card {card.get('id')}"
    layout = req_str(card, "layout", where)
    if layout in _NON_CARD_LAYOUTS or card.get("digital") is True:
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
        cmc=req_float(card, "cmc", where) if "cmc" in card else req_float(front, "cmc", where),
        type_line=type_line,
        color_identity=_colors(str_list(card, "color_identity", where)),
        colors=_card_colors(card, front, where),
        game_changer=card.get("game_changer") is True,
        commander_legal=req_object(card, "legalities", where).get("commander") == "legal",
        edhrec_rank=opt_int(card, "edhrec_rank", where),
        oracle_text=_oracle_text(card, faces, where),
        keywords=str_list(card, "keywords", where),
        power=opt_str(card, "power", where) or opt_str(front, "power", where),
        toughness=opt_str(card, "toughness", where) or opt_str(front, "toughness", where),
        produced="".join(c for c in "WUBRGC" if c in str_list(card, "produced_mana", where)),
        rarity=opt_str(card, "rarity", where) or "",
        layout=layout,
    )


def _oracle_text(card: Mapping[str, object], faces: list[Mapping[str, object]], where: str) -> str:
    # A two-faced card keeps each face's text on the face; a split or adventure card has both too.
    if faces and all("oracle_text" in face for face in faces):
        return FACE_SEPARATOR.join(opt_str(face, "oracle_text", where) or "" for face in faces)
    return opt_str(card, "oracle_text", where) or ""


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
    return Tag(
        req_str(tag, "id", where),
        req_str(tag, "slug", where),
        str_list(tag, "child_ids", where),
        frozenset(oracle_ids),
    )


def role_members(tags: Mapping[str, Tag]) -> dict[Role, frozenset[str]]:
    """Oracle ids per role, walking each role tag's sub-tags; a role whose tag is gone is left out.

    Sweepers that remove no creatures are left out of board wipes; `role_gates` lets their text decide.
    """
    members: dict[Role, frozenset[str]] = {}
    for role, spec in ROLE_TAGS.items():
        if spec.tag_id not in tags:
            log.warning("Scryfall no longer has the %r tag; %s cannot be counted", spec.slug, role.value)
            continue
        excluded = _subtree(tags, spec.exclude)
        included = _subtree(tags, (spec.tag_id,)) - excluded
        members[role] = _members(tags, included)
    if Role.BOARD_WIPE in members:
        members[Role.BOARD_WIPE] -= _noncreature_sweepers(tags, members[Role.BOARD_WIPE])
    return members


def role_gates(tags: Mapping[str, Tag]) -> tuple[TextGate, ...]:
    """The cards role_members leaves out of a role only for their tags, to be judged by their rules text."""
    board_wipe = ROLE_TAGS[Role.BOARD_WIPE]
    if board_wipe.tag_id not in tags:
        return ()
    sweepers = _members(tags, _subtree(tags, (board_wipe.tag_id,)))
    return (TextGate(Role.BOARD_WIPE, _noncreature_sweepers(tags, sweepers), BOARD_WIPE_PHRASE),)


def _noncreature_sweepers(tags: Mapping[str, Tag], sweepers: frozenset[str]) -> frozenset[str]:
    """Sweepers tagged as removing only noncreature permanents; untagged ones, like Blasphemous Act, stay."""
    hits_creatures = _members(tags, _subtree(tags, _CREATURE_REMOVAL))
    hits_others = _members(tags, _subtree(tags, _NONCREATURE_REMOVAL))
    return (sweepers & hits_others) - hits_creatures


def _members(tags: Mapping[str, Tag], tag_ids: set[str]) -> frozenset[str]:
    return frozenset(oracle_id for tag_id in tag_ids for oracle_id in tags[tag_id].oracle_ids)


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


def _card_colors(card: Mapping[str, object], front: Mapping[str, object], where: str) -> str:
    """The card's colours: on the card, else on its front face, else the coloured symbols of its cost."""
    for source in (card, front):
        if "colors" in source:
            return _colors(str_list(source, "colors", where))
    cost = opt_str(card, "mana_cost", where) or opt_str(front, "mana_cost", where) or ""
    symbols = re.findall(r"\{([^}]*)\}", cost)
    return "".join(color for color in COLORS if any(color in symbol for symbol in symbols))


def _colors(colors: tuple[str, ...]) -> str:
    unknown = set(colors) - set(COLORS)
    if unknown:
        raise ShapeError(f"unknown colours {sorted(unknown)}")
    return "".join(color for color in COLORS if color in colors)
