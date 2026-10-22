"""Running the goldfish for a deck and for swaps on it (ADR 0054): the report, a swap's comparison against it,
the few figures the app shows beside a card, and the results kept between asks."""

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from commander.answers import card_key
from commander.board import Board, BoardCard, Status
from commander.carddb import CardDb
from commander.goldfish import (
    BRACKET_TURN,
    CEDH_BRACKET,
    GAMES,
    FigureChange,
    FigureKind,
    GoldfishComparison,
    GoldfishReport,
    PartialSwap,
    Swap,
    compare_deck,
    goldfish,
)
from commander.goldfish_load import GoldfishError, GoldfishInput, incoming_card

log = logging.getLogger(__name__)

# The turn of the figures shown beside a card in a bracket 5 deck, which has no turn of its own.
CEDH_HEADLINE_TURN = 3
# A deck's comparisons kept at once; a Review of many cards asks for each card alone and all together.
MAX_KEPT_COMPARISONS = 64
_HEADLINE_PER_TURN = frozenset(
    {FigureKind.COMMANDER_CAST_BY, FigureKind.MANA_WORST_TENTH, FigureKind.COMBO_LANDED_BY}
)


@dataclass(slots=True, frozen=True)
class SwapRequest:
    """A card to put in and the deck card it takes the place of, `copies` of each."""

    add: str
    cut: str
    copies: int = 1


@dataclass(slots=True, frozen=True)
class GoldfishRun:
    """A deck's inputs and the report of its games."""

    loaded: GoldfishInput
    report: GoldfishReport


def seed_of(loaded: GoldfishInput) -> int:
    """The deck's id, so the same deck gives the same games each time."""
    return loaded.context.deck.id


def run(loaded: GoldfishInput, *, games: int = GAMES) -> GoldfishRun:
    """Play the deck out, `GAMES` times unless told otherwise."""
    report = goldfish(loaded.deck, bracket=loaded.bracket, seed=seed_of(loaded), games=games)
    return GoldfishRun(loaded, report)


def swap_of(card: BoardCard) -> SwapRequest | None:
    """The swap a board card stands for: queued to be added with a deck card chosen to make room."""
    if card.status is Status.ADD and card.choice:
        return SwapRequest(card.name, card.choice, card.copies)
    return None


def queued_swaps(board: Board) -> list[SwapRequest]:
    """The swaps of the cards queued with a cut, in the board's order."""
    return [swap for card in board.cards if (swap := swap_of(card)) is not None]


def compare_swaps(
    played: GoldfishRun, db: CardDb, swaps: Sequence[SwapRequest], *, lenient: bool = False
) -> GoldfishComparison:
    """The deck's figures with `swaps` made against the games already played, on the same draws.

    The incoming card takes the slots of the copies cut, so the deck plays with the copies that are left of
    the cut card (Cultivate for one of ten Forests plays nine Forests). A swap raises GoldfishError saying
    why when it cuts more copies than the deck holds, a cut that is not in the deck's library, one cut twice,
    an incoming card put in twice, one the deck holds or one the card data lacks. With `lenient` such a swap
    is logged and left out instead, as the app's board may hold one.
    """
    loaded = played.loaded
    cut_keys: set[str] = set()
    incoming_keys: set[str] = set()
    pairs: list[Swap] = []
    for request in swaps:
        try:
            pairs.append(_pair(loaded, db, request, cut_keys, incoming_keys))
        except GoldfishError as error:
            if not lenient:
                raise
            log.info("goldfish: %s for %s left out of the comparison: %s", request.add, request.cut, error)
    return compare_deck(loaded.deck, played.report, pairs, seed=seed_of(loaded))


def _pair(
    loaded: GoldfishInput,
    db: CardDb,
    request: SwapRequest,
    cut_keys: set[str],
    incoming_keys: set[str],
) -> Swap:
    """The cut card and the incoming one, checking that the swap can be played; a swap that passes adds its
    cut to `cut_keys` and its incoming card to `incoming_keys`, a refused one adds neither."""
    key = card_key(request.cut)
    cut = next((card for card in loaded.deck.cards if card_key(card.name) == key), None)
    if cut is None:
        commander = any(card_key(card.name) == key for card in loaded.deck.commanders)
        why = "a commander" if commander else "not in the deck"
        raise GoldfishError(f"{request.cut} is {why}, so it cannot be swapped out")
    if key in cut_keys:
        raise GoldfishError(f"{cut.name} is cut twice")
    if not 1 <= request.copies <= cut.copies:
        raise GoldfishError(f"the deck holds {cut.copies} of {cut.name} and {request.copies} are swapped")
    incoming = incoming_card(loaded, db, request.add, request.copies)
    in_deck = (*loaded.deck.cards, *loaded.deck.commanders)
    if any(card_key(card.name) == card_key(incoming.name) for card in in_deck):
        raise GoldfishError(f"{incoming.name} is in the deck already")
    incoming_key = card_key(incoming.name)
    if incoming_key in incoming_keys:
        raise GoldfishError(f"{incoming.name} is put in twice")
    cut_keys.add(key)
    incoming_keys.add(incoming_key)
    if request.copies == cut.copies:
        return cut, incoming
    return PartialSwap(cut, incoming, request.copies)


def headline_turn(report: GoldfishReport) -> int:
    """The turn the headline figures are read at: the bracket's (9, 8, 6 and 4 turns for brackets 1 to 4,
    the third in bracket 5), or the nearest shown if that one is not."""
    aim = CEDH_HEADLINE_TURN if report.keep_bracket == CEDH_BRACKET else BRACKET_TURN[report.keep_bracket]
    return min(report.turns, key=lambda turn: (abs(turn - aim), turn))


def headline(comparison: GoldfishComparison) -> list[FigureChange]:
    """The figures beside a card in the app, those that moved: each commander's cast-by share, the worst
    tenth of mana, and each combo's landed-by share, all at the headline turn, and the colour screw."""
    turn = headline_turn(comparison.base)
    return [
        change
        for change in comparison.moved
        if change.kind is FigureKind.COLOUR_SCREW
        or (change.kind in _HEADLINE_PER_TURN and change.turn == turn)
    ]


class GoldfishCache:
    """Reports and comparisons kept per deck until the deck's stamp changes, one run at a time per deck so two
    callers asking at once play the games once."""

    def __init__(self) -> None:
        self._runs: dict[int, tuple[str, GoldfishRun]] = {}
        self._swaps: dict[int, tuple[str, dict[tuple[SwapRequest, ...], GoldfishComparison]]] = {}
        self._locks: dict[int, threading.RLock] = {}
        self._guard = threading.Lock()

    def clear(self) -> None:
        with self._guard:
            self._runs.clear()
            self._swaps.clear()

    def _lock(self, deck_id: int) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(deck_id, threading.RLock())

    def played(self, deck_id: int, stamp: str, play: Callable[[], GoldfishRun]) -> GoldfishRun:
        with self._lock(deck_id):
            known = self._runs.get(deck_id)
            if known is not None and known[0] == stamp:
                return known[1]
            fresh = play()
            self._runs[deck_id] = (stamp, fresh)
            return fresh

    def compared(
        self,
        deck_id: int,
        stamp: str,
        swaps: tuple[SwapRequest, ...],
        play: Callable[[], GoldfishRun],
        compare: Callable[[GoldfishRun], GoldfishComparison],
    ) -> GoldfishComparison:
        with self._lock(deck_id):
            base = self.played(deck_id, stamp, play)
            known = self._swaps.get(deck_id)
            if known is None or known[0] != stamp:
                known = self._swaps[deck_id] = (stamp, {})
            kept = known[1]
            if swaps not in kept:
                if len(kept) >= MAX_KEPT_COMPARISONS:
                    del kept[next(iter(kept))]
                kept[swaps] = compare(base)
            return kept[swaps]
