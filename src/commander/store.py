"""Deck snapshots on disk: one folder per deck holding deck.json, list.md and notes.md."""

import json
import logging
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from commander.fsutil import write_text_atomic
from commander.jsonshape import (
    ShapeError,
    as_object,
    opt_bool,
    opt_int,
    opt_str,
    req_bool,
    req_int,
    req_list,
    req_str,
    str_list,
)
from commander.models import Deck, DeckCard
from commander.render import deck_list_markdown, notes_template

log = logging.getLogger(__name__)

SNAPSHOT_VERSION = 4
SNAPSHOT_FILE = "deck.json"
LIST_FILE = "list.md"
NOTES_FILE = "notes.md"
# The account's card labels (ADR 0012), shared by every deck; beside the deck folders.
LABELS_FILE = "labels.json"
LABELS_VERSION = 1
_MAX_SLUG = 60


class WorkspaceError(Exception):
    """The deck folder is in a state this tool will not guess its way out of."""


@dataclass(slots=True, frozen=True)
class StoredDeck:
    folder: Path
    deck: Deck


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root

    def folder_for(self, deck_id: int) -> Path | None:
        if not self.root.is_dir():
            return None
        matches = [p for p in self.root.iterdir() if p.is_dir() and _folder_deck_id(p.name) == deck_id]
        if len(matches) > 1:
            names = ", ".join(sorted(p.name for p in matches))
            raise WorkspaceError(f"more than one folder for deck {deck_id}: {names}; keep one")
        return matches[0] if matches else None

    def load(self, deck_id: int) -> Deck | None:
        """The saved snapshot, or None when there is none or it cannot be read (logged)."""
        folder = self.folder_for(deck_id)
        if folder is None:
            return None
        return self.load_folder(folder)

    def load_folder(self, folder: Path) -> Deck | None:
        """The snapshot saved in this deck folder, None when there is none or it cannot be read (logged)."""
        return _read_snapshot(folder / SNAPSHOT_FILE)

    def deck_folders(self) -> list[tuple[int, Path]]:
        """Each deck folder with its deck id, from the folder names alone; nothing is read."""
        if not self.root.is_dir():
            return []
        found = ((_folder_deck_id(p.name), p) for p in sorted(self.root.iterdir()) if p.is_dir())
        return [(deck_id, folder) for deck_id, folder in found if deck_id is not None]

    def load_all(self) -> list[StoredDeck]:
        if not self.root.is_dir():
            return []
        stored: list[StoredDeck] = []
        for folder in sorted(self.root.iterdir()):
            if not folder.is_dir() or _folder_deck_id(folder.name) is None:
                continue
            deck = _read_snapshot(folder / SNAPSHOT_FILE)
            if deck is not None:
                stored.append(StoredDeck(folder, deck))
        return stored

    def find(self, query: str) -> list[StoredDeck]:
        """Decks whose id equals query, or else whose name, folder or commander contains it (any case)."""
        stored = self.load_all()
        text = query.strip()
        if text.isdigit():
            by_id = [s for s in stored if s.deck.id == int(text)]
            if by_id:
                return by_id
        needle = text.casefold()
        return [
            s
            for s in stored
            if needle in s.deck.name.casefold()
            or needle in s.folder.name.casefold()
            or any(needle in commander.casefold() for commander in s.deck.commanders)
        ]

    def account_labels(self) -> list[tuple[str, str]] | None:
        """The account's card labels as (name, colour), as last read from Archidekt; None if never read."""
        path = self.root / LABELS_FILE
        try:
            return parse_labels(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, ShapeError) as error:
            # A copy of Archidekt data: the next sync or label change reads it again.
            log.warning("ignoring unreadable %s: %s", path, error)
            return None

    def save_account_labels(self, labels: list[tuple[str, str]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        document = {
            "version": LABELS_VERSION,
            "labels": [{"name": name, "colour": colour} for name, colour in labels],
        }
        write_text_atomic(self.root / LABELS_FILE, json.dumps(document, ensure_ascii=False, indent=2) + "\n")

    def save(self, deck: Deck) -> Path:
        """Write deck.json and list.md, renaming the folder to match the deck name; never touches notes.md."""
        target = self.root / folder_name(deck)
        existing = self.folder_for(deck.id)
        if existing is not None and existing != target:
            if target.exists():
                raise WorkspaceError(f"cannot rename {existing.name} to {target.name}: it already exists")
            existing.rename(target)
        target.mkdir(parents=True, exist_ok=True)
        write_text_atomic(target / SNAPSHOT_FILE, snapshot_json(deck))
        write_text_atomic(target / LIST_FILE, deck_list_markdown(deck))
        notes = target / NOTES_FILE
        if not notes.exists():
            write_text_atomic(notes, notes_template(deck))
        return target


def parse_labels(raw: object) -> list[tuple[str, str]]:
    document = as_object(raw, "labels")
    if req_int(document, "version", "labels") != LABELS_VERSION:
        raise ShapeError(f"labels version is not {LABELS_VERSION}")
    labels: list[tuple[str, str]] = []
    for index, item in enumerate(req_list(document, "labels", "labels")):
        where = f"labels[{index}]"
        entry = as_object(item, where)
        labels.append((req_str(entry, "name", where), req_str(entry, "colour", where)))
    return labels


def folder_name(deck: Deck) -> str:
    slug = slugify(deck.name)
    return f"{deck.id}-{slug}" if slug else str(deck.id)


def slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug[:_MAX_SLUG].rstrip("-")


def snapshot_json(deck: Deck) -> str:
    document = {
        "version": SNAPSHOT_VERSION,
        "id": deck.id,
        "name": deck.name,
        "owner": deck.owner,
        "bracket": deck.bracket,
        "updatedAt": deck.updated_at,
        "tags": None if deck.tags is None else list(deck.tags),
        "private": deck.private,
        "unlisted": deck.unlisted,
        "cards": [
            {
                "name": card.name,
                "quantity": card.quantity,
                "categories": list(card.categories),
                "inDeck": card.in_deck,
                "scryfallId": card.scryfall_id,
                "archidektCardId": card.archidekt_card_id,
                "entryId": card.entry_id,
                "set": card.set_code,
                "collectorNumber": card.collector_number,
                "finish": card.finish,
                "label": card.label,
                "labelColour": card.label_colour,
            }
            for card in deck.cards
        ],
    }
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def parse_snapshot(raw: object) -> Deck:
    document = _migrate(as_object(raw, "snapshot"))
    cards: list[DeckCard] = []
    for index, item in enumerate(req_list(document, "cards", "snapshot")):
        where = f"snapshot.cards[{index}]"
        card = as_object(item, where)
        cards.append(
            DeckCard(
                name=req_str(card, "name", where),
                quantity=req_int(card, "quantity", where),
                categories=str_list(card, "categories", where),
                in_deck=req_bool(card, "inDeck", where),
                scryfall_id=req_str(card, "scryfallId", where),
                archidekt_card_id=req_int(card, "archidektCardId", where),
                entry_id=req_int(card, "entryId", where),
                set_code=req_str(card, "set", where),
                collector_number=req_str(card, "collectorNumber", where),
                finish=req_str(card, "finish", where),
                label=opt_str(card, "label", where),
                label_colour=opt_str(card, "labelColour", where) or "",
            )
        )
    return Deck(
        id=req_int(document, "id", "snapshot"),
        name=req_str(document, "name", "snapshot"),
        owner=req_str(document, "owner", "snapshot"),
        bracket=opt_int(document, "bracket", "snapshot"),
        updated_at=req_str(document, "updatedAt", "snapshot"),
        cards=tuple(cards),
        tags=_tags(document),
        # Snapshots from before sync read private decks have neither key; those were all public.
        private=opt_bool(document, "private", "snapshot") or False,
        unlisted=opt_bool(document, "unlisted", "snapshot") or False,
    )


def _tags(document: Mapping[str, object]) -> tuple[str, ...] | None:
    if "tags" not in document:
        raise ShapeError("snapshot.tags is missing")
    return None if document["tags"] is None else str_list(document, "tags", "snapshot")


def _migrate(document: Mapping[str, object]) -> Mapping[str, object]:
    # Migrations from older snapshot versions go here, on the raw tree, before binding to Deck.
    version = req_int(document, "version", "snapshot")
    if version == 1:
        # Version 1 kept no deck tags; None says they are unknown, so the next sync fetches the deck again.
        document = {**document, "version": 2, "tags": None}
        version = 2
    if version in {2, 3}:
        # Version 2 kept no card labels and version 3 no label colours; None says the labels are unknown,
        # so the next sync fetches the deck again.
        cards = [
            {**as_object(c, "snapshot.cards[]"), "label": None, "labelColour": None}
            for c in req_list(document, "cards", "snapshot")
        ]
        document = {**document, "version": 4, "cards": cards}
        version = 4
    if version != SNAPSHOT_VERSION:
        raise ShapeError(f"snapshot version {version} is not supported (expected {SNAPSHOT_VERSION})")
    return document


def _read_snapshot(path: Path) -> Deck | None:
    # A snapshot is a copy of Archidekt data, so an unreadable one is reported and refetched, not fatal.
    try:
        return parse_snapshot(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, ShapeError, ValueError) as error:
        log.warning("ignoring unreadable snapshot %s: %s", path, error)
        return None


def _folder_deck_id(name: str) -> int | None:
    match = re.fullmatch(r"(\d+)(?:-.*)?", name)
    return int(match.group(1)) if match else None
