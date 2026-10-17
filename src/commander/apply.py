"""Turn a plan into checked changes against a deck as it is on Archidekt now, and save them (ADR 0004)."""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum

from commander.analysis import game_changers
from commander.archidekt import ArchidektError
from commander.archidekt_edit import (
    DEFAULT_LABEL,
    ArchidektEditor,
    AuthError,
    CardEdit,
    EditKind,
    EntryState,
    LiveDeck,
    ReadBackError,
    label_value,
    new_entry_state,
)
from commander.carddb import CardDb, CardInfo, Printing
from commander.models import (
    ASIDE_CATEGORIES,
    COMMANDER_CATEGORY,
    DONT_HAVE_LABEL,
    MAYBEBOARD_CATEGORY,
    Deck,
    DeckCard,
)
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
    # An entry from the Sideboard or Maybeboard, moved into the deck with its own printing.
    MOVE_IN = "into the deck"
    # A deck entry moved to the Maybeboard, so it stays under consideration.
    SET_ASIDE = "to the maybeboard"
    # An entry taken off the Sideboard or Maybeboard: no longer under consideration.
    DROP = "off the maybeboard"


_MOVES = frozenset({ChangeKind.MOVE_IN, ChangeKind.SET_ASIDE})


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
    # A move's categories after it, the first one deciding where the card counts.
    categories: tuple[str, ...] = ()
    # Whether the entry counted in the deck before the change; None means as in_deck.
    was_in_deck: bool | None = None

    @property
    def counted_before(self) -> int:
        counted = self.in_deck if self.was_in_deck is None else self.was_in_deck
        return self.before if counted else 0

    @property
    def counted_after(self) -> int:
        return self.after if self.in_deck else 0


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
        delta = sum(c.counted_after - c.counted_before for c in self.changes)
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
        if any(COMMANDER_CATEGORY in e.categories for e in entries):
            self.problems.append(f"remove {wanted.name}: it is a commander and stays in the deck")
            return
        if any(e.entry_id in self.removed_entries for e in entries):
            self.problems.append(f"remove {wanted.name}: the plan already removes this card")
            return
        self.removed_entries.update(e.entry_id for e in entries)
        held = sum(e.quantity for e in entries)
        if held <= wanted.keep:
            what = "not in the deck" if not entries else f"already {held}"
            self.skipped.append(f"remove {wanted.name}: {what}")
            return
        if wanted.maybeboard:
            self.changes.extend(self._set_aside(entry) for entry in entries)
            return
        self.changes.extend(_cut_to(entries, held - wanted.keep))

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
        raises = card_raise(info, self.deck.bracket, changers)
        aside = [
            e
            for e in _entries_named(self.deck, info.name)
            if e.aside and e.entry_id not in self.removed_entries
        ]
        if aside:
            self.removed_entries.add(aside[0].entry_id)
            self.changes.append(self._move_in(aside[0], info, wanted, raises))
            return changers + 1 if info.game_changer else changers
        printing = self._printing(info, wanted)
        if printing is None:
            return changers
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

    def drop(self, name: str) -> None:
        entries = [e for e in _entries_named(self.deck, name) if e.aside]
        if not entries:
            self.skipped.append(f"drop {name}: not on the Sideboard or Maybeboard")
            return
        if any(e.entry_id in self.removed_entries for e in entries):
            self.problems.append(f"drop {name}: the plan also moves this card")
            return
        self.removed_entries.update(e.entry_id for e in entries)
        self.changes.extend(_change(ChangeKind.DROP, entry, 0) for entry in entries)

    def _set_aside(self, entry: DeckCard) -> Change:
        aside = self._maybeboard_name()
        rest = tuple(c for c in entry.categories if c.casefold() != aside.casefold())
        # The deck's own categories stay behind the Maybeboard, so moving the card back restores them.
        return replace(
            _change(ChangeKind.SET_ASIDE, entry, entry.quantity),
            category=aside,
            categories=(aside, *rest),
            in_deck=counts_in_deck(aside, self.categories),
            was_in_deck=True,
        )

    def _move_in(
        self, entry: DeckCard, info: CardInfo, wanted: AddCard, raises: BracketRaise | None
    ) -> Change:
        rest = [c for c in entry.categories if c.casefold() not in ASIDE_CATEGORIES]
        category = wanted.category or (rest[0] if rest else default_category(info))
        # The entry keeps its own printing and copies, raised to the count the plan wants.
        if wanted.set_code is not None and wanted.set_code != entry.set_code:
            self.warnings.append(
                f"{info.name}: it comes from the Maybeboard in its own printing "
                f"({entry.set_code.upper()}), not {wanted.set_code.upper()}"
            )
        return replace(
            _change(ChangeKind.MOVE_IN, entry, max(entry.quantity, wanted.quantity)),
            name=info.name,
            category=category,
            categories=(category, *(c for c in rest if c != category)),
            in_deck=counts_in_deck(category, self.categories),
            was_in_deck=False,
            raises=raises,
        )

    def _maybeboard_name(self) -> str:
        """The deck's own spelling of its Maybeboard category, or the usual one for Archidekt to create."""
        known = (name for name in self.categories if name.casefold() == MAYBEBOARD_CATEGORY.casefold())
        return next(known, MAYBEBOARD_CATEGORY)

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
    for name in plan.drop:
        resolver.drop(name)
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


class SaveReadBackError(SaveError, ReadBackError):
    """The changes reached Archidekt, but reading the deck back failed."""


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
    # Don't Have goes on in the colour the account gives it (ADR 0012).
    dont_have = label_value(DONT_HAVE_LABEL, dict(editor.account_labels()))
    edits = card_edits(resolution.changes, live.entries, card_ids, dont_have)
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
        raise SaveReadBackError(
            f"Archidekt accepted the changes, but reading the deck back failed ({error}); "
            "the local copy may be behind"
        ) from error
    return SaveResult(after, snapshot, tuple(verify(resolution.changes, deck, after)))


def card_edits(
    changes: Sequence[Change],
    entries: Mapping[int, EntryState],
    card_ids: Mapping[str, int],
    dont_have: str = label_value(DONT_HAVE_LABEL),
) -> list[CardEdit]:
    """The modifyCards edits for the changes; a card coming into the deck is labelled dont_have, still to buy
    (ADR 0010), unless it already carries a label (Proxy)."""
    edits: list[CardEdit] = []
    for change in changes:
        if change.kind is ChangeKind.ADD:
            card_id = card_ids[change.scryfall_id]
            edits.append(
                CardEdit(EditKind.ADD, card_id, change.after, new_entry_state(change.category, dont_have))
            )
            continue
        if change.entry_id is None or change.card_id is None:
            raise ArchidektError(f"{change.name}: the deck entry is unknown")
        state = entries.get(change.entry_id)
        if state is None:
            raise ArchidektError(f"{change.name}: entry {change.entry_id} is no longer in the deck")
        if change.kind in {ChangeKind.REMOVE, ChangeKind.DROP}:
            edits.append(CardEdit(EditKind.REMOVE, change.card_id, change.before, state, change.entry_id))
            continue
        if change.kind in _MOVES:
            # A move is a modify that sends the entry's new categories.
            state = replace(state, categories=change.categories)
        if change.kind is ChangeKind.MOVE_IN and state.label == DEFAULT_LABEL:
            # Into the deck from the Maybeboard: still to buy, unless it already carries a label (Proxy).
            state = replace(state, label=dont_have)
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
        elif entry is not None and change.kind in _MOVES and entry.primary_category != change.category:
            mismatches.append(
                f"{change.name}: expected it in {change.category}, Archidekt shows {entry.primary_category}"
            )
    return mismatches


class NotYourDeckError(Exception):
    """The deck belongs to another Archidekt user; cmdr edits only the user's own decks."""


@dataclass(slots=True, frozen=True)
class ApplyOutcome:
    resolution: Resolution
    # None for a preview, or when the plan has problems or nothing to change.
    result: SaveResult | None


def fetch_own_deck(editor: ArchidektEditor, deck_id: int, *, user: str) -> LiveDeck:
    """The deck as it is on Archidekt now; refuses another user's deck."""
    live = editor.get_deck(deck_id)
    if live.deck.owner.casefold() != user.casefold():
        raise NotYourDeckError(
            f"deck {deck_id} belongs to {live.deck.owner}, not {user}; cmdr edits only yours"
        )
    return live


def apply_to_live(
    editor: ArchidektEditor,
    live: LiveDeck,
    plan: Plan,
    db: CardDb,
    *,
    save_changes: bool,
    now: Callable[[], datetime],
    on_progress: Callable[[str], None] = lambda _: None,
) -> ApplyOutcome:
    """Check a plan against a deck fetched from Archidekt; save it when asked and nothing is wrong."""
    resolution = resolve_plan(live.deck, plan, db, categories=live.categories)
    if not (save_changes and resolution.changes and not resolution.problems):
        return ApplyOutcome(resolution, None)
    return ApplyOutcome(resolution, save(editor, live, resolution, now=now, on_progress=on_progress))


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
    live = fetch_own_deck(editor, deck_id, user=user)
    return apply_to_live(editor, live, plan, db, save_changes=save_changes, now=now, on_progress=on_progress)


def _entries_named(deck: Deck, name: str) -> list[DeckCard]:
    key = name.casefold()
    return [c for c in deck.cards if c.name.casefold() == key or c.name.casefold().split(" // ")[0] == key]


def _cut_to(entries: Sequence[DeckCard], cut: int) -> list[Change]:
    """Take cut copies out of the entries, the last entry first; an emptied entry is removed whole."""
    changes: list[Change] = []
    for entry in reversed(entries):
        take = min(entry.quantity, cut)
        if take == 0:
            break
        cut -= take
        left = entry.quantity - take
        changes.append(_change(ChangeKind.REMOVE if left == 0 else ChangeKind.FEWER, entry, left))
    changes.reverse()
    return changes


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
