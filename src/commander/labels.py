"""Set a deck card's Archidekt label, and mark the cards still to get as ordered or arrived (ADR 0010).

A card saved into a deck carries "Don't Have"; ordered, it carries "Getting"; arrived, no label. Each mark
is written to Archidekt at once and needs no snapshot: a label never changes which cards are in the deck.
The labels on offer are the account's own, shared by all its decks, in its colours (ADR 0012).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import Enum

from commander.archidekt import ArchidektError
from commander.archidekt_edit import (
    DEFAULT_LABEL,
    LABEL_COLOURS,
    NO_LABEL_COLOUR,
    ArchidektEditor,
    AuthError,
    CardEdit,
    EditKind,
    label_value,
)
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


def label_options(deck: Deck, account: Sequence[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    """The labels a card in the deck can be given, as (name, colour); "" names no label.

    No label first, then the account's labels by name, as Archidekt's label settings list them, the
    buying steps if the account lacks them, and last any other label the deck already uses, by name.
    """
    options = [("", NO_LABEL_COLOUR), *sorted(account or (), key=lambda label: label[0].casefold())]
    taken = {name for name, _ in options}
    for name, colour in LABEL_COLOURS.items():
        if name not in taken:
            options.append((name, colour))
            taken.add(name)
    for card in sorted(deck.cards, key=lambda c: (c.label or "").casefold()):
        if card.label and card.label not in taken and card.label_colour:
            options.append((card.label, card.label_colour))
            taken.add(card.label)
    return options


def set_label(
    editor: ArchidektEditor,
    workspace: Workspace,
    stored: StoredDeck,
    service: BoardService,
    *,
    name: str,
    label: str,
    actor: Actor,
) -> Marked:
    """Give one deck card any of the deck's label options ("" for none), as the app's label panel does."""
    colours = dict(label_options(stored.deck, _account_labels(editor, workspace)))
    if label not in colours:
        raise LabelError(f"{stored.deck.short_name} has no label {label!r}")
    value = f"{label},{colours[label]}" if label else DEFAULT_LABEL
    action = f"labelled {label}" if label else "unlabelled"
    return _relabel(editor, workspace, stored, service, name, label, value, actor, action)


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
    account = dict(_account_labels(editor, workspace))
    return _relabel(
        editor,
        workspace,
        stored,
        service,
        name,
        step.label,
        label_value(step.label, account),
        actor,
        f"marked {step.value}",
    )


def _account_labels(editor: ArchidektEditor, workspace: Workspace) -> list[tuple[str, str]]:
    # Read fresh before each write, so a label added on the website is on offer at once; kept for the app.
    labels = editor.account_labels()
    workspace.save_account_labels(labels)
    return labels


def _relabel(  # noqa: PLR0917 — the shared body of set_label and mark
    editor: ArchidektEditor,
    workspace: Workspace,
    stored: StoredDeck,
    service: BoardService,
    name: str,
    label: str,
    value: str,
    actor: Actor,
    action: str,
) -> Marked:
    deck_id = stored.deck.id
    live = editor.get_deck(deck_id)
    entries = [c for c in live.deck.main_cards if c.name.casefold() == name.casefold()]
    if not entries:
        raise LabelError(f"{name} is not in {stored.deck.short_name} on Archidekt")
    card = entries[0].name
    before = entries[0].label or ""
    if all(e.label == label for e in entries):
        raise LabelError(f"{card} is already {action}")
    edits = [
        CardEdit(
            EditKind.MODIFY,
            e.archidekt_card_id,
            e.quantity,
            replace(live.entries[e.entry_id], label=value),
            e.entry_id,
        )
        for e in entries
        if e.label != label
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
    service.note(actor, action, f"label {before or 'none'} -> {label or 'none'}", card)
    return Marked(after, card, before, label)
