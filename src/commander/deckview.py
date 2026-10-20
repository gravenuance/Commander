"""The app's decklist as the page draws it: the grid groups with each card's queued change and its + and -,
the cards outside the deck, the deck's categories for an add, and the stats above it (ADR 0042, 0043)."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from commander.answers import card_key
from commander.board import MAX_COPIES, Board, CountChange
from commander.carddb import CardDb, CardInfo, ResolvedCard
from commander.copylimit import copy_limit
from commander.deckgrid import GridCard, GridGroup, PendingAdd, aside_grid, deck_grid, image_url
from commander.deckstats import deck_stats, stat_cards, stats_after
from commander.models import COMMANDER_CATEGORY, Deck

type Json = dict[str, Any]


def card_image(db: CardDb, info: CardInfo) -> str | None:
    """The image of the card's latest regular printing; None when the card data has no printing."""
    printings = db.printings_of(info.oracle_id)
    regular = [p for p in printings if p.regular] or printings
    if not regular:
        return None
    return image_url(regular[-1].scryfall_id)


def decklist_json(
    deck: Deck,
    board: Board,
    db: CardDb,
    *,
    colours: Mapping[str, str],
    share: Callable[[str], Json | None],
) -> Json:
    """The grid (deck groups, then the groups outside the deck), the deck's categories and its stats.

    A deck card carries its queued count change and whether + and - apply: - on any card but a commander,
    + on a card allowed in multiples (or back up to the copies the deck holds), neither on a card a queued
    swap brings in or cuts. A card queued to come in sits in its type's group with the id of its board card.
    The stats are the saved deck's, and, while changes are queued, the deck's as a save would leave it.
    """
    main = db.resolve(deck.main_cards)
    outside = db.resolve(tuple(card for card in deck.cards if not card.in_deck))
    held = {card_key(card.name) for card in deck.main_cards}
    edits = _Edits(
        board,
        _copy_limits(main, db),
        frozenset(card_key(name) for card in board.queued for name in (card.name, card.choice) if name),
    )
    groups = [
        _group_json(group, colours, share, edits)
        for group in (*deck_grid(main, _pending_adds(board, db, held)), *aside_grid(outside))
    ]
    saved = deck_stats(stat_cards(main))
    after = stats_after(deck, main, board, db) if board.pending else saved
    return {
        "grid": groups,
        "categories": sorted(
            {c.primary_category for c in deck.main_cards if COMMANDER_CATEGORY not in c.categories},
            key=str.casefold,
        ),
        "stats": {"saved": saved.json(), "after": after.json()},
    }


@dataclass(slots=True, frozen=True)
class _Edits:
    """What decides a grid card's queued change and its + and -."""

    board: Board
    # Each deck card's copy limit by card_key; None for any number.
    limits: Mapping[str, int | None]
    # The card_keys a queued swap brings in or cuts.
    swapped: frozenset[str]

    def json(self, card: GridCard, group: GridGroup) -> Json:
        if card.board_id is not None:
            return {"pending": {"kind": "add", "id": card.board_id}, "more": False, "less": False}
        queued = self.board.count_of(card.name) if group.in_deck else None
        pending = {"kind": "count", "was": queued.was, "count": queued.count} if queued else None
        editable = (
            group.in_deck and group.name != COMMANDER_CATEGORY and card_key(card.name) not in self.swapped
        )
        return {
            "pending": pending,
            "more": editable and _more_allowed(card, queued, self.limits),
            "less": editable and (queued.count if queued else card.quantity) > 0,
        }


def _pending_adds(board: Board, db: CardDb, held: set[str]) -> list[PendingAdd]:
    # A basic land swap adds copies of a card the deck holds: its grid card already stands for it.
    queued = [card for card in board.queued if card_key(card.name) not in held]
    infos = db.cards(card.oracle_id for card in queued)
    return [
        PendingAdd(card.id, info, card.copies, card.category, card_image(db, info) or "")
        for card in queued
        if (info := infos.get(card.oracle_id)) is not None
    ]


def _copy_limits(main: Sequence[ResolvedCard], db: CardDb) -> dict[str, int | None]:
    texts = db.card_texts(r.info.oracle_id for r in main if r.info is not None)
    return {
        card_key(r.card.name): copy_limit(r.info, text.text if (text := texts.get(r.info.oracle_id)) else "")
        for r in main
        if r.info is not None
    }


def _group_json(
    group: GridGroup, colours: Mapping[str, str], share: Callable[[str], Json | None], edits: _Edits
) -> Json:
    """One grid group; a label the account keeps shows in the account's colour (ADR 0012). The commander
    is in every list by definition, so it carries no share."""
    return {
        "key": group.name if group.in_deck else f"outside:{group.name}",
        "name": group.name,
        "inDeck": group.in_deck,
        "count": group.count,
        "cents": group.cents,
        "cards": [
            {
                "name": c.name,
                "quantity": c.quantity,
                "image": c.image,
                "category": c.category,
                "label": c.label,
                "labelColour": colours.get(c.label, c.label_colour) if c.label else c.label_colour,
                "cents": c.cents,
                "share": None if group.name == COMMANDER_CATEGORY else share(c.name),
                **edits.json(c, group),
            }
            for c in group.cards
        ],
    }


def _more_allowed(card: GridCard, queued: CountChange | None, limits: Mapping[str, int | None]) -> bool:
    current = queued.count if queued else card.quantity
    if current < card.quantity:
        return True
    limit = limits.get(card_key(card.name), 1)
    return current < (MAX_COPIES if limit is None else min(limit, MAX_COPIES))
