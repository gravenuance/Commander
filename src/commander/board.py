"""Deck boards: cards under consideration or suggested for one deck, their research, decisions and history.

Each deck folder holds board.json (the cards now) and activity.jsonl (every change, oldest first), so the
desktop app, the CLI and Claude all work from the same files and can catch up on each other (ADR 0006).
"""

import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path

from commander.answers import card_key
from commander.carddb import CardDb, CardInfo
from commander.fsutil import write_text_atomic
from commander.jsonshape import ShapeError, as_object, opt_float, opt_str, req_int, req_list, req_str
from commander.models import Deck
from commander.plan import AddCard, Plan, RemoveCard
from commander.suggest import Suggestions, deck_identity, name_keys

log = logging.getLogger(__name__)

BOARD_FILE = "board.json"
ACTIVITY_FILE = "activity.jsonl"
LOCK_FILE = ".board.lock"
BOARD_VERSION = 1
MAX_REASON = 4000
MAX_NOTE = 1000
MAX_REPLACE = 10
MAX_CARDS = 500
MAX_LOG_LINE = 16 * 1024
LOCK_TIMEOUT_SECONDS = 10.0
# A lock file older than this was left by a crashed writer; a real update takes milliseconds.
STALE_LOCK_SECONDS = 60.0
# How far a turned-down card's share of the meta's lists must rise before the meta offers it again.
RETURN_RISE = 0.20
_LOCK_POLL_SECONDS = 0.05
_THREAD_LOCK = threading.Lock()


class BoardError(Exception):
    """The board cannot do what was asked; the message says why, in a few plain words."""


class Section(Enum):
    CONSIDERED = "considered"
    SUGGESTED = "suggested"


class Actor(Enum):
    YOU = "you"
    CLAUDE = "claude"
    CMDR = "cmdr"
    # Put on the deck's Sideboard or Maybeboard on archidekt.com, and picked up by a sync.
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
    """Where an idea compiled for Considered came from (ADR 0014); None for anything else."""

    # Staples of the commander's EDHTop16 lists (bracket 5 only).
    META = "meta"
    # Staples of the most-viewed recent Archidekt decks for the commander at the deck's bracket.
    DECKS = "archidekt decks"


@dataclass(slots=True, frozen=True)
class BoardCard:
    id: str
    name: str
    oracle_id: str
    section: Section
    source: Actor
    reason: str
    replace: tuple[ReplaceOption, ...]
    status: Status
    # The deck card chosen to make room when the card is added; None adds it without a cut.
    choice: str | None
    note: str
    added_at: str
    updated_at: str
    # For a turned-down card: its share of the meta's lists when it was turned down; None when unknown.
    rejected_share: float | None = None
    origin: Origin | None = None

    def __post_init__(self) -> None:
        if self.choice is not None and self.status not in {Status.ADD, Status.APPLIED}:
            raise ValueError(f"{self.name}: only a card being added has a chosen cut")
        if self.rejected_share is not None and self.status is not Status.REJECTED:
            raise ValueError(f"{self.name}: only a turned-down card keeps its meta share")


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

    def to_drop(self, deck: Deck) -> tuple[str, ...]:
        """Turned-down cards still on the deck's Sideboard or Maybeboard: the next save takes them off."""
        aside = {key for card in deck.aside_cards for key in name_keys(card.name)}
        return tuple(c.name for c in self.cards if c.status is Status.REJECTED and name_keys(c.name) & aside)

    def pending(self, deck: Deck) -> int:
        """Changes the next save would make: cards to add and cards to take off the Maybeboard."""
        return len(self.queued) + len(self.to_drop(deck))


def board_stamp(folder: Path) -> str:
    """Changes whenever the board, its activity or the deck snapshot changes; cheap enough to poll."""
    parts: list[str] = []
    for name in (BOARD_FILE, ACTIVITY_FILE, "deck.json", "profile.toml"):
        try:
            stat = (folder / name).stat()
        except FileNotFoundError:
            parts.append("-")
            continue
        parts.append(f"{stat.st_mtime_ns}:{stat.st_size}")
    return "|".join(parts)


def read_board(folder: Path) -> Board:
    path = folder / BOARD_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Board(())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BoardError(f"cannot read {path}: {error}") from error
    try:
        return parse_board(_migrate(as_object(raw, "board")))
    except (ShapeError, ValueError) as error:
        raise BoardError(f"{path}: {error}") from error


def read_activity(folder: Path, *, since: str | None = None) -> list[Activity]:
    """The board's history, oldest first; since keeps entries strictly after that UTC timestamp."""
    path = folder / ACTIVITY_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as error:
        raise BoardError(f"cannot read {path}: {error}") from error
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
                section=Section(req_str(card, "section", where)),
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
            )
        )
    return Board(tuple(cards))


def _origin(value: str | None) -> Origin | None:
    return Origin(value) if value else None


def board_json(board: Board) -> dict[str, object]:
    return {"version": BOARD_VERSION, "cards": [card_json(card) for card in board.cards]}


def card_json(card: BoardCard) -> dict[str, object]:
    return {
        "id": card.id,
        "name": card.name,
        "oracleId": card.oracle_id,
        "section": card.section.value,
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

    It adds every queued card and moves its chosen cut to the Maybeboard, so the swap can be weighed the
    other way round later, and takes turned-down cards off the Sideboard or Maybeboard.
    """
    queued = board.queued
    drop = board.to_drop(deck)
    if not queued and not drop:
        return None
    return Plan(
        str(deck.id),
        tuple(AddCard(card.name) for card in queued),
        tuple(RemoveCard(card.choice, maybeboard=True) for card in queued if card.choice),
        drop,
    )


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
        section: Section = Section.CONSIDERED,
        reason: str = "",
        replace_options: Sequence[ReplaceOption] = (),
        origin: Origin | None = None,
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
                card = replace(
                    existing,
                    section=section,
                    reason=reason or existing.reason,
                    replace=options or existing.replace,
                    status=Status.OPEN,
                    choice=None,
                    note="",
                    updated_at=stamp,
                    rejected_share=None,
                    origin=origin or existing.origin,
                )
                save(_swap(board, card), Activity(stamp, actor, "reopened", card.name, reason))
                return card
            if len(board.cards) >= MAX_CARDS:
                raise BoardError(f"the board holds at most {MAX_CARDS} cards; remove some first")
            card = BoardCard(
                id=secrets.token_hex(4),
                name=info.name,
                oracle_id=info.oracle_id,
                section=section,
                source=actor,
                reason=reason,
                replace=options,
                status=Status.OPEN,
                choice=None,
                note="",
                added_at=stamp,
                updated_at=stamp,
                origin=origin,
            )
            save(
                Board((*board.cards, card)),
                Activity(stamp, actor, f"proposed ({section.value})", card.name, reason),
            )
            return card

    def research(
        self,
        key: str,
        actor: Actor,
        *,
        reason: str | None = None,
        replace_options: Sequence[ReplaceOption] | None = None,
        section: Section | None = None,
    ) -> BoardCard:
        """Record research on a card: its reason, the cards it could replace, or the list it belongs on."""
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
                section=section or card.section,
                updated_at=self._stamp(),
            )
            if updated.choice is not None and updated.choice.casefold() not in {
                o.name.casefold() for o in updated.replace
            }:
                updated = replace(updated, choice=None, status=Status.OPEN)
            changed = [
                what
                for what, before, after in (
                    ("reason", card.reason, updated.reason),
                    ("cards to replace", card.replace, updated.replace),
                    ("list", card.section, updated.section),
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
    ) -> BoardCard:
        """Mark a card to add (with the deck card to cut), turn it down, or reopen it.

        share is the card's share of the meta's lists now; a turned-down card keeps it, so a meta that has
        since taken to the card can offer it again (`feed_ideas`).
        """
        if status is Status.APPLIED:
            raise BoardError("a card counts as applied only once it is saved to Archidekt")
        note = _bounded(note, MAX_NOTE, "the note")
        with self._editing() as (board, save):
            card = _require(board, key)
            if card.status is Status.APPLIED:
                raise BoardError(f"{card.name} is already in the deck")
            cut = self._choice(choice) if status is Status.ADD else None
            kept_share = share if status is Status.REJECTED else None
            updated = replace(
                card,
                status=status,
                choice=cut,
                note=note,
                updated_at=self._stamp(),
                rejected_share=kept_share,
            )
            detail = "; ".join(filter(None, [f"cut {cut}" if cut else "", note]))
            save(
                _swap(board, updated),
                Activity(updated.updated_at, actor, _DECISIONS[status], card.name, detail),
            )
            return updated

    def remove(self, key: str, actor: Actor) -> None:
        with self._editing() as (board, save):
            card = _require(board, key)
            remaining = Board(tuple(c for c in board.cards if c.id != card.id))
            save(remaining, Activity(self._stamp(), actor, "removed", card.name, ""))

    def mark_applied(self, ids: Sequence[str], actor: Actor, snapshot: str) -> None:
        """Record that the queued cards with these ids were saved to Archidekt."""
        with self._editing() as (board, save):
            stamp = self._stamp()
            done = set(ids)
            cards = tuple(
                replace(c, status=Status.APPLIED, updated_at=stamp)
                if c.id in done and c.status is Status.ADD
                else c
                for c in board.cards
            )
            names = ", ".join(c.name for c in board.cards if c.id in done)
            save(
                Board(cards),
                Activity(stamp, actor, "saved to Archidekt", None, f"{names}; snapshot '{snapshot}'"),
            )

    def remember_share(self, key: str, share: float) -> None:
        """Give a turned-down card that has none its meta share now, as the baseline for its return."""
        with self._editing() as (board, save):
            card = _require(board, key)
            if card.status is not Status.REJECTED or card.rejected_share is not None:
                return
            stamp = self._stamp()
            save(
                _swap(board, replace(card, rejected_share=share, updated_at=stamp)),
                Activity(stamp, Actor.CMDR, "meta share noted", card.name, f"in {share:.0%} of meta lists"),
            )

    def note(self, actor: Actor, action: str, detail: str, card: str | None = None) -> None:
        """Log something that happened to the deck without changing the board, such as a failed save."""
        with self._editing() as (board, save):
            save(
                board, Activity(self._stamp(), actor, action, card, _bounded(detail, MAX_NOTE, "the detail"))
            )

    def _card(self, name: str) -> CardInfo:
        if not name.strip():
            raise BoardError("the card name is empty")
        info = self._db.card_named(name.strip())
        if info is None:
            raise BoardError(f"no card named {name.strip()!r} (check the spelling)")
        if not info.commander_legal:
            raise BoardError(f"{info.name} is not legal in Commander")
        identity = deck_identity(self._db.resolve(self._deck.main_cards))
        if identity is not None and not set(info.color_identity) <= set(identity):
            raise BoardError(f"{info.name} is outside the deck's colours ({identity or 'colourless'})")
        if any(name_keys(c.name) & name_keys(info.name) for c in self._deck.main_cards):
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

    def _choice(self, choice: str | None) -> str | None:
        if choice is None:
            return None
        held = self._held(choice)
        if held is None:
            raise BoardError(f"{choice.strip()!r} is not in the deck, so it cannot make room")
        return held

    def _held(self, name: str) -> str | None:
        keys = name_keys(name.strip())
        return next((c.name for c in self._deck.main_cards if name_keys(c.name) & keys), None)

    def _stamp(self) -> str:
        return self._now().isoformat(timespec="seconds")

    @contextmanager
    def _editing(self) -> Generator[tuple[Board, Callable[[Board, Activity], None]]]:
        with _board_lock(self._folder):
            board = read_board(self._folder)

            def save(updated: Board, entry: Activity) -> None:
                write_text_atomic(
                    self._folder / BOARD_FILE,
                    json.dumps(board_json(updated), ensure_ascii=False, indent=2) + "\n",
                )
                _append_activity(self._folder, entry)

            yield board, save


_DECISIONS = {Status.ADD: "queued to add", Status.REJECTED: "turned down", Status.OPEN: "reopened"}


def _require(board: Board, key: str) -> BoardCard:
    card = board.find(key)
    if card is None:
        raise BoardError(f"{key.strip()!r} is not on the board")
    return card


def _swap(board: Board, card: BoardCard) -> Board:
    return Board(tuple(card if c.id == card.id else c for c in board.cards))


def _bounded(text: str, limit: int, what: str) -> str:
    cleaned = text.strip()
    if len(cleaned) > limit:
        raise BoardError(f"{what} is longer than {limit:,} characters")
    return cleaned


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
    if version != BOARD_VERSION:
        raise ShapeError(f"board version {version} is not supported (expected {BOARD_VERSION})")
    return raw


@contextmanager
def _board_lock(folder: Path) -> Generator[None]:
    """Hold the deck's board lock: one thread here, and one process across the CLI and the app."""
    path = folder / LOCK_FILE
    with _THREAD_LOCK:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if _lock_is_stale(path):
                    log.warning("removing stale board lock %s", path)
                    path.unlink(missing_ok=True)
                    continue
                if time.monotonic() > deadline:
                    raise BoardError(f"the board is busy ({path} is held); try again") from None
                time.sleep(_LOCK_POLL_SECONDS)
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            yield
        finally:
            path.unlink(missing_ok=True)


def _lock_is_stale(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime > STALE_LOCK_SECONDS
    except FileNotFoundError:
        return False


MAX_CUT_OPTIONS = 3


def feed_suggestions(service: BoardService, result: Suggestions, board: Board) -> list[BoardCard]:
    """Put suggest's ideas on the board as suggested by cmdr, the deck's cut candidates as cards to replace.

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
) -> list[BoardCard]:
    """Put each idea (card name to reason) on the board as suggested by cmdr, with cuts as cards to replace
    and where the ideas came from.

    Cards already on the board are left alone, but with the meta's shares (by `card_key`) a turned-down one
    comes back once its share has risen by RETURN_RISE since; one turned down without a known share gets
    today's as its baseline.
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
                section=Section.SUGGESTED,
                reason=reason,
                replace_options=cuts,
                origin=origin,
            )
        except BoardError as error:
            log.info("not putting %s on the board: %s", name, error)
            continue
        added.append(card)
    return added


def _returning(service: BoardService, card: BoardCard, shares: Mapping[str, float] | None) -> str | None:
    """Why a turned-down card comes back to the board now, or None to leave it alone."""
    if card.status is not Status.REJECTED or shares is None:
        return None
    now = shares.get(card_key(card.name), 0.0)
    if card.rejected_share is None:
        service.remember_share(card.id, now)
        return None
    if now < card.rejected_share + RETURN_RISE:
        return None
    return f"back: in {now:.0%} of meta lists, up from {card.rejected_share:.0%} when turned down"


def import_aside(service: BoardService, deck: Deck, board: Board) -> list[BoardCard]:
    """Put the deck's Sideboard and Maybeboard cards on the board as considered, as picked on Archidekt.

    A card already on the board stays as it is, unless it was applied: one cut back to the Maybeboard since
    is reopened, so a swap can be weighed the other way round.
    """
    added: list[BoardCard] = []
    for card in deck.aside_cards:
        existing = board.find(card.name)
        if existing is not None and existing.status is not Status.APPLIED:
            continue
        where = card.primary_category
        try:
            added.append(service.propose(card.name, Actor.ARCHIDEKT, reason=f"on the {where} on Archidekt"))
        except BoardError as error:
            log.info("not putting %s from the %s on the board: %s", card.name, where, error)
    return added
