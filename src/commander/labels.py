"""Mark the cards a deck still has to get as ordered or arrived, by their Archidekt label (ADR 0010).

A card saved into a deck carries "Don't Have"; ordered, it carries "Getting"; arrived, no label. Each mark
is written to Archidekt at once and needs no snapshot: a label never changes which cards are in the deck.
"""

import logging
from dataclasses import dataclass, replace
from enum import Enum

from commander.archidekt import ArchidektError
from commander.archidekt_edit import ArchidektEditor, AuthError, CardEdit, EditKind, label_value
from commander.board import Actor, BoardService
from commander.models import GETTING_LABEL, Deck
from commander.net import NetworkError
from commander.store import StoredDeck, Workspace

log = logging.getLogger(__name__)


class LabelError(Exception):
    """The mark cannot be made; the message says why."""


class Step(Enum):
    ORDERED = "ordered"
    ARRIVED = "arrived"

    @property
    def label(self) -> str:
        return GETTING_LABEL if self is Step.ORDERED else ""


@dataclass(slots=True, frozen=True)
class Marked:
    deck: Deck
    card: str
    before: str
    after: str


def to_get_json(deck: Deck) -> list[dict[str, object]]:
    """The deck's cards still to get, by name, for the app and `--json`."""
    return [
        {"name": c.name, "quantity": c.quantity, "label": c.label, "category": c.primary_category}
        for c in sorted(deck.to_get, key=lambda c: c.name.casefold())
    ]


def mark(
    editor: ArchidektEditor,
    workspace: Workspace,
    stored: StoredDeck,
    service: BoardService,
    *,
    name: str,
    step: Step,
    actor: Actor,
) -> Marked:
    """Set the label of every deck entry of one card for this step, then update the local copy and log it."""
    deck_id = stored.deck.id
    live = editor.get_deck(deck_id)
    entries = [c for c in live.deck.main_cards if c.name.casefold() == name.casefold()]
    if not entries:
        raise LabelError(f"{name} is not in {stored.deck.short_name} on Archidekt")
    card = entries[0].name
    before = entries[0].label or ""
    if all(e.label == step.label for e in entries):
        raise LabelError(f"{card} is already marked {step.value}")
    edits = [
        CardEdit(
            EditKind.MODIFY,
            e.archidekt_card_id,
            e.quantity,
            replace(live.entries[e.entry_id], label=label_value(step.label)),
            e.entry_id,
        )
        for e in entries
        if e.label != step.label
    ]
    editor.modify_cards(deck_id, edits)
    try:
        after = editor.get_deck(deck_id).deck
    except (NetworkError, ArchidektError, AuthError) as error:
        log.info("reading deck %d back after a label change failed: %s", deck_id, error)
        raise LabelError(
            f"Archidekt took the change, but reading the deck back failed ({error}); run `cmdr sync`"
        ) from error
    workspace.save(after)
    service.note(actor, f"marked {step.value}", f"label {before or 'none'} -> {step.label or 'none'}", card)
    return Marked(after, card, before, step.label)
