"""Deck boards: cards under consideration for one deck, their research, decisions and history.

Each deck folder holds board.json (the cards now) and activity.jsonl (every change, oldest first), so the
desktop app, the CLI and Claude all work from the same files and can catch up on each other (ADR 0006).
"""

import hashlib
import json
import logging
import os
import secrets
import shutil
import threading
import time
from collections import Counter
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from functools import cached_property
from pathlib import Path
from typing import Protocol, Self

from commander.answers import card_key
from commander.carddb import CardDb, CardInfo
from commander.copylimit import copy_limit, copy_limits
from commander.fsutil import write_text_atomic
from commander.jsonshape import (
    ShapeError,
    as_object,
    opt_float,
    opt_int,
    opt_str,
    req_int,
    req_list,
    req_object,
    req_str,
)
from commander.models import COMMANDER_CATEGORY, Deck
from commander.plan import MAX_CATEGORY_LENGTH, AddCard, Plan, RemoveCard
from commander.suggest import Suggestions, deck_identity, name_keys

log = logging.getLogger(__name__)

BOARD_FILE = "board.json"
ACTIVITY_FILE = "activity.jsonl"
LOCK_FILE = ".board.lock"
BOARD_VERSION = 4
MAX_REASON = 4000
MAX_NOTE = 1000
MAX_REPLACE = 10
MAX_CARDS = 500
# A deck holds at most 99 cards besides its commander.
MAX_COPIES = 99
MAX_LOG_LINE = 16 * 1024
# How much of the end of the activity log is read at a time when only its newest lines are wanted.
TAIL_BLOCK_BYTES = 64 * 1024
LOCK_TIMEOUT_SECONDS = 10.0
# A lock file older than this was left by a crashed writer; a real update takes milliseconds.
STALE_LOCK_SECONDS = 60.0
# How far a turned-down card's share of the lists read must rise before it is offered again.
RETURN_RISE = 0.20
# Why a sync closes an open card the main deck holds.
ALREADY_IN_THE_DECK = "already in the deck"
_LOCK_POLL_SECONDS = 0.05
# Basic land names, Wastes included, for telling a basic swap; manabase and manaplan list only the five
# coloured basics, by type or colour, because Wastes has no land type and makes no colour.
_BASIC_LAND_NAMES = frozenset({"plains", "island", "swamp", "mountain", "forest", "wastes"})
# One thread lock per deck folder, so a lock another process holds on one deck holds up only that deck.
_THREAD_LOCKS: dict[Path, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(folder: Path) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(folder.resolve(), threading.Lock())


class BoardError(Exception):
    """The board cannot do what was asked; the message says why, in a few plain words."""


class BoardIOError(BoardError):
    """The board's files or lock failed (not the request): the CLI exits 1 for it, not 2."""


class NothingQueuedError(BoardError):
    """A save or plan was asked for with no card queued: a failed run, not a usage error."""


class Actor(Enum):
    YOU = "you"
    CLAUDE = "claude"
    CMDR = "cmdr"
    # Old boards hold cards picked up from the Sideboard or Maybeboard (ADR 0006); nothing makes more now.
    ARCHIDEKT = "archidekt"


class Status(Enum):
    OPEN = "open"
    ADD = "add"
    REJECTED = "rejected"
    APPLIED = "applied"


@dataclass(slots=True, frozen=True)
class ReplaceOption:
    """A deck card that could make room for the board card, and why."""

    name: str
    reason: str = ""


class Origin(Enum):
    """Where an idea compiled for Considering came from (ADR 0028); None for anything else."""

    # Staples of the commander's tournament lists (brackets 4 and 5).
    META = "meta"
    # Staples of the most-viewed recent Archidekt decks for the commander, at the deck's bracket and above.
    DECKS = "archidekt decks"
    # Staples of recent Archidekt decks led by the commanders the deck's profile names as alike (ADR 0028).
    ADJACENT = "adjacent decks"
    # Staples of recent Archidekt decks of any commander carrying one of the deck's theme tags.
    TAGGED = "tagged decks"


@dataclass(slots=True, frozen=True)
class OriginPool:
    """The pool of decks an alike or tagged idea was found in: how many of its `total` decks play the card."""

    count: int
    total: int

    def __post_init__(self) -> None:
        if not 0 <= self.count <= self.total or self.total < 1:
            raise ValueError(f"pool: {self.count} of {self.total} decks is not a share")

    @property
    def share(self) -> float:
        return self.count / self.total


@dataclass(slots=True, frozen=True)
class BoardCard:
    id: str
    name: str
    oracle_id: str
    source: Actor
    reason: str
    replace: tuple[ReplaceOption, ...]
    status: Status
    # The deck card chosen to make room when the card is added; None adds it without a cut.
    choice: str | None
    note: str
    added_at: str
    updated_at: str
    # For a turned-down card: its share of the lists read when it was turned down; None when unknown.
    rejected_share: float | None = None
    origin: Origin | None = None
    # For a tagged idea: the deck tag whose decks play the card.
    origin_tag: str | None = None
    # For an alike or tagged idea: how many of the decks it was found in play it; None on a board from before.
    origin_pool: OriginPool | None = None
    # Copies to add, each taking a copy of the chosen cut: a basic land swap moves several at once.
    copies: int = 1
    # Set when the user reopened a turned-down card: the automatic re-judge leaves it until they decide it.
    reconsidered: bool = False
    # The Archidekt category a card being added is filed under; None files it by its card type.
    category: str | None = None
    # For a basic land swap a save was sent for: the copies the deck held then. A basic is added to what the
    # deck holds, so without this a send that went unmarked (read-back failed, timeout, board lock) would add
    # the copies again on the next save; the deck holding this many plus `copies` means it landed.
    sent_held: int | None = None
    # The copies the deck held of the chosen cut then; None when nothing is cut or the send was noted before
    # this was kept. A swap landed only if the add rose and the cut fell, as a Mountain added by hand moves
    # the first alone.
    sent_cut_held: int | None = None

    def with_status(self, status: Status, **changes: object) -> Self:
        """The card in another status with `changes`; leaving ADD drops the count held when sent."""
        for field in ("sent_held", "sent_cut_held"):
            changes.setdefault(field, getattr(self, field) if status is Status.ADD else None)
        return replace(self, status=status, **changes)

    @property
    def relabellable(self) -> bool:
        """Whether lists may name where the card comes from: a compiled idea or an old Maybeboard pick-up,
        not one that already has a pool of decks behind it."""
        return self.source in {Actor.CMDR, Actor.ARCHIDEKT} and self.origin not in {
            Origin.ADJACENT,
            Origin.TAGGED,
        }

    def __post_init__(self) -> None:
        if not 1 <= self.copies <= MAX_COPIES:
            raise ValueError(f"{self.name}: copies must be 1-{MAX_COPIES}, got {self.copies}")
        if self.choice is not None and self.status not in {Status.ADD, Status.APPLIED}:
            raise ValueError(f"{self.name}: only a card being added has a chosen cut")
        if self.rejected_share is not None and self.status is not Status.REJECTED:
            raise ValueError(f"{self.name}: only a turned-down card keeps its share")
        if self.origin_tag is not None and self.origin is not Origin.TAGGED:
            raise ValueError(f"{self.name}: only a tagged idea names a tag")
        if self.origin_pool is not None and self.origin not in {Origin.ADJACENT, Origin.TAGGED}:
            raise ValueError(f"{self.name}: only an alike or tagged idea has a pool")
        if self.reconsidered and self.status is not Status.OPEN:
            raise ValueError(f"{self.name}: only an open card can be reconsidered")
        if self.category is not None and self.status not in {Status.ADD, Status.APPLIED}:
            raise ValueError(f"{self.name}: only a card being added has a category")
        if self.sent_held is not None and (self.status is not Status.ADD or self.sent_held < 0):
            raise ValueError(f"{self.name}: only a card being added has a count held when sent")
        if self.sent_cut_held is not None and (self.sent_held is None or self.sent_cut_held < 0):
            raise ValueError(f"{self.name}: a cut's count held when sent needs the added card's too")


@dataclass(slots=True, frozen=True)
class CountChange:
    """A deck card queued to end up with `count` copies (0 takes it out), from `was` when it was queued: the
    decklist's + and - (ADR 0043). Gone from the board once the deck holds that many.

    With a category, the change is to the copies filed under that category outside the deck, such as the
    Maybeboard, and only takes them off (ADR 0052); without one, to the deck's own copies.
    """

    name: str
    was: int
    count: int
    by: Actor
    at: str
    category: str | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.count <= MAX_COPIES or not 1 <= self.was <= MAX_COPIES:
            raise ValueError(f"{self.name}: copies must be 0-{MAX_COPIES}, from 1-{MAX_COPIES}")
        if self.count == self.was:
            raise ValueError(f"{self.name}: a count change must change the count")
        if self.category is not None and self.count != 0:
            raise ValueError(f"{self.name}: a card outside the deck only comes off {self.category}")

    @property
    def key(self) -> tuple[str, str | None]:
        """The card and where its copies are: one change per card in the deck and per outside category."""
        return change_key(self.name, self.category)

    def met_by(self, held: int) -> bool:
        """Whether a deck holding `held` copies needs nothing more: a decrease is at most `count` copies, an
        increase at least `count`, so a copy cut or added on Archidekt meanwhile never turns one around. A
        card the deck no longer holds is met either way: bringing it back is a separate choice."""
        if held == 0:
            return True
        return held <= self.count if self.count < self.was else held >= self.count


@dataclass(slots=True, frozen=True)
class Relabel:
    """What today's lists make of an idea's label: the source it comes from, if any list plays it, and why.

    Without an origin only the reason changes.
    """

    origin: Origin | None
    reason: str


@dataclass(slots=True, frozen=True)
class Activity:
    at: str
    actor: Actor
    action: str
    card: str | None
    detail: str


@dataclass(slots=True, frozen=True)
class Board:
    cards: tuple[BoardCard, ...]
    counts: tuple[CountChange, ...] = ()

    def find(self, key: str) -> BoardCard | None:
        """A card by its id, or by its name (any case; the front face is enough)."""
        wanted = key.strip().casefold()
        for card in self.cards:
            if card.id == wanted or wanted in name_keys(card.name):
                return card
        return None

    @property
    def queued(self) -> tuple[BoardCard, ...]:
        return tuple(card for card in self.cards if card.status is Status.ADD)

    @property
    def rejected_names(self) -> tuple[str, ...]:
        return tuple(card.name for card in self.cards if card.status is Status.REJECTED)

    @property
    def pending(self) -> int:
        """How many changes the next save makes: the cards queued to add and the count changes."""
        return len(self.queued) + len(self.counts)

    def count_of(self, name: str, category: str | None = None) -> CountChange | None:
        """The queued count change of a deck card, by name (any case; the front face is enough), or with a
        category, of the card's copies filed under that category outside the deck."""
        keys = name_keys(name.strip())
        wanted = _folded(category)
        return next(
            (c for c in self.counts if name_keys(c.name) & keys and _folded(c.category) == wanted), None
        )


def change_key(name: str, category: str | None) -> tuple[str, str | None]:
    """What tells count changes apart: the card, and the outside category it comes off (None: the deck)."""
    return card_key(name), _folded(category)


def _folded(category: str | None) -> str | None:
    return category.strip().casefold() if category is not None else None


def board_stamp(folder: Path) -> str:
    """Changes whenever the board, its activity or the deck snapshot changes; cheap enough to poll."""
    return files_stamp(folder, (BOARD_FILE, ACTIVITY_FILE, "deck.json", "profile.toml"))


def board_digest(folder: Path) -> str:
    """A hash of board.json's content, to tell whether the board changed between two looks."""
    try:
        data = (folder / BOARD_FILE).read_bytes()
    except FileNotFoundError:
        return "-"
    except OSError as error:
        raise BoardIOError(f"cannot read {folder / BOARD_FILE}: {error}") from error
    return hashlib.sha256(data).hexdigest()


def files_stamp(folder: Path, names: Iterable[str]) -> str:
    """Changes whenever one of the folder's files does: their times and sizes, "-" for a missing one."""
    parts: list[str] = []
    for name in names:
        try:
            stat = (folder / name).stat()
        except FileNotFoundError:
            parts.append("-")
            continue
        parts.append(f"{stat.st_mtime_ns}:{stat.st_size}")
    return "|".join(parts)


def read_board(folder: Path) -> Board:
    return _read_stored(folder)[0]


def _read_stored(folder: Path) -> tuple[Board, int]:
    """The board and the format version its file was written in (BOARD_VERSION when there is no file)."""
    path = folder / BOARD_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Board(()), BOARD_VERSION
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BoardIOError(f"cannot read {path}: {error}") from error
    try:
        tree = as_object(raw, "board")
        return parse_board(_migrate(tree)), req_int(tree, "version", "board")
    except (ShapeError, ValueError) as error:
        raise BoardIOError(f"{path}: {error}") from error


def read_activity(folder: Path, *, since: str | None = None) -> list[Activity]:
    """The board's history, oldest first; since keeps entries strictly after that UTC timestamp."""
    path = folder / ACTIVITY_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as error:
        raise BoardIOError(f"cannot read {path}: {error}") from error
    entries: list[Activity] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = _activity_from_json(json.loads(line[:MAX_LOG_LINE]))
        except (json.JSONDecodeError, ShapeError, ValueError) as error:
            # One damaged line must not hide the rest of the history.
            log.warning("skipping unreadable line %d of %s: %s", number, path, error)
            continue
        if since is None or entry.at > since:
            entries.append(entry)
    return entries


def read_recent_activity(folder: Path, count: int) -> list[Activity]:
    """The last `count` entries of the board's history, oldest first.

    It reads the file from its end, a block at a time, until it has them, so a log that has grown for months
    costs what is shown, not its length. A damaged line is skipped, as `read_activity` does.
    """
    path = folder / ACTIVITY_FILE
    newest: list[Activity] = []
    try:
        with path.open("rb") as handle:
            position = handle.seek(0, os.SEEK_END)
            # The start of the oldest line read so far; the next block completes it.
            head = b""
            while position > 0 and len(newest) < count:
                step = min(TAIL_BLOCK_BYTES, position)
                position -= step
                handle.seek(position)
                lines = (handle.read(step) + head).split(b"\n")
                head = lines[0]
                _take_newest(newest, reversed(lines[1:]), count, path)
            if position == 0 and len(newest) < count:
                _take_newest(newest, [head], count, path)
    except FileNotFoundError:
        return []
    except OSError as error:
        raise BoardIOError(f"cannot read {path}: {error}") from error
    newest.reverse()
    return newest


def _take_newest(newest: list[Activity], lines: Iterable[bytes], count: int, path: Path) -> None:
    """Add the entries of lines, newest first, to newest until it holds count of them."""
    for raw in lines:
        if len(newest) >= count:
            return
        try:
            text = raw.decode("utf-8")
            if not text.strip():
                continue
            newest.append(_activity_from_json(json.loads(text[:MAX_LOG_LINE])))
        except (UnicodeDecodeError, json.JSONDecodeError, ShapeError, ValueError) as error:
            # One damaged line must not hide the rest of the history.
            log.warning("skipping an unreadable line of %s: %s", path, error)


def parse_board(raw: Mapping[str, object]) -> Board:
    cards: list[BoardCard] = []
    for index, item in enumerate(req_list(raw, "cards", "board")):
        where = f"board.cards[{index}]"
        card = as_object(item, where)
        cards.append(
            BoardCard(
                id=req_str(card, "id", where),
                name=req_str(card, "name", where),
                oracle_id=req_str(card, "oracleId", where),
                source=Actor(req_str(card, "source", where)),
                reason=req_str(card, "reason", where),
                replace=tuple(
                    ReplaceOption(req_str(o, "name", where), req_str(o, "reason", where))
                    for o in (as_object(r, where) for r in req_list(card, "replace", where))
                ),
                status=Status(req_str(card, "status", where)),
                choice=opt_str(card, "choice", where),
                note=req_str(card, "note", where),
                added_at=req_str(card, "addedAt", where),
                updated_at=req_str(card, "updatedAt", where),
                rejected_share=opt_float(card, "rejectedShare", where),
                origin=_origin(opt_str(card, "origin", where)),
                origin_tag=opt_str(card, "originTag", where),
                origin_pool=_pool(card, where),
                copies=opt_int(card, "copies", where) or 1,
                reconsidered=card.get("reconsidered") is True,
                category=opt_str(card, "category", where),
                sent_held=opt_int(card, "sentHeld", where),
                sent_cut_held=opt_int(card, "sentCutHeld", where),
            )
        )
    counts: list[CountChange] = []
    for index, item in enumerate(req_list(raw, "counts", "board") if "counts" in raw else ()):
        where = f"board.counts[{index}]"
        change = as_object(item, where)
        counts.append(
            CountChange(
                name=req_str(change, "name", where),
                was=req_int(change, "was", where),
                count=req_int(change, "count", where),
                by=Actor(req_str(change, "by", where)),
                at=req_str(change, "at", where),
                category=opt_str(change, "category", where),
            )
        )
    return Board(tuple(cards), tuple(counts))


def _origin(value: str | None) -> Origin | None:
    return Origin(value) if value else None


def _pool(card: Mapping[str, object], where: str) -> OriginPool | None:
    if card.get("originPool") is None:
        return None
    found = req_object(card, "originPool", where)
    return OriginPool(
        req_int(found, "count", f"{where}.originPool"), req_int(found, "total", f"{where}.originPool")
    )


def board_json(board: Board) -> dict[str, object]:
    return {
        "version": BOARD_VERSION,
        "cards": [card_json(card) for card in board.cards],
        "counts": [count_json(change) for change in board.counts],
    }


def count_json(change: CountChange) -> dict[str, object]:
    return {
        "name": change.name,
        "was": change.was,
        "count": change.count,
        "by": change.by.value,
        "at": change.at,
        "category": change.category,
    }


def card_json(card: BoardCard) -> dict[str, object]:
    return {
        "id": card.id,
        "name": card.name,
        "oracleId": card.oracle_id,
        "source": card.source.value,
        "reason": card.reason,
        "replace": [{"name": o.name, "reason": o.reason} for o in card.replace],
        "status": card.status.value,
        "choice": card.choice,
        "note": card.note,
        "addedAt": card.added_at,
        "updatedAt": card.updated_at,
        "rejectedShare": card.rejected_share,
        "origin": card.origin.value if card.origin else None,
        "originTag": card.origin_tag,
        "originPool": {"count": card.origin_pool.count, "total": card.origin_pool.total}
        if card.origin_pool
        else None,
        "copies": card.copies,
        "reconsidered": card.reconsidered,
        "category": card.category,
        "sentHeld": card.sent_held,
        "sentCutHeld": card.sent_cut_held,
    }


def activity_json(entry: Activity) -> dict[str, object]:
    return {
        "at": entry.at,
        "actor": entry.actor.value,
        "action": entry.action,
        "card": entry.card,
        "detail": entry.detail,
    }


def board_plan(deck: Deck, board: Board) -> Plan | None:
    """The plan for the next save; None when it would change nothing.

    It adds every queued card, under its category when it has one, and removes its chosen cut. A cut the
    deck holds more copies of, such as a basic land, loses one copy for each copy a card adds (most add one)
    instead. Each count change asks for its count: at least that many when it raises the count, at most that
    many when it lowers it. A change to the copies outside the deck takes them off their category only.

    The counts come from deck, so it should be the deck as it is on Archidekt now: a queued card that deck
    already holds (other than a basic land) is done, and neither adds nor cuts; so is a count change the
    deck already meets (`CountChange.met_by`), including a change to a card the deck no longer holds.
    """
    held = _held(deck)
    done = already_added(deck, board)
    queued = tuple(card for card in board.queued if card not in done)
    met = counts_met(deck, board)
    counts = tuple(change for change in board.counts if change not in met)
    if not queued and not counts:
        return None
    adds = [AddCard(card.name, _add_target(held, card), category=card.category) for card in queued]
    cuts = list(_cuts(held, queued))
    for change in counts:
        if change.count > change.was:
            adds.append(AddCard(change.name, change.count))
        else:
            cuts.append(RemoveCard(change.name, keep=change.count, category=change.category))
    return Plan(str(deck.id), tuple(adds), tuple(cuts))


@dataclass(slots=True, frozen=True)
class HeldAtSend:
    """The copies a deck held of a basic land swap's added card and of its cut (None without a cut)."""

    added: int
    cut: int | None


def basic_land_swaps(deck: Deck, board: Board) -> dict[str, HeldAtSend]:
    """The copies deck holds for each queued basic land the next save adds to, by card id: what
    `BoardService.record_sending` keeps, because the plan adds to the copies held (`board_plan`)."""
    held = _held(deck)
    done = already_added(deck, board)
    return {
        card.id: HeldAtSend(
            held[card_key(card.name)], held[card_key(card.choice)] if card.choice is not None else None
        )
        for card in board.queued
        if _is_basic_land(card.name) and card not in done
    }


def counts_met(deck: Deck, board: Board) -> tuple[CountChange, ...]:
    """The count changes deck already meets (`CountChange.met_by`): nothing is left to do for them."""
    return tuple(
        change for change in board.counts if change.met_by(copies_held(deck, change.name, change.category))
    )


def copies_held(deck: Deck, name: str, category: str | None = None) -> int:
    """The copies of a card the deck holds, or with a category, those filed under it outside the deck."""
    if category is None:
        return _held(deck)[card_key(name)]
    key = change_key(name, category)
    return sum(
        card.quantity
        for card in deck.cards
        if not card.in_deck and change_key(card.name, card.primary_category) == key
    )


def already_added(deck: Deck, board: Board) -> tuple[BoardCard, ...]:
    """The queued cards that deck already holds: nothing is left to do for them. A basic land counts only
    once the deck holds the copies it added, on top of those it held when a save was sent (`sent_held`)."""
    return tuple(card for card in board.queued if _holds(deck, card))


def held_keys(deck: Deck, db: CardDb) -> frozenset[str]:
    """The `card_key` of every card the main deck holds that a deck may run only one of: an open card of one
    of these is no idea, so Considering never lists it (ADR 0046). Basic lands and cards whose rules text
    allows more copies stay ideas, since the mana tab and the decklist adjust their counts."""
    limits = copy_limits(db.resolve(deck.main_cards), db)
    return frozenset(key for key in _held(deck) if limits.get(key, None if _is_basic_land(key) else 1) == 1)


def _holds(deck: Deck, card: BoardCard) -> bool:
    """Whether deck already holds every copy the card adds. A basic land is added to what the deck holds, so
    only a send recorded in `sent_held` can show it done, and only if the cut fell too (`sent_cut_held`):
    a copy added by hand since raises the count alone."""
    counts = _held(deck)
    if not _is_basic_land(card.name):
        return counts[card_key(card.name)] >= card.copies
    if card.sent_held is None or counts[card_key(card.name)] < card.sent_held + card.copies:
        return False
    if card.choice is None or card.sent_cut_held is None:
        return True
    return counts[card_key(card.choice)] <= card.sent_cut_held - card.copies


def _add_target(held: Counter[str], card: BoardCard) -> int:
    # A basic land swap adds to the copies held; any other card is wanted in `copies` copies in all.
    return card.copies + (held[card_key(card.name)] if _is_basic_land(card.name) else 0)


def _is_basic_land(name: str) -> bool:
    return card_key(name).removeprefix("snow-covered ") in _BASIC_LAND_NAMES


def _held(deck: Deck) -> Counter[str]:
    held = Counter[str]()
    for card in deck.main_cards:
        held[card_key(card.name)] += card.quantity
    return held


def _cuts(held: Counter[str], queued: Sequence[BoardCard]) -> tuple[RemoveCard, ...]:
    named = Counter[str]()
    for card in queued:
        if card.choice:
            named[card_key(card.choice)] += card.copies
    cuts: dict[str, RemoveCard] = {}
    for card in queued:
        if card.choice and (key := card_key(card.choice)) not in cuts:
            left = held[key] - named[key]
            cuts[key] = RemoveCard(card.choice, keep=max(left, 0))
    return tuple(cuts.values())


class ActivityNotes(Protocol):
    """What logs something that happened to a deck without changing its board."""

    def note(self, actor: Actor, action: str, detail: str, card: str | None = None) -> None: ...


class BoardNotes:
    """Writes lines to one deck's activity log under the board lock, and needs no card data, so a write that
    reads none (a label change) can log without holding the card database (ADR 0042)."""

    def __init__(self, folder: Path, *, now: Callable[[], datetime]) -> None:
        self._folder = folder
        self._now = now

    def note(self, actor: Actor, action: str, detail: str, card: str | None = None) -> None:
        entry = _note_line(self._now().isoformat(timespec="seconds"), actor, action, detail, card)
        with board_lock(self._folder):
            try:
                _append_activity(self._folder, entry)
            except OSError as error:
                log.exception("activity line was not written (%s)", entry.action)
                saved = "its activity line could not be written"
                raise BoardIOError(f"{entry.action}: {saved} ({error})") from error


class BoardService:
    """Every change to one deck's board: checked against the deck and card data, locked, and logged."""

    def __init__(self, folder: Path, deck: Deck, db: CardDb, *, now: Callable[[], datetime]) -> None:
        self._folder = folder
        self._deck = deck
        self._db = db
        self._now = now

    def propose(
        self,
        name: str,
        actor: Actor,
        *,
        reason: str = "",
        replace_options: Sequence[ReplaceOption] = (),
        origin: Origin | None = None,
        origin_tag: str | None = None,
        origin_pool: OriginPool | None = None,
        copies: int = 1,
    ) -> BoardCard:
        """Put a card on the board; a card turned down or applied before is reopened, not listed twice."""
        info = self._card(name)
        options = self._options(replace_options)
        reason = _bounded(reason, MAX_REASON, "the reason")
        with self._editing() as (board, save):
            existing = board.find(info.name)
            stamp = self._stamp()
            if existing is not None and existing.status not in {Status.REJECTED, Status.APPLIED}:
                raise BoardError(f"{info.name} is already on the board ({existing.status.value})")
            if existing is not None:
                card = existing.with_status(
                    Status.OPEN,
                    reason=reason or existing.reason,
                    replace=options or existing.replace,
                    choice=None,
                    note="",
                    updated_at=stamp,
                    rejected_share=None,
                    origin=origin or existing.origin,
                    origin_tag=origin_tag if origin else existing.origin_tag,
                    origin_pool=origin_pool if origin else existing.origin_pool,
                    copies=copies,
                )
                save(_swap(board, card), Activity(stamp, actor, "reopened", card.name, reason))
                return card
            if len(board.cards) >= MAX_CARDS:
                raise BoardError(f"the board holds at most {MAX_CARDS} cards; remove some first")
            card = BoardCard(
                id=secrets.token_hex(4),
                name=info.name,
                oracle_id=info.oracle_id,
                source=actor,
                reason=reason,
                replace=options,
                status=Status.OPEN,
                choice=None,
                note="",
                added_at=stamp,
                updated_at=stamp,
                origin=origin,
                origin_tag=origin_tag,
                origin_pool=origin_pool,
                copies=copies,
            )
            save(
                Board((*board.cards, card), board.counts),
                Activity(stamp, actor, "proposed (considering)", card.name, reason),
            )
            return card

    def research(
        self,
        key: str,
        actor: Actor,
        *,
        reason: str | None = None,
        replace_options: Sequence[ReplaceOption] | None = None,
    ) -> BoardCard:
        """Record research on a card: its reason, or the cards it could replace."""
        options = self._options(replace_options) if replace_options is not None else None
        bounded = _bounded(reason, MAX_REASON, "the reason") if reason is not None else None
        with self._editing() as (board, save):
            card = _require(board, key)
            if card.status is Status.APPLIED:
                raise BoardError(f"{card.name} is already in the deck")
            updated = replace(
                card,
                reason=bounded if bounded is not None else card.reason,
                replace=options if options is not None else card.replace,
                updated_at=self._stamp(),
            )
            if updated.choice is not None and updated.choice.casefold() not in {
                o.name.casefold() for o in updated.replace
            }:
                updated = updated.with_status(Status.OPEN, choice=None)
            changed = [
                what
                for what, before, after in (
                    ("reason", card.reason, updated.reason),
                    ("cards to replace", card.replace, updated.replace),
                )
                if before != after
            ]
            save(
                _swap(board, updated),
                Activity(updated.updated_at, actor, "researched", card.name, ", ".join(changed)),
            )
            return updated

    def decide(
        self,
        key: str,
        status: Status,
        actor: Actor,
        *,
        choice: str | None = None,
        note: str = "",
        share: float | None = None,
        relabel: Relabel | None = None,
        category: str | None = None,
    ) -> BoardCard:
        """Mark a card to add (with the deck card to cut, or none, and the category to file it under), turn
        it down, or reopen it.

        share is the card's weighted share of the lists read now; a turned-down card keeps it, so lists that
        have since taken to the card can offer it again (`feed_ideas`). A turned-down card the user reopens is
        marked reconsidered, which keeps the automatic re-judge off it until they decide it; relabel, for
        that reopened card when it is relabellable, gives it the label today's lists make of it. Undoing a
        queued add does neither.
        """
        if status is Status.APPLIED:
            raise BoardError("a card counts as applied only once it is saved to Archidekt")
        note = _bounded(note, MAX_NOTE, "the note")
        filed = self._category(category) if status is Status.ADD else None
        with self._editing() as (board, save):
            card = _require(board, key)
            if card.status is Status.APPLIED:
                raise BoardError(f"{card.name} is already in the deck")
            # Queued, a card the deck already holds would save nothing and leave its cut in the deck.
            if status is Status.ADD and _holds(self._deck, card):
                raise BoardError(f"{card.name} is already in the deck; remove it from the board")
            cut = self._choice(choice) if status is Status.ADD else None
            if cut is not None and (held := self._copies(cut)) < card.copies:
                raise BoardError(f"{card.name} adds {card.copies} copies; the deck has only {held} {cut}")
            for name in (card.name, cut) if status is Status.ADD else ():
                if name is not None and board.count_of(name) is not None:
                    raise BoardError(f"{name} has a queued count change; undo it first")
            kept_share = share if status is Status.REJECTED else None
            reconsidering = status is Status.OPEN and card.status is Status.REJECTED
            updated = card.with_status(
                status,
                choice=cut,
                category=filed,
                note=note,
                updated_at=self._stamp(),
                rejected_share=kept_share,
                reconsidered=status is Status.OPEN
                and (card.reconsidered or (reconsidering and actor is Actor.YOU)),
                # The cut's count held at a send says nothing about another cut.
                sent_cut_held=card.sent_cut_held if status is Status.ADD and cut == card.choice else None,
            )
            if reconsidering and relabel is not None and card.relabellable:
                updated = _relabelled(updated, relabel)
            detail = "; ".join(filter(None, [f"cut {cut}" if cut else "", note]))
            save(
                _swap(board, updated),
                Activity(updated.updated_at, actor, _DECISIONS[status], card.name, detail),
            )
            return updated

    def remove(self, key: str, actor: Actor) -> None:
        with self._editing() as (board, save):
            card = _require(board, key)
            remaining = Board(tuple(c for c in board.cards if c.id != card.id), board.counts)
            save(remaining, Activity(self._stamp(), actor, "removed", card.name, ""))

    def mark_applied(
        self,
        ids: Sequence[str],
        actor: Actor,
        snapshot: str | None,
        *,
        counts: Sequence[CountChange] = (),
    ) -> None:
        """Record that the queued cards with these ids, and the count changes in counts (by card and
        category), were saved to Archidekt, or, with no snapshot, were found in the deck there already. A
        done count change leaves the board; a change to the same card made since stays."""
        with self._editing() as (board, save):
            stamp = self._stamp()
            done = set(ids)
            cards = tuple(
                c.with_status(Status.APPLIED, updated_at=stamp)
                if c.id in done and c.status is Status.ADD
                else c
                for c in board.cards
            )
            # The very changes that were sent: one made since, for the same card, is still to be saved.
            finished = [c for c in board.counts if c in counts]
            names = ", ".join(
                [c.name for c in board.cards if c.id in done] + [count_text(c) for c in finished]
            )
            if snapshot is None:
                entry = Activity(stamp, actor, "found in the deck on Archidekt", None, names)
            else:
                entry = Activity(stamp, actor, "saved to Archidekt", None, f"{names}; snapshot '{snapshot}'")
            save(Board(cards, tuple(c for c in board.counts if c not in finished)), entry)

    def record_sending(self, held: Mapping[str, HeldAtSend], actor: Actor) -> None:
        """Before a save is sent: keep for each queued basic land swap (by card id) the copies the deck holds
        of the card and its cut, so a send that is never marked applied is not made again
        (`BoardCard.sent_held`, `BoardCard.sent_cut_held`)."""
        with self._editing() as (board, save):
            cards = tuple(
                replace(c, sent_held=held[c.id].added, sent_cut_held=held[c.id].cut) if c.id in held else c
                for c in board.cards
            )
            names = ", ".join(f"{c.name} from {c.sent_held}" for c in cards if c.id in held)
            sending = _note_line(self._stamp(), actor, "sending to Archidekt", names, None)
            save(Board(cards, board.counts), sending)

    def set_count(
        self, name: str, count: int, actor: Actor, *, category: str | None = None
    ) -> CountChange | None:
        """Queue a deck card to end up with count copies (0 takes it out); None when that is what the deck
        holds, which drops the card's queued count change. More copies than the deck holds are only for a
        card allowed in multiples (a basic land, or one whose rules text allows more). With a category
        outside the deck, the count is of the card's copies there, and only 0 takes them off it."""
        return self._count(name, lambda _current, _was: count, actor, category)

    def step_count(
        self, name: str, step: int, actor: Actor, *, category: str | None = None
    ) -> CountChange | None:
        """Move a deck card's queued count (the copies it holds when none is queued) by step: + and -. With a
        category outside the deck, - takes all the card's copies off it and + puts them back."""
        if category is not None:
            return self._count(name, lambda current, was: _outside_step(current, was, step), actor, category)
        return self._count(name, lambda current, _: current + step, actor, None)

    def _count(
        self, name: str, target: Callable[[int, int], int], actor: Actor, category: str | None
    ) -> CountChange | None:
        held, filed = self._outside(name, category) if category is not None else (self._held(name), None)
        if held is None:
            raise BoardError(f"{name.strip()!r} is not in the deck")
        was = copies_held(self._deck, held, filed)
        with self._editing() as (board, save):
            queued = board.count_of(held, filed)
            count = target(queued.count if queued is not None else was, was)
            # Copies outside the deck come off all at once or stay: no count between.
            if filed is not None and 0 < count != was:
                raise BoardError(f"{held} on {filed} can only be taken off")
            self._check_count(board, held, was, count, outside=filed is not None)
            others = tuple(c for c in board.counts if c is not queued)
            stamp = self._stamp()
            if count == was:
                if queued is None:
                    return None
                save(Board(board.cards, others), Activity(stamp, actor, "count change undone", held, ""))
                return None
            if queued is None and len(board.counts) >= MAX_CARDS:
                raise BoardError(f"the board holds at most {MAX_CARDS} count changes; save some first")
            change = CountChange(held, was, count, actor, stamp, filed)
            action = "queued to remove" if count == 0 else "queued copies"
            action = f"{action} from {filed}" if filed else action
            save(
                Board(board.cards, (*others, change)),
                Activity(stamp, actor, action, held, f"{was} → {count}"),
            )
            return change

    def _outside(self, name: str, category: str) -> tuple[str, str]:
        """The deck's spelling of a card filed under a category outside the deck, and of that category."""
        key = change_key(name, category)
        card = next(
            (c for c in self._deck.cards if not c.in_deck and change_key(c.name, c.primary_category) == key),
            None,
        )
        if card is None:
            raise BoardError(f"{name.strip()!r} is not on {category.strip()}")
        return card.name, card.primary_category

    def _check_count(self, board: Board, name: str, was: int, count: int, *, outside: bool = False) -> None:
        if count < 0:
            raise BoardError(f"{name} is already queued to come out")
        if outside:
            # Copies outside the deck are no cut nor target of a swap, and only ever come off.
            return
        if count > MAX_COPIES:
            raise BoardError(f"a deck holds at most {MAX_COPIES} copies of {name}")
        if count > was and (limit := self._copy_limit(name)) is not None and count > limit:
            allowed = "one" if limit == 1 else str(limit)
            raise BoardError(f"Commander allows {allowed} {name}")
        keys = name_keys(name)
        for card in board.queued:
            if any(other and name_keys(other) & keys for other in (card.name, card.choice)):
                raise BoardError(f"{name} is in a queued change for {card.name}; undo that first")

    def _copy_limit(self, name: str) -> int | None:
        info = self._db.card_named(name)
        if info is None:
            raise BoardError(f"no card data for {name}; sync to refresh it")
        text = self._db.card_text(info.oracle_id)
        return copy_limit(info, text.text if text else "")

    def turn_down_open(self, key: str, note: str, share: float) -> BoardCard | None:
        """Turn down a card as cmdr, but only while it is still open and its own: None when it was decided,
        reopened by the user or removed meanwhile, or is by you or Claude. share is its weighted share of
        the lists read now, the baseline for its return."""
        with self._editing() as (board, save):
            card = board.find(key)
            if (
                card is None
                or card.status is not Status.OPEN
                or card.reconsidered
                or card.source not in {Actor.CMDR, Actor.ARCHIDEKT}
            ):
                return None
            stamp = self._stamp()
            updated = card.with_status(
                Status.REJECTED, note=note, updated_at=stamp, rejected_share=share, reconsidered=False
            )
            save(
                _swap(board, updated),
                Activity(stamp, Actor.CMDR, _DECISIONS[Status.REJECTED], card.name, note),
            )
            return updated

    def close_held(self) -> list[BoardCard]:
        """Close every open card the main deck now holds as applied, noting "already in the deck", whoever put
        it on the board and even when the user reopened it: such a card is no idea (ADR 0046). A card that
        leaves the deck again comes back as an idea at the next sync (`feed_ideas`). Returns the cards closed,
        in board order; queued cards are left to Review's mark-applied path."""
        held = held_keys(self._deck, self._db)
        open_held = [
            card.id
            for card in read_board(self._folder).cards
            if card.status is Status.OPEN and card_key(card.name) in held
        ]
        closed = (self._close_if_open(card_id) for card_id in open_held)
        return [card for card in closed if card is not None]

    def _close_if_open(self, key: str) -> BoardCard | None:
        with self._editing() as (board, save):
            card = board.find(key)
            if card is None or card.status is not Status.OPEN:
                return None
            stamp = self._stamp()
            updated = card.with_status(
                Status.APPLIED, note=ALREADY_IN_THE_DECK, updated_at=stamp, reconsidered=False
            )
            save(_swap(board, updated), Activity(stamp, Actor.CMDR, "closed", card.name, ALREADY_IN_THE_DECK))
            return updated

    def clear_met_counts(self) -> list[CountChange]:
        """Drop every queued count change the main deck already meets (`CountChange.met_by`), noting each on
        the activity log: a done change would show on, and block, a card the deck no longer needs it for
        (ADR 0043). Returns the changes cleared, in board order."""
        met = counts_met(self._deck, read_board(self._folder))
        cleared = (self._clear_count_if_met(change.name, change.category) for change in met)
        return [change for change in cleared if change is not None]

    def _clear_count_if_met(self, name: str, category: str | None) -> CountChange | None:
        held = copies_held(self._deck, name, category)
        with self._editing() as (board, save):
            change = board.count_of(name, category)
            if change is None or not change.met_by(held):
                return None
            stamp = self._stamp()
            others = tuple(c for c in board.counts if c is not change)
            detail = f"{change.was} → {change.count}; {category or 'the deck'} holds {held}"
            save(Board(board.cards, others), Activity(stamp, Actor.CMDR, "count change done", name, detail))
            return change

    def relabel_open(self, key: str, label: Relabel) -> BoardCard | None:
        """Give an open idea from the old Maybeboard pick-up the label today's lists make of it, as an idea
        cmdr compiled: None when the card was decided or removed meanwhile, or no list names a source."""
        return self._relabel(key, label, lambda card: card.source is Actor.ARCHIDEKT and card.relabellable)

    def relabel_pooled_open(self, key: str, label: Relabel) -> BoardCard | None:
        """Give an open alike or tagged idea of cmdr, whose pool is no longer its source, the label today's
        lists make of it; None when it was decided, reopened by the user or removed meanwhile, or no list
        names a source."""
        return self._relabel(
            key,
            label,
            lambda card: (
                card.source is Actor.CMDR
                and card.origin in {Origin.ADJACENT, Origin.TAGGED}
                and not card.reconsidered
            ),
        )

    def _relabel(self, key: str, label: Relabel, eligible: Callable[[BoardCard], bool]) -> BoardCard | None:
        with self._editing() as (board, save):
            card = board.find(key)
            if card is None or card.status is not Status.OPEN or label.origin is None or not eligible(card):
                return None
            updated = replace(_relabelled(card, label), updated_at=self._stamp())
            save(
                _swap(board, updated),
                Activity(
                    updated.updated_at, Actor.CMDR, "relabelled", card.name, f"from {label.origin.value}"
                ),
            )
            return updated

    def remember_share(self, key: str, share: float) -> None:
        """Give a turned-down card that has none its share now, as the baseline for its return."""
        with self._editing() as (board, save):
            card = _require(board, key)
            if card.status is not Status.REJECTED or card.rejected_share is not None:
                return
            stamp = self._stamp()
            save(
                _swap(board, replace(card, rejected_share=share, updated_at=stamp)),
                Activity(stamp, Actor.CMDR, "share noted", card.name, f"in {share:.0%} of the lists read"),
            )

    def remember_pool(self, key: str, pool: OriginPool) -> None:
        """Give an alike or tagged idea on the board that has no pool the numbers it was found by."""
        with self._editing() as (board, save):
            card = _require(board, key)
            if card.origin_pool is not None or card.origin not in {Origin.ADJACENT, Origin.TAGGED}:
                return
            stamp = self._stamp()
            save(
                _swap(board, replace(card, origin_pool=pool, updated_at=stamp)),
                Activity(
                    stamp, Actor.CMDR, "pool noted", card.name, f"in {pool.count} of {pool.total} decks"
                ),
            )

    def note(self, actor: Actor, action: str, detail: str, card: str | None = None) -> None:
        """Log something that happened to the deck without changing the board, such as a failed save."""
        with self._editing() as (board, save):
            save(board, _note_line(self._stamp(), actor, action, detail, card))

    @cached_property
    def _identity(self) -> str | None:
        # Once per service: the deck does not change under it, and a sync adds up to 150 cards.
        return deck_identity(self._db.resolve(self._deck.main_cards))

    def _card(self, name: str) -> CardInfo:
        if not name.strip():
            raise BoardError("the card name is empty")
        info = self._db.card_named(name.strip())
        if info is None:
            raise BoardError(f"no card named {name.strip()!r} (check the spelling)")
        if not info.commander_legal:
            raise BoardError(f"{info.name} is not legal in Commander")
        identity = self._identity
        if identity is not None and not set(info.color_identity) <= set(identity):
            raise BoardError(f"{info.name} is outside the deck's colours ({identity or 'colourless'})")
        # A deck may run any number of a basic land, so a basic can be added when it already runs some.
        basic = info.front_type_line.startswith("Basic ")
        if not basic and any(name_keys(c.name) & name_keys(info.name) for c in self._deck.main_cards):
            raise BoardError(f"{info.name} is already in the deck")
        return info

    def _options(self, options: Sequence[ReplaceOption]) -> tuple[ReplaceOption, ...]:
        if len(options) > MAX_REPLACE:
            raise BoardError(f"at most {MAX_REPLACE} cards to replace per card")
        checked: list[ReplaceOption] = []
        for option in options:
            held = self._held(option.name)
            if held is None:
                raise BoardError(f"{option.name.strip()!r} is not in the deck, so it cannot make room")
            if any(c.name == held for c in checked):
                continue
            checked.append(ReplaceOption(held, _bounded(option.reason, MAX_NOTE, "a replace reason")))
        return tuple(checked)

    def _category(self, category: str | None) -> str | None:
        """The category to file an added card under, checked; None leaves it to the card type."""
        if category is None or not category.strip():
            return None
        named = category.strip()
        if len(named) > MAX_CATEGORY_LENGTH:
            raise BoardError(f"a category name is at most {MAX_CATEGORY_LENGTH} characters")
        outside = {c.primary_category.casefold() for c in self._deck.cards if not c.in_deck}
        if named.casefold() in outside | _OUTSIDE_NAMES:
            raise BoardError(f"{named} is outside the deck; name a category in the deck")
        return named

    def _choice(self, choice: str | None) -> str | None:
        if choice is None:
            return None
        held = self._held(choice)
        if held is None:
            raise BoardError(f"{choice.strip()!r} is not in the deck, so it cannot make room")
        return held

    def _copies(self, name: str) -> int:
        return sum(c.quantity for c in self._deck.main_cards if c.name == name)

    def _held(self, name: str) -> str | None:
        """The deck's spelling of a card that can make room, None when it is not in the deck."""
        keys = name_keys(name.strip())
        card = next((c for c in self._deck.main_cards if name_keys(c.name) & keys), None)
        if card is None:
            return None
        if COMMANDER_CATEGORY in card.categories:
            raise BoardError(f"{card.name} is a commander, so it cannot make room")
        return card.name

    def _stamp(self) -> str:
        return self._now().isoformat(timespec="seconds")

    @contextmanager
    def _editing(self) -> Generator[tuple[Board, Callable[[Board, Activity], None]]]:
        with board_lock(self._folder):
            board, stored_version = _read_stored(self._folder)

            def save(updated: Board, entry: Activity) -> None:
                nonlocal stored_version
                if stored_version < BOARD_VERSION:
                    _back_up_older(self._folder, stored_version)
                    stored_version = BOARD_VERSION
                write_text_atomic(
                    self._folder / BOARD_FILE,
                    json.dumps(board_json(updated), ensure_ascii=False, indent=2) + "\n",
                )
                try:
                    _append_activity(self._folder, entry)
                except OSError as error:
                    # The board is written first, so the decision stands; say that the log is short of it.
                    log.exception("board saved but its activity line was not written (%s)", entry.action)
                    saved = "saved on the board, but its activity line could not be written"
                    raise BoardIOError(f"{entry.action}: {saved} ({error})") from error

            yield board, save


_DECISIONS = {Status.ADD: "queued to add", Status.REJECTED: "turned down", Status.OPEN: "reopened"}
# Archidekt's own categories outside the deck, refused for an add even in a deck that has neither yet.
_OUTSIDE_NAMES = frozenset({"maybeboard", "sideboard"})


def count_text(change: CountChange) -> str:
    """A count change as the activity log and sync name it: "Mountain 6 → 7", "Ponder on Maybeboard 1 → 0"."""
    where = f" on {change.category}" if change.category else ""
    return f"{change.name}{where} {change.was} → {change.count}"


def _outside_step(current: int, was: int, step: int) -> int:
    """- takes every copy off a category outside the deck at once and + puts them all back; a step past
    either end comes out of range, which the count check refuses."""
    if step < 0:
        return 0 if current > 0 else -1
    return was if current == 0 else current + step


def _require(board: Board, key: str) -> BoardCard:
    card = board.find(key)
    if card is None:
        raise BoardError(f"{key.strip()!r} is not on the board")
    return card


def _relabelled(card: BoardCard, label: Relabel) -> BoardCard:
    """The card with the reason of the label, and, when the label names a source, as an idea cmdr compiled."""
    if label.origin is None:
        return replace(card, reason=label.reason)
    return replace(
        card,
        source=Actor.CMDR,
        reason=label.reason,
        origin=label.origin,
        origin_tag=None,
        origin_pool=None,
    )


def _swap(board: Board, card: BoardCard) -> Board:
    return Board(tuple(card if c.id == card.id else c for c in board.cards), board.counts)


def _bounded(text: str, limit: int, what: str) -> str:
    cleaned = text.strip()
    if len(cleaned) > limit:
        raise BoardError(f"{what} is longer than {limit:,} characters")
    return cleaned


def _note_line(stamp: str, actor: Actor, action: str, detail: str, card: str | None) -> Activity:
    """The activity line of a note, its detail bounded."""
    return Activity(stamp, actor, action, card, _bounded(detail, MAX_NOTE, "the detail"))


def _back_up_older(folder: Path, version: int) -> None:
    """Keep the board file as it was written in an older format, so a bad migration can be recovered."""
    backup = folder / f"board.v{version}.bak"
    try:
        shutil.copyfile(folder / BOARD_FILE, backup)
    except OSError as error:
        raise BoardIOError(f"cannot back up {folder / BOARD_FILE} to {backup}: {error}") from error


def _append_activity(folder: Path, entry: Activity) -> None:
    line = json.dumps(activity_json(entry), ensure_ascii=False) + "\n"
    with (folder / ACTIVITY_FILE).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)


def _activity_from_json(raw: object) -> Activity:
    entry = as_object(raw, "activity")
    return Activity(
        at=req_str(entry, "at", "activity"),
        actor=Actor(req_str(entry, "actor", "activity")),
        action=req_str(entry, "action", "activity"),
        card=opt_str(entry, "card", "activity"),
        detail=req_str(entry, "detail", "activity"),
    )


def _migrate(raw: Mapping[str, object]) -> Mapping[str, object]:
    # Migrations from older board versions go here, on the raw tree, before binding to Board.
    version = req_int(raw, "version", "board")
    if version > BOARD_VERSION:
        raise ShapeError(f"board version {version} is newer than this cmdr reads (up to {BOARD_VERSION})")
    if version < 1:
        raise ShapeError(f"board version {version} is not supported")
    if version == 1:
        # Version 2 has one list, Considering, where version 1 had "considered" and "suggested".
        raw = {
            **raw,
            "cards": [
                {key: value for key, value in as_object(card, "board.cards").items() if key != "section"}
                for card in req_list(raw, "cards", "board")
            ],
        }
    return raw


@contextmanager
def board_lock(folder: Path) -> Generator[None]:
    """Hold the deck's board lock: one thread here, and one process across the CLI and the app. Raises
    BoardIOError when another holds it for longer than LOCK_TIMEOUT_SECONDS."""
    path = folder / LOCK_FILE
    with _thread_lock(folder):
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if _lock_is_stale(path) and _clear_stale_lock(path):
                    continue
                if time.monotonic() > deadline:
                    raise BoardIOError(f"the board is busy ({path} is held); try again") from None
                time.sleep(_LOCK_POLL_SECONDS)
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            yield
        finally:
            path.unlink(missing_ok=True)


def _clear_stale_lock(path: Path) -> bool:
    """Remove a stale lock; False when another process is doing so or the lock turned out to be fresh.

    Whoever holds the takeover file alone removes the lock, and looks again first: a bare unlink after the
    check could delete the lock another process made fresh in between.
    """
    guard = path.with_name(path.name + ".takeover")
    try:
        os.close(os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        if _lock_is_stale(guard):
            log.warning("removing stale board takeover file %s", guard)
            guard.unlink(missing_ok=True)
        return False
    try:
        if not _lock_is_stale(path):
            return False
        log.warning("removing stale board lock %s", path)
        path.unlink(missing_ok=True)
        return True
    finally:
        guard.unlink(missing_ok=True)


def _lock_is_stale(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime > STALE_LOCK_SECONDS
    except FileNotFoundError:
        return False


MAX_CUT_OPTIONS = 3


def feed_suggestions(service: BoardService, result: Suggestions, board: Board) -> list[BoardCard]:
    """Put suggest's ideas on the board under Considering, the deck's cut candidates as cards to replace.

    Cards already on the board, in any state, are left alone, so a turned-down card is never offered again.
    """
    cuts = [
        ReplaceOption(
            info.name, f"no role, in no combo; EDHREC #{info.edhrec_rank:,}" if info.edhrec_rank else ""
        )
        for info in result.cuts[:MAX_CUT_OPTIONS]
    ]
    ideas: dict[str, str] = {}
    for idea in result.themed:
        fits = f": {', '.join(idea.fits)}" if idea.fits else ""
        ideas.setdefault(idea.info.name, f"fits the deck's themes{fits}")
    for target in result.roles:
        for idea in target.ideas:
            ideas.setdefault(
                idea.info.name, f"{target.role.value} is short: {target.have} of {target.target}"
            )
    for piece in result.pieces:
        tags = sorted({v.tag.label for v in piece.combos})
        count = len(piece.combos)
        ideas.setdefault(
            piece.name, f"completes {count} combo{'s' if count != 1 else ''} ({', '.join(tags)})"
        )
    return feed_ideas(service, ideas, cuts, board)


def feed_ideas(
    service: BoardService,
    ideas: Mapping[str, str],
    cuts: Sequence[ReplaceOption],
    board: Board,
    *,
    shares: Mapping[str, float] | None = None,
    origin: Origin | None = None,
    origin_tag: str | None = None,
    origin_pool: OriginPool | None = None,
) -> list[BoardCard]:
    """Put each idea (card name to reason) on the board under Considering, with cuts as cards to replace
    and where the ideas came from (origin_tag: the deck tag, for tagged decks; origin_pool: the decks it
    was found in, for alike and tagged ones).

    Cards already on the board are left alone, but with the weighted shares of the lists read (by `card_key`)
    a turned-down one comes back once its share has risen by RETURN_RISE since; one turned down without a
    known share gets today's as its baseline. An applied card the deck no longer holds comes back at once.
    """
    added: list[BoardCard] = []
    for name, idea in ideas.items():
        reason = idea
        existing = board.find(name)
        if existing is not None:
            back = _returning(service, existing, shares)
            if back is None:
                continue
            reason = f"{back}; {idea}" if idea else back
        try:
            card = service.propose(
                name,
                Actor.CMDR,
                reason=reason,
                replace_options=cuts,
                origin=origin,
                origin_tag=origin_tag,
                origin_pool=origin_pool,
            )
        except BoardError as error:
            log.info("not putting %s on the board: %s", name, error)
            continue
        added.append(card)
    return added


def _returning(service: BoardService, card: BoardCard, shares: Mapping[str, float] | None) -> str | None:
    """Why a turned-down card comes back to the board now, or None to leave it alone."""
    if card.status is Status.APPLIED:
        return "back: no longer in the deck"
    if card.status is not Status.REJECTED or shares is None:
        return None
    now = shares.get(card_key(card.name), 0.0)
    if card.rejected_share is None:
        service.remember_share(card.id, now)
        return None
    if now < card.rejected_share + RETURN_RISE:
        return None
    return f"back: in {now:.0%} of the lists read, up from {card.rejected_share:.0%} when turned down"
