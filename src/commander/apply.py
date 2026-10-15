"""Turn a plan into checked changes against a deck as it is on Archidekt now, and save them (ADR 0004)."""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum

from commander.analysis import game_changers
from commander.archidekt import ArchidektError
from commander.archidekt_edit import (
    ArchidektEditor,
    AuthError,
    CardEdit,
    EditKind,
    EntryState,
    LiveDeck,
    new_entry_state,
)
from commander.carddb import CardDb, CardInfo, Printing
from commander.models import Deck, DeckCard
from commander.net import NetworkError
from commander.plan import AddCard, Plan, RemoveCard
from commander.suggest import BracketRaise, card_raise, deck_identity

log = logging.getLogger(__name__)

# The category Archidekt files a new card under is its first card type in this order.
_TYPE_CATEGORIES = (
    "Land",
    "Creature",
    "Planeswalker",
    "Battle",
    "Instant",
    "Sorcery",
    "Artifact",
    "Enchantment",
)
COMMANDER_DECK_SIZE = 100
# A save creates a category the deck lacks. Seen live on 2026-10-15: Archidekt creates Maybeboard outside
# the deck, and every other name, Sideboard included, inside it.
_ASIDE_WHEN_CREATED = frozenset({"maybeboard"})
_USUALLY_ASIDE = frozenset({"sideboard"})


class ChangeKind(Enum):
    ADD = "add"
    MORE = "more copies"
    FEWER = "fewer copies"
    REMOVE = "remove"


@dataclass(slots=True, frozen=True)
class Change:
    kind: ChangeKind
    name: str
    before: int
    after: int
    category: str
    in_deck: bool
    # The printing: for an add the one chosen, otherwise the deck's own.
    set_code: str
    collector_number: str
    scryfall_id: str
    price_cents: int | None = None
    raises: BracketRaise | None = None
    # Known for cards already in the deck; looked up on Archidekt for adds when saving.
    card_id: int | None = None
    entry_id: int | None = None


@dataclass(slots=True, frozen=True)
class Resolution:
    deck: Deck
    changes: tuple[Change, ...]
    problems: tuple[str, ...]
    warnings: tuple[str, ...]
    # Lines of the plan the deck already meets.
    skipped: tuple[str, ...] = ()

    @property
    def main_before(self) -> int:
        return self.deck.main_count

    @property
    def main_after(self) -> int:
        delta = sum(c.after - c.before for c in self.changes if c.in_deck)
        return self.deck.main_count + delta


@dataclass(slots=True)
class _Resolver:
    deck: Deck
    db: CardDb
    categories: Mapping[str, bool]
    problems: list[str] = field(default_factory=list[str])
    warnings: list[str] = field(default_factory=list[str])
    skipped: list[str] = field(default_factory=list[str])
    changes: list[Change] = field(default_factory=list[Change])
    # A card can be named by its full name or its front face, so the plan's own check on names
    # misses repeats; these catch them by card and by deck entry.
    added: set[str] = field(default_factory=set[str])
    removed_entries: set[int] = field(default_factory=set[int])

    def remove(self, wanted: RemoveCard) -> None:
        # Only cards counted in the deck; a maybeboard copy stays.
        entries = [e for e in _entries_named(self.deck, wanted.name) if e.in_deck]
        if any(e.entry_id in self.removed_entries for e in entries):
            self.problems.append(f"remove {wanted.name}: the plan already removes this card")
            return
        self.removed_entries.update(e.entry_id for e in entries)
        held = sum(e.quantity for e in entries)
        if held <= wanted.keep:
            what = "not in the deck" if not entries else f"already {held}"
            self.skipped.append(f"remove {wanted.name}: {what}")
            return
        if wanted.keep == 0:
            self.changes.extend(_change(ChangeKind.REMOVE, entry, 0) for entry in entries)
            return
        if len(entries) > 1:
            self.problems.append(
                f"remove {wanted.name}: the deck has it in {len(entries)} entries; remove it whole, "
                "or change the copies on Archidekt"
            )
            return
        self.changes.append(_change(ChangeKind.FEWER, entries[0], wanted.keep))

    def add(self, wanted: AddCard, identity: str | None, changers: int) -> int:
        """Resolve one add; returns the game changers the deck holds after it."""
        info = self.db.card_named(wanted.name)
        if info is None:
            self.problems.append(f"add {wanted.name}: no such card in the card data (check the spelling)")
            return changers
        held = [e for e in _entries_named(self.deck, info.name) if e.in_deck]
        problem = self._add_problem(info, identity, held)
        if problem is not None:
            self.problems.append(f"add {info.name}: {problem}")
            return changers
        if wanted.quantity > 1 and not info.front_type_line.startswith("Basic"):
            self.warnings.append(
                f"{info.name}: {wanted.quantity} copies; Commander allows one unless the card says so"
            )
        if held:
            entry = held[0]
            have = sum(e.quantity for e in held)
            if have >= wanted.quantity:
                self.skipped.append(f"add {info.name}: already {have} in the deck")
            else:
                self.changes.append(_change(ChangeKind.MORE, entry, entry.quantity + wanted.quantity - have))
            return changers
        printing = self._printing(info, wanted)
        if printing is None:
            return changers
        raises = card_raise(info, self.deck.bracket, changers)
        category = wanted.category or default_category(info)
        if category not in self.categories and category.casefold() in _USUALLY_ASIDE:
            self.warnings.append(
                f"{info.name}: Archidekt creates the new category {category} counting in the deck; "
                "untick it on Archidekt to keep it out"
            )
        self.changes.append(
            Change(
                kind=ChangeKind.ADD,
                name=info.name,
                before=0,
                after=wanted.quantity,
                category=category,
                in_deck=counts_in_deck(category, self.categories),
                set_code=printing.set_code,
                collector_number=printing.collector_number,
                scryfall_id=printing.scryfall_id,
                price_cents=printing.eur_cents,
                raises=raises,
            )
        )
        return changers + 1 if info.game_changer else changers

    def _add_problem(self, info: CardInfo, identity: str | None, held: Sequence[DeckCard]) -> str | None:
        if info.oracle_id in self.added:
            return "the plan already adds this card"
        self.added.add(info.oracle_id)
        if not info.commander_legal:
            return "not legal in Commander"
        if identity is not None and not set(info.color_identity) <= set(identity):
            return f"its colours ({info.color_identity}) are outside the deck's ({identity or 'colourless'})"
        if any(e.entry_id in self.removed_entries for e in held):
            return "the plan also removes this card"
        return None

    def _printing(self, info: CardInfo, wanted: AddCard) -> Printing | None:
        printings = self.db.printings_of(info.oracle_id)
        if wanted.set_code is not None:
            printings = [p for p in printings if p.set_code == wanted.set_code]
            if not printings:
                self.problems.append(f"add {info.name}: no printing in set {wanted.set_code!r}")
                return None
        elif any(p.regular for p in printings):
            # A gold-bordered or oversized copy is often the cheapest; it takes naming its set.
            printings = [p for p in printings if p.regular]
        priced = [p for p in printings if p.eur_cents is not None]
        if priced:
            return min(priced, key=lambda p: (p.eur_cents or 0, p.set_code, p.collector_number))
        if wanted.set_code is not None:
            return printings[0]
        self.problems.append(f'add {info.name}: no printing has a EUR price; pick one with set = "..."')
        return None


def resolve_plan(
    live: Deck, plan: Plan, db: CardDb, *, categories: Mapping[str, bool] | None = None
) -> Resolution:
    """Check every change in the plan against the deck; problems block saving, warnings do not.

    categories holds the deck's own setting of which categories count in the deck.
    """
    resolver = _Resolver(live, db, categories or {})
    main = db.resolve(live.main_cards)
    identity = deck_identity(main)
    if identity is None:
        resolver.warnings.append("the deck has no known commander, so colours are not checked")
    for wanted in plan.remove:
        resolver.remove(wanted)
    changers = len(game_changers(main))
    for wanted in plan.add:
        changers = resolver.add(wanted, identity, changers)
    resolution = Resolution(
        live,
        tuple(resolver.changes),
        tuple(resolver.problems),
        tuple(resolver.warnings),
        tuple(resolver.skipped),
    )
    if resolution.changes and not resolution.problems and resolution.main_after != COMMANDER_DECK_SIZE:
        size = f"the deck will have {resolution.main_after} cards, not {COMMANDER_DECK_SIZE}"
        resolution = replace(resolution, warnings=(*resolution.warnings, size))
    return resolution


def counts_in_deck(category: str, categories: Mapping[str, bool]) -> bool:
    """Whether a card filed under category counts in the deck, by the deck's setting or else by default."""
    if category in categories:
        return categories[category]
    return category.casefold() not in _ASIDE_WHEN_CREATED


def default_category(info: CardInfo) -> str:
    front = info.front_type_line
    return next((t for t in _TYPE_CATEGORIES if t in front.split("—")[0].split()), "Uncategorized")


class SaveError(Exception):
    """Saving stopped; the message says whether anything reached Archidekt."""


@dataclass(slots=True, frozen=True)
class SaveResult:
    deck: Deck
    snapshot: str
    mismatches: tuple[str, ...]


def save(
    editor: ArchidektEditor,
    live: LiveDeck,
    resolution: Resolution,
    *,
    now: Callable[[], datetime],
    on_progress: Callable[[str], None] = lambda _: None,
) -> SaveResult:
    """Look up new cards, snapshot the deck on Archidekt, send every change at once, then read it back."""
    if resolution.problems:
        raise ValueError("a resolution with problems cannot be saved")
    if not resolution.changes:
        raise ValueError("nothing to save")
    deck = live.deck
    card_ids: dict[str, int] = {}
    for change in resolution.changes:
        if change.kind is ChangeKind.ADD:
            on_progress(f"looking up {change.name} ({change.set_code.upper()}) on Archidekt")
            card_id = editor.find_card(change.name, change.set_code, change.scryfall_id)
            if card_id is None:
                raise SaveError(
                    f"Archidekt has no {change.name} from {change.set_code.upper()}"
                    f' {change.collector_number}; pick another printing with set = "...".'
                    " Nothing was changed"
                )
            card_ids[change.scryfall_id] = card_id
    edits = card_edits(resolution.changes, live.entries, card_ids)
    # Seconds keep two saves in one minute apart; Archidekt accepts snapshots with equal names.
    snapshot = f"before cmdr apply {now().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    on_progress(f"saving snapshot '{snapshot}' on Archidekt")
    editor.create_snapshot(deck.id, snapshot, "Restore point saved by cmdr before applying a plan.")
    on_progress(f"sending {len(edits)} change(s) to deck {deck.id}")
    try:
        editor.modify_cards(deck.id, edits)
    except (NetworkError, ArchidektError, AuthError) as error:
        log.info("saving deck %d failed after the snapshot: %s", deck.id, error)
        raise SaveError(
            f"{error}. The snapshot '{snapshot}' is on Archidekt; check the deck there before trying again"
        ) from error
    on_progress("reading the deck back")
    try:
        after = editor.get_deck(deck.id).deck
    except (NetworkError, ArchidektError) as error:
        raise SaveError(
            f"Archidekt accepted the changes, but reading the deck back failed ({error}); "
            "run `cmdr sync` to check the deck and update the local copy"
        ) from error
    return SaveResult(after, snapshot, tuple(verify(resolution.changes, deck, after)))


def card_edits(
    changes: Sequence[Change], entries: Mapping[int, EntryState], card_ids: Mapping[str, int]
) -> list[CardEdit]:
    edits: list[CardEdit] = []
    for change in changes:
        if change.kind is ChangeKind.ADD:
            card_id = card_ids[change.scryfall_id]
            edits.append(CardEdit(EditKind.ADD, card_id, change.after, new_entry_state(change.category)))
            continue
        if change.entry_id is None or change.card_id is None:
            raise ArchidektError(f"{change.name}: the deck entry is unknown")
        state = entries.get(change.entry_id)
        if state is None:
            raise ArchidektError(f"{change.name}: entry {change.entry_id} is no longer in the deck")
        if change.kind is ChangeKind.REMOVE:
            edits.append(CardEdit(EditKind.REMOVE, change.card_id, change.before, state, change.entry_id))
        else:
            edits.append(CardEdit(EditKind.MODIFY, change.card_id, change.after, state, change.entry_id))
    return edits


def verify(changes: Sequence[Change], before: Deck, after: Deck) -> list[str]:
    """Every change that the deck read back from Archidekt does not show."""
    # An add shows as a new entry; a copy already elsewhere, such as on the maybeboard, must not count.
    old_entries = {c.entry_id for c in before.cards}
    mismatches: list[str] = []
    for change in changes:
        if change.kind is ChangeKind.ADD:
            found = sum(
                c.quantity
                for c in after.cards
                if c.scryfall_id == change.scryfall_id and c.entry_id not in old_entries
            )
            if found < change.after:
                mismatches.append(f"{change.name}: expected {change.after} added, Archidekt shows {found}")
            continue
        entry = next((c for c in after.cards if c.entry_id == change.entry_id), None)
        found = entry.quantity if entry else 0
        if found != change.after:
            mismatches.append(f"{change.name}: expected {change.after}, Archidekt shows {found}")
    return mismatches


class NotYourDeckError(Exception):
    """The deck belongs to another Archidekt user; cmdr edits only the user's own decks."""


@dataclass(slots=True, frozen=True)
class ApplyOutcome:
    resolution: Resolution
    # None for a preview, or when the plan has problems or nothing to change.
    result: SaveResult | None


def apply_plan(
    editor: ArchidektEditor,
    deck_id: int,
    plan: Plan,
    db: CardDb,
    *,
    user: str,
    save_changes: bool,
    now: Callable[[], datetime],
    on_progress: Callable[[str], None] = lambda _: None,
) -> ApplyOutcome:
    """Check a plan against the deck as it is on Archidekt now; save it when asked and nothing is wrong."""
    live = editor.get_deck(deck_id)
    if live.deck.owner.casefold() != user.casefold():
        raise NotYourDeckError(
            f"deck {deck_id} belongs to {live.deck.owner}, not {user}; cmdr edits only yours"
        )
    resolution = resolve_plan(live.deck, plan, db, categories=live.categories)
    if not (save_changes and resolution.changes and not resolution.problems):
        return ApplyOutcome(resolution, None)
    return ApplyOutcome(resolution, save(editor, live, resolution, now=now, on_progress=on_progress))


def _entries_named(deck: Deck, name: str) -> list[DeckCard]:
    key = name.casefold()
    return [c for c in deck.cards if c.name.casefold() == key or c.name.casefold().split(" // ")[0] == key]


def _change(kind: ChangeKind, entry: DeckCard, after: int) -> Change:
    return Change(
        kind=kind,
        name=entry.name,
        before=entry.quantity,
        after=after,
        category=entry.primary_category,
        in_deck=entry.in_deck,
        set_code=entry.set_code,
        collector_number=entry.collector_number,
        scryfall_id=entry.scryfall_id,
        card_id=entry.archidekt_card_id,
        entry_id=entry.entry_id,
    )
