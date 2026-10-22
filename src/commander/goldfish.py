"""The goldfish game (ADR 0054): a deck dealt and played many times by one fixed policy, with no opponents.

Pure: the input is the compiled cards of `goldfish_cards`, the output a `GoldfishReport` of figures plus the
outcome of every game. The game loop reads small ints and tuples built once per run, never rules text.

Determinism: game `i` of a run uses `game_rng(seed, i)` and nothing else, and its k-th shuffle takes
`8 * len(slots)` bytes from that stream, whatever was played before, as the numbers of a Fisher-Yates from
the front that is carried out only as far as the game reads (`_Game._settle`). A swap that puts a new
card into a cut card's slots (`compare`) therefore replays the same shuffles, and a game whose outcome
records that the cut slot was never reached (`reached`), and whose searches (`land_search`,
`tutor_look`) never looked at a kind of card the swap changes, is unchanged.
"""

import random
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from itertools import combinations
from math import sqrt
from typing import NamedTuple

from commander.answers import card_key
from commander.goldfish_cards import (
    CHANGELING,
    CardKind,
    EntryCost,
    GoldfishCard,
    GoldfishCombo,
    GoldfishDeck,
    LandDestination,
    ManaNeed,
    Tutor,
    TutorDestination,
)

# Raised whenever the policy or the recognition changes, so kept results are not mixed with new ones.
POLICY_VERSION = 3
GAMES = 10_000
# The turn by which a bracket expects to be able to play before winning or losing (Wizards, 2025-10-21).
BRACKET_TURN = {1: 9, 2: 8, 3: 6, 4: 4}
# A combo landing by the bracket's turn in this share of games or more is warned about.
WARN_SHARE = 0.25
# A finisher with X in its cost counts as castable once this much can go into X.
FINISHER_X = 10

CEDH_BRACKET = 5
_CEDH_TURNS = 5
# From this bracket up a hand is kept for its way to win, below it for its lands and its draw.
_STRICT_KEEP = 4
_TURN_SPREAD = 2
_DEFAULT_BRACKET = 3
_HAND = 7
_FREE_MULLIGANS = 1
_MAX_KEPT_LANDS = 4
_BIG_CURVE = 3.5
_START_LIFE = 40
# Mana paid for with life is used down to this much life (the owner's choice).
_LIFE_FLOOR = 10
# Mox Opal's metalcraft.
_METALCRAFT = 3
# The colours a card's cost gives it, which a tutor for a colour reads.
_CARD_COLOURS = frozenset("WUBRG")
# What one-shot mana is spent on: life alone for anything; rituals and bursts for a mana source, a combo
# piece, a tutor fetching one, a finisher, the commander or a combo landing; Lion's Eye Diamond only for the
# last three, which win or are the commander.
_FOR_NOTHING = 0
_FOR_PLAY = 1
_FOR_WIN = 2
# A one-shot in a plan that is no card in hand: a life payment, a Treasure, or the burst of the creature
# `_BURST - code`.
_LIFE = -1
_TREASURE = -2
_BURST = -3

# Colours as bits, so a mana unit is one small int: the set of colours it can pay.
_BITS = {"W": 1, "U": 2, "B": 4, "R": 8, "G": 16, "C": 32}
_COLOUR_BITS = 63
# A Treasure's unit: any colour.
_ANY_COLOUR = sum(_BITS[colour] for colour in "WUBRG")
# A unit a ritual made that may be spent only on some kinds of spell, and is lost once that spell is paid.
_RESTRICTED = 64
_MASKS = 128
_POP = tuple((mask & _COLOUR_BITS).bit_count() for mask in range(_MASKS))
# The order units are paid in: restricted ones first, as they are lost after the spell they were made for,
# then colourless, as it pays only generic mana, then by how many colours.
_PAY_KEY = tuple(
    -1 if mask & _RESTRICTED else 0 if mask == _BITS["C"] else _POP[mask] for mask in range(_MASKS)
).__getitem__
_KIND_BITS = {kind: 1 << index for index, kind in enumerate(CardKind)}


def _kind_bits(kinds: Iterable[CardKind]) -> int:
    bits = 0
    for kind in kinds:
        bits |= _KIND_BITS[kind]
    return bits


_BASICS = frozenset({"plains", "island", "swamp", "mountain", "forest", "wastes"})

# Ranks order what the policy does first: lower goes first. A held card is never cast by the policy.
_HELD = 10**6
_MANA_RANK = 100
_COMMANDER_RANK = 200
_TUTOR_RANK = 300
_DRAW_RANK = 400
_COMBO_RANK = 500
_OTHER_RANK = 600
_RANK_SPAN = 99
# Bytes of randomness a shuffle takes per card: one 64-bit number for each step of the Fisher-Yates.
_STEP_BYTES = 8


def turns_shown(bracket: int | None) -> tuple[int, ...]:
    """The turns the figures are shown for: 1 to 5 in bracket 5, else two either side of the bracket's turn;
    a deck with no bracket is shown as bracket 3."""
    if bracket == CEDH_BRACKET:
        return tuple(range(1, _CEDH_TURNS + 1))
    key = _DEFAULT_BRACKET if bracket is None else bracket
    if key not in BRACKET_TURN:
        raise ValueError(f"bracket must be 1-5 or None, got {bracket}")
    centre = BRACKET_TURN[key]
    return tuple(range(centre - _TURN_SPREAD, centre + _TURN_SPREAD + 1))


def deck_slots(deck: GoldfishDeck) -> tuple[GoldfishCard, ...]:
    """The library cards one slot per copy, in the order of `deck.cards`, commanders left out. A swap changes
    the card in the slots of the card it cuts and nothing else, so a slot keeps its place in every shuffle."""
    return tuple(card for card in deck.cards for _ in range(card.copies))


def game_rng(seed: int, index: int) -> random.Random:
    return random.Random(seed * 1_000_003 + index)  # noqa: S311 — shuffles for a simulation, not a secret


def _shuffle_front(order: list[int], numbers: memoryview, start: int, stop: int) -> None:
    """Carry out steps `start` up to `stop` of the Fisher-Yates from the front that `numbers` (one 64-bit
    number a card) fixes: step i swaps `order[i]` with one of the cards from i on."""
    size = len(numbers)
    for i in range(start, min(stop, size)):
        j = i + numbers[i] % (size - i)
        order[i], order[j] = order[j], order[i]


# --- results ----------------------------------------------------------------------------------------------


class GameOutcome(NamedTuple):
    """What one game left behind, as plain numbers; turn 0 means "not within the turns played"."""

    # Cards in the kept opening hand: 7, 6 or 5.
    hand_size: int
    # Per commander, in the order given: the turn it was cast.
    commander_turns: tuple[int, ...]
    # Mana the permanents could make, on turn 1, 2, 3 ... up to the last turn played.
    mana: tuple[int, ...]
    # The first turn up to the middle one shown that ended with mana unspent while a spell in hand had the
    # mana for it but not the colours.
    colour_screw_turn: int
    # Per combo, in the order given: the turn it landed.
    combo_turns: tuple[int, ...]
    # Bit k is set when a tutor fetched a piece of combo k before it landed.
    combo_fetched: int
    # Per finisher, in `GoldfishReport` order: the first turn it could be cast.
    finisher_turns: tuple[int, ...]
    # Per shuffle: how far into it the game reached. A card of the last shuffle further down was neither drawn
    # nor, as the library lost cards to searches, moved up to be drawn.
    shuffle_depths: tuple[int, ...]
    # Whether a land search or a tutor resolved, which takes cards out of the library of the last shuffle.
    searched: bool
    # The numbers of the cards (in order of first appearance in the slots) that a hand held or the draws
    # reached: the cards of every opening hand and of the last shuffle down to its depth.
    reached: frozenset[int]
    # Whether a land search ran, which looks at every land left in the library.
    land_search: bool
    # Whether a tutor looked in the library for a combo piece or finisher, fetching one or not.
    tutor_look: bool


@dataclass(slots=True, frozen=True)
class CommanderFigures:
    name: str
    # Per turn shown: the share of games with it cast by then.
    cast_by: tuple[float, ...]
    # None when fewer than half of the games cast it within the turns played.
    median_turn: int | None


@dataclass(slots=True, frozen=True)
class ComboFigures:
    id: str
    pieces: tuple[str, ...]
    landed_by: tuple[float, ...]
    # Among the games where it landed, the share where a tutor fetched one of its pieces.
    fetched_share: float


@dataclass(slots=True, frozen=True)
class FinisherFigures:
    name: str
    castable_by: tuple[float, ...]


@dataclass(slots=True, frozen=True)
class WaysToWin:
    combos: tuple[ComboFigures, ...]
    finishers: tuple[FinisherFigures, ...]
    # Ids of the combos landing by the bracket's turn in WARN_SHARE of games or more.
    faster_than_bracket: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class GoldfishReport:
    games: int
    # The bracket asked for, and the bracket whose keep rule ran (3 for a deck with none).
    bracket: int | None
    keep_bracket: int
    turns: tuple[int, ...]
    kept_on_seven: float
    kept_on_six: float
    kept_on_five_or_fewer: float
    commanders: tuple[CommanderFigures, ...]
    # Per turn shown: the median and the worst tenth of the mana the permanents could make.
    mana_median: tuple[int, ...]
    mana_worst_tenth: tuple[int, ...]
    colour_screw: float
    colour_screw_turn: int
    # None for a deck with no combo and no finisher.
    ways_to_win: WaysToWin | None
    not_modelled: tuple[str, ...]
    outcomes: tuple[GameOutcome, ...]


# --- costs and paying -------------------------------------------------------------------------------------


def unit_mask(colours: Iterable[str]) -> int:
    mask = 0
    for colour in colours:
        mask |= _BITS.get(colour, 0)
    return mask


def symbol_mask(symbol: str) -> int:
    """The units that pay a pip: a colour ("W"), or either colour of a hybrid pip ("W/U")."""
    return unit_mask(symbol.split("/"))


def _hall(needs: Mapping[int, int]) -> tuple[tuple[int, int], ...]:
    """Hall's condition for paying pips from units that each pay some colours: for every set of pip kinds,
    the units able to pay any of them must number as many as those pips. A pip kind is the mask of the units
    that pay it, one colour or, for a hybrid pip, two."""
    terms: list[tuple[int, int]] = []
    for size in range(1, len(needs) + 1):
        for masks in combinations(sorted(needs), size):
            union = 0
            for mask in masks:
                union |= mask
            terms.append((union, sum(needs[m] for m in masks)))
    return tuple(terms)


class Cost:
    """A cost in the form the payment checks read: `needs` counts the pips that each mask of units can pay."""

    __slots__ = ("generic", "hall", "pip_masks", "single", "total")

    def __init__(self, total: int, needs: Mapping[int, int]) -> None:
        pips = sum(needs.values())
        self.total = max(total, pips)
        self.generic = self.total - pips
        self.hall = _hall(needs)
        self.pip_masks = tuple(mask for mask in sorted(needs) for _ in range(needs[mask]))
        self.single = next(iter(needs)) if len(needs) == 1 else 0


def cost_of(total: int, pips: Mapping[str, int]) -> Cost:
    """A cost of mana value `total` with the pips of a card (`GoldfishCard.pips`): colours, colourless and
    two-colour hybrid symbols; the rest of the total is generic."""
    needs: dict[int, int] = {}
    for symbol, count in pips.items():
        mask = symbol_mask(symbol)
        if mask and count > 0:
            needs[mask] = needs.get(mask, 0) + count
    return Cost(total, needs)


def can_pay(units: Sequence[int], cost: Cost) -> bool:
    if len(units) < cost.total:
        return False
    for mask, need in cost.hall:
        found = 0
        for unit in units:
            if unit & mask:
                found += 1
                if found >= need:
                    break
        else:
            return False
    return True


def _match(units: Sequence[int], masks: Sequence[int]) -> list[int] | None:
    """Indexes of units paying each pip, found by trying the scarcest pip first and backing out."""
    scarcity = {mask: sum(1 for unit in units if unit & mask) for mask in set(masks)}
    order = sorted(masks, key=scarcity.__getitem__)
    used: list[int] = []

    def place(position: int) -> bool:
        if position == len(order):
            return True
        mask = order[position]
        tried: set[int] = set()
        for index, unit in enumerate(units):
            if not unit & mask or unit in tried or index in used:
                continue
            tried.add(unit)
            used.append(index)
            if place(position + 1):
                return True
            used.pop()
        return False

    return used if place(0) else None


def sort_units(units: list[int]) -> None:
    """Order mana units as `pay` expects them: colourless first, then the ones paying the fewest colours."""
    units.sort(key=_PAY_KEY)


def pay(units: list[int], cost: Cost) -> None:
    """Take the cost out of units, which are in `sort_units` order: coloured pips use the least flexible unit
    that can pay them, and generic mana the least flexible of what is left."""
    if cost.single:
        mask = cost.single
        for _ in cost.pip_masks:
            for index, unit in enumerate(units):
                if unit & mask:
                    del units[index]
                    break
    elif cost.pip_masks:
        chosen = _match(units, cost.pip_masks)
        if chosen is None:
            raise AssertionError("a checked cost could not be paid")
        for index in sorted(chosen, reverse=True):
            del units[index]
    if cost.generic:
        del units[: cost.generic]


# --- cards as the game loop reads them --------------------------------------------------------------------


class _Card:
    """One card as plain ints and flags; `src` is the record for what is read rarely."""

    __slots__ = (
        "artifact",
        "aura",
        "basic",
        "burst_types",
        "colours",
        "combo",
        "cost",
        "creature",
        "delayed",
        "discards",
        "draw_count",
        "engine",
        "entry",
        "extra_drops",
        "fast",
        "fin_cost",
        "finisher",
        "gain",
        "hybrids",
        "is_land",
        "keep_syn",
        "key",
        "kind_bits",
        "land_amount",
        "land_lost",
        "land_mask",
        "land_tapped",
        "land_types",
        "life_cost",
        "mana_amount",
        "mana_mask",
        "mana_perm",
        "mdfc",
        "mv",
        "need",
        "needs_sac",
        "permanent",
        "pip_mask",
        "plus_sacrificed",
        "purpose",
        "rank",
        "reach_for",
        "ritual",
        "source",
        "spend_bits",
        "src",
        "win_piece",
    )

    def __init__(self, card: GoldfishCard) -> None:
        self.src = card
        self.key = card_key(card.name)
        self.mv = card.mana_value
        mana = card.mana
        spend_from_hand = mana is not None and mana.from_hand
        self.ritual = mana is not None and (mana.ritual or mana.from_hand)
        # A permanent making mana of some kind, and the kinds that are not a fixed part of each turn's mana:
        # mana that needs other permanents, mana paid for with life, a burst for a land sacrificed.
        self.source = mana is not None and not self.ritual
        self.need = mana.needs if mana is not None else None
        self.life_cost = mana.life if mana is not None else 0
        self.burst_types = mana.land_sacrificed if mana is not None else frozenset[str]()
        self.mana_perm = self.source and not (self.need or self.life_cost or self.burst_types)
        self.artifact = CardKind.ARTIFACT in card.kinds and card.permanent
        self.mana_amount = mana.amount if mana is not None else 0
        self.mana_mask = unit_mask(mana.colours) if mana is not None else 0
        # The kinds of spell a ritual's mana may pay for, 0 for any; its units are then marked restricted.
        self.spend_bits = _kind_bits(mana.spend_on) if mana is not None else 0
        if self.spend_bits:
            self.mana_mask |= _RESTRICTED
        self.kind_bits = _kind_bits(card.kinds)
        self.delayed = mana is not None and mana.delayed
        self.aura = mana is not None and mana.on_land
        # Mana a ritual adds, net of the mana its own cost takes.
        self.gain = 0 if mana is None else mana.amount - (0 if spend_from_hand else card.mana_value)
        activation = card.land_ramp.activation if card.land_ramp is not None else 0
        # A spirit guide is exiled, not cast: its pips are not paid.
        self.cost = cost_of(0, {}) if spend_from_hand else cost_of(card.mana_value + activation, card.pips)
        fin = card.mana_value + FINISHER_X if card.x_cost else card.mana_value
        self.fin_cost = cost_of(fin, card.pips)
        paid = [symbol for symbol, count in card.pips.items() if count > 0]
        self.pip_mask = unit_mask(symbol for symbol in paid if "/" not in symbol)
        self.hybrids = tuple(symbol_mask(symbol) for symbol in paid if "/" in symbol)
        self.colours = frozenset(letter for symbol in paid for letter in symbol if letter in _CARD_COLOURS)
        self._read_land(card)
        self.creature = CardKind.CREATURE in card.kinds
        self.permanent = card.permanent
        self.extra_drops = card.extra_land_drops
        self.needs_sac = card.needs_sacrifice
        self.entry = card.entry_cost
        self.draw_count = card.draw.count if card.draw is not None else 0
        self.engine = card.draw is not None and card.draw.engine
        self.combo = card.combo
        self.finisher = card.finisher
        self.keep_syn = card.synergy or card.draw is not None
        # Fast mana for the keep rule: a cheap mana source other than a land.
        self.fast = mana is not None and not spend_from_hand and card.mana_value <= (_HAND - 5)
        self.win_piece = card.combo or card.tutor is not None or card.finisher
        self.discards = mana is not None and mana.discards_hand
        self.plus_sacrificed = mana is not None and mana.plus_sacrificed
        # What one-shot mana may pay for it: a mana source or a combo piece takes rituals; a tutor takes them
        # only for a combo piece, which `reach_for` allows for before its target is known.
        self.purpose = _FOR_PLAY if self.source or card.combo else _FOR_NOTHING
        self.reach_for = _FOR_PLAY if card.tutor is not None else self.purpose
        self.rank = self._rank()

    def _read_land(self, card: GoldfishCard) -> None:
        self.is_land = card.land is not None and CardKind.LAND in card.kinds
        self.mdfc = card.land is not None and not self.is_land
        land = card.land
        self.land_mask = unit_mask(land.colours) if land is not None else 0
        self.land_amount = land.amount if land is not None else 0
        self.land_tapped = land is not None and land.tapped
        self.land_lost = land is not None and land.lost_to_land_drop
        self.land_types = card.land_types
        name = card.name.casefold()
        self.basic = name in _BASICS or name.startswith("snow-covered ")

    def _rank(self) -> int:
        card = self.src
        if self.is_land or card.finisher or self.ritual or (card.combo and not card.permanent):
            return _HELD
        cost = min(self.cost.total, _RANK_SPAN)
        if self.source or card.land_ramp is not None or card.extra_land_drops:
            return _MANA_RANK + cost
        if card.tutor is not None:
            return _TUTOR_RANK + cost
        if card.draw is not None:
            return _DRAW_RANK + cost
        if card.combo:
            return _COMBO_RANK + cost
        return _OTHER_RANK + _RANK_SPAN - min(card.mana_value, _RANK_SPAN)


class _Setup:
    """Everything about a run that does not change from game to game."""

    __slots__ = (
        "attacks",
        "average",
        "cards",
        "cmd_ids",
        "cmd_set",
        "combo_free",
        "combo_ids",
        "combo_pieces",
        "deaths",
        "fin_ids",
        "has_needs",
        "has_rituals",
        "horizon",
        "ids",
        "keep_bracket",
        "max_lands",
        "merged",
        "mid",
        "min_lands",
        "min_sources",
        "need_syn",
        "need_win",
        "shown",
        "tap_pieces",
    )

    def __init__(
        self,
        slots: Sequence[GoldfishCard],
        commanders: Sequence[GoldfishCard],
        combos: Sequence[GoldfishCombo],
        bracket: int | None,
        average_mana_value: float,
        *,
        late: Sequence[GoldfishCard] = (),
    ) -> None:
        """`late` cards are numbered after every other card, the commanders included, so a card put into
        some slots of another leaves the numbers of all the cards already there as they were."""
        self.shown = turns_shown(bracket)
        self.horizon = self.shown[-1]
        self.mid = self.shown[len(self.shown) // 2]
        self.keep_bracket = _DEFAULT_BRACKET if bracket is None else bracket
        self.average = average_mana_value
        self.cards: list[_Card] = []
        by_object: dict[int, int] = {}
        by_key: dict[str, int] = {}

        def add(card: GoldfishCard) -> int:
            cid = by_object.get(id(card))
            if cid is None:
                cid = by_object[id(card)] = len(self.cards)
                self.cards.append(_Card(card))
                by_key.setdefault(self.cards[cid].key, cid)
            return cid

        deferred = {id(card) for card in late}
        for card in (*(card for card in slots if id(card) not in deferred), *commanders, *late):
            add(card)
        self.ids = [add(card) for card in slots]
        self.cmd_ids = tuple(add(card) for card in commanders)
        self.cmd_set = frozenset(self.cmd_ids)
        self.combo_ids = tuple(combo.id for combo in combos)
        self.combo_pieces = tuple(_piece_ids(combo, by_key) for combo in combos)
        self.combo_free = tuple(
            frozenset(by_key[card_key(name)] for name in combo.command_zone if card_key(name) in by_key)
            for combo in combos
        )
        self.fin_ids = tuple(cid for cid, card in enumerate(self.cards) if card.finisher)
        self.tap_pieces = frozenset(cid for cid, card in enumerate(self.cards) if card.src.tap_ability)
        # Creatures that search as they die, and a free outlet to sacrifice them to.
        self.deaths = any(c.src.dies_tutor for c in self.cards) and any(c.src.free_outlet for c in self.cards)
        # Attacks make Treasures: a creature's own trigger, or a permanent's for a creature becoming tapped.
        self.attacks = any(c.src.treasures_on_attack or c.src.treasure_on_tap for c in self.cards)
        # One-shot mana beyond the turn's: rituals in hand, life payments, bursts for a land, Treasures.
        self.has_rituals = self.attacks or any(
            card.ritual or card.life_cost or card.burst_types or card.src.treasures_on_entry
            for card in self.cards
        )
        self.has_needs = any(card.need for card in self.cards)
        self.merged: dict[tuple[int, ...], Cost] = {}
        self._set_keep_rule()

    def _set_keep_rule(self) -> None:
        rule = self.keep_bracket
        library = [self.cards[cid] for cid in self.ids]
        if rule >= _STRICT_KEEP:
            self.min_lands, self.max_lands = 1, 10**6
            self.min_sources = 2 if rule == CEDH_BRACKET else 3
        elif rule == _DEFAULT_BRACKET:
            self.min_lands = 3 if self.average > _BIG_CURVE else 2
            self.max_lands, self.min_sources = _MAX_KEPT_LANDS, 0
        else:
            self.min_lands, self.max_lands, self.min_sources = 3, _MAX_KEPT_LANDS, 0
        # A deck with no combo and no finisher has no way to win to ask for, so it is kept by the lower
        # brackets' clause; that clause is dropped for a deck with no card that could meet it.
        has_way = any(c.combo or c.finisher for c in library)
        self.need_win = rule >= _STRICT_KEEP and has_way
        wants_syn = rule < _STRICT_KEEP or not has_way
        self.need_syn = wants_syn and any(c.keep_syn and not c.is_land for c in library)

    def keeps(self, hand: Sequence[int]) -> bool:
        lands = fast = win = syn = 0
        for cid in hand:
            card = self.cards[cid]
            if card.is_land:
                lands += 1
            else:
                fast += card.fast
                win += card.win_piece
                syn += card.keep_syn
        if not self.min_lands <= lands <= self.max_lands or lands + fast < self.min_sources:
            return False
        return (win > 0 or not self.need_win) and (syn > 0 or not self.need_syn)

    def merged_cost(self, cids: tuple[int, ...]) -> Cost:
        found = self.merged.get(cids)
        if found is None:
            total = 0
            needs: dict[int, int] = {}
            for cid in cids:
                cost = self.cards[cid].cost
                total += cost.total
                for mask in cost.pip_masks:
                    needs[mask] = needs.get(mask, 0) + 1
            found = self.merged[cids] = Cost(total, needs)
        return found


def _piece_ids(combo: GoldfishCombo, by_key: Mapping[str, int]) -> tuple[int, ...] | None:
    """The cards of a combo, or None when one is not among the deck's cards."""
    found = [by_key.get(card_key(name)) for name in combo.pieces]
    ids = tuple(cid for cid in found if cid is not None)
    return ids if len(ids) == len(found) and ids else None


def _split_bottom(cards: Sequence[_Card], hand: Sequence[int], count: int) -> tuple[list[int], list[int]]:
    """The hand kept and the cards put on the bottom, in the order put: lands past four first, then the
    dearest spell that is not a combo piece, a tutor or a finisher, then the dearest of those, then a land."""
    kept = list(hand)
    bottomed: list[int] = []
    for _ in range(count):
        if not kept:
            break
        lands = [cid for cid in kept if cards[cid].is_land]
        spells = [cid for cid in kept if not cards[cid].is_land]
        free = [cid for cid in spells if not cards[cid].win_piece]
        if len(lands) > _MAX_KEPT_LANDS or not spells:
            pick = min(lands, key=lambda cid: (_POP[cards[cid].land_mask], cards[cid].land_tapped is False))
        else:
            pick = max(free or spells, key=lambda cid: cards[cid].mv)
        kept.remove(pick)
        bottomed.append(pick)
    return kept, bottomed


def london_bottom(hand: Sequence[GoldfishCard], count: int) -> list[GoldfishCard]:
    """The cards of the hand put on the bottom for `count` mulligans past the free one, in the order put."""
    cards = [_Card(card) for card in hand]
    _, bottomed = _split_bottom(cards, range(len(hand)), count)
    return [hand[index] for index in bottomed]


# --- one game ---------------------------------------------------------------------------------------------

type _Pick = tuple[int, _Card, tuple[int, ...], tuple[int, int] | None, int, bool]


class _Game:
    """The state of one game. Mana units are masks of the colours each can pay, sorted by how many that is."""

    __slots__ = (
        "activators",
        "arrived",
        "artifacts",
        "bf",
        "burst_used",
        "bursts",
        "check_land",
        "cmd_left",
        "cmd_turn",
        "combo_fetched",
        "combo_turn",
        "creatures",
        "dealt",
        "drops_left",
        "engines",
        "extra_drops",
        "fetched",
        "fin_turn",
        "hand",
        "hand_size",
        "land_search",
        "lands_in_play",
        "lib",
        "life",
        "life_sources",
        "lost_lands",
        "loyalty_used",
        "mana",
        "needing",
        "needs_met",
        "open_combos",
        "open_fins",
        "pending",
        "pool",
        "pops",
        "rand",
        "reached",
        "rng",
        "s",
        "screw",
        "searched",
        "settled",
        "shuffles",
        "sick",
        "top",
        "treasures",
        "turn",
        "tutor_look",
        "units",
    )

    def __init__(self, setup: _Setup, rng: random.Random) -> None:
        self.s = setup
        self.rng = rng
        self.lib: list[int] = []
        # The random numbers of the library's shuffle, one per card, and how many at its front are final.
        self.rand = memoryview(b"").cast("Q")
        self.settled = 0
        self.top = 0
        self.hand: list[int] = []
        self.hand_size = _HAND
        self.shuffles = 0
        self.bf: set[int] = set()
        self.pool: list[int] = []
        self.units: list[int] = []
        self.pending: list[int] = []
        # Mana creatures cast this turn, whose mana sits in `pending` until the next turn.
        self.sick: set[int] = set()
        self.creatures: list[tuple[int, int, int]] = []
        # Permanents that came this turn: a creature among them is summoning sick.
        self.arrived: set[int] = set()
        self.artifacts = 0
        # Mana sources needing other permanents in the order they came, and those whose mana is in the pool.
        self.needing: list[int] = []
        self.needs_met: set[int] = set()
        self.life = _START_LIFE
        self.life_sources: list[int] = []
        # Creatures making a burst of mana for a land sacrificed, and those used this turn.
        self.bursts: list[int] = []
        self.burst_used: set[int] = set()
        self.treasures = 0
        # Permanents in play with a search paid for by Treasures or loyalty, and those whose loyalty is spent.
        self.activators: list[int] = []
        self.loyalty_used: set[int] = set()
        self.engines = 0
        self.extra_drops = 0
        self.drops_left = 0
        self.lands_in_play = 0
        # Lands in play that are sacrificed when another land is played (City of Traitors).
        self.lost_lands: list[int] = []
        self.check_land = False
        self.cmd_left = list(setup.cmd_ids)
        self.cmd_turn = dict.fromkeys(setup.cmd_ids, 0)
        self.fetched: set[int] = set()
        self.searched = False
        self.pops = 0
        # The library as dealt, once a search has settled it and started taking cards out.
        self.dealt: list[int] | None = None
        self.reached: set[int] = set()
        self.land_search = False
        self.tutor_look = False
        self.screw = 0
        self.turn = 0
        self.mana: list[int] = []
        self.open_combos = [k for k, pieces in enumerate(setup.combo_pieces) if pieces is not None]
        self.combo_turn = [0] * len(setup.combo_pieces)
        self.combo_fetched = 0
        self.open_fins = list(range(len(setup.fin_ids)))
        self.fin_turn = [0] * len(setup.fin_ids)

    # -- the game -----------------------------------------------------------------------------------------

    def play(self) -> GameOutcome:
        s = self.s
        self._opening()
        for turn in range(1, s.horizon + 1):
            self.turn = turn
            self._take_turn()
        depth = self.top + self.pops
        if self.dealt is None:
            self._settle(depth)
            self.reached.update(self.lib[: min(depth, len(self.rand))])
        else:
            self.reached.update(self.dealt[:depth])
        return GameOutcome(
            hand_size=self.hand_size,
            commander_turns=tuple(self.cmd_turn[cid] for cid in s.cmd_ids),
            mana=tuple(self.mana),
            colour_screw_turn=self.screw,
            combo_turns=tuple(self.combo_turn),
            combo_fetched=self.combo_fetched,
            finisher_turns=tuple(self.fin_turn),
            shuffle_depths=(*([_HAND] * (self.shuffles - 1)), depth),
            searched=self.searched,
            reached=frozenset(self.reached),
            land_search=self.land_search,
            tutor_look=self.tutor_look,
        )

    def _opening(self) -> None:
        """Deal, and mulligan until the keep rule is met: the first mulligan is free, the later ones draw
        seven and put one card on the bottom each past the free one, and a hand of five is kept as it is."""
        s = self.s
        while True:
            lib = list(s.ids)
            # Every shuffle takes the same amount from the stream, however far the game reads into it.
            self.rand = memoryview(self.rng.randbytes(_STEP_BYTES * len(lib))).cast("Q")
            self.settled = 0
            self.lib = lib
            self.shuffles += 1
            self._settle(_HAND)
            bottoms = max(self.shuffles - 1 - _FREE_MULLIGANS, 0)
            hand = lib[:_HAND]
            self.reached.update(hand)
            kept, bottomed = _split_bottom(s.cards, hand, bottoms)
            if bottoms >= 2 or s.keeps(kept):  # noqa: PLR2004 — five cards, kept whatever they hold
                break
        self.lib.extend(bottomed)
        self.top = len(hand)
        self.hand = kept
        self.hand_size = _HAND - bottoms

    def _take_turn(self) -> None:
        s = self.s
        pool = self.pool
        if self.pending:
            pool.extend(self.pending)
            self.pending.clear()
            pool.sort(key=_PAY_KEY)
        self.sick.clear()
        self.arrived.clear()
        self.burst_used.clear()
        self._draw(1 + self.engines)
        self.drops_left = 1 + self.extra_drops
        self.units = list(pool)
        self._play_lands()
        # Searches paid for by Treasures or loyalty are used as each main phase begins, the second after the
        # attack.
        if self.activators:
            self._activate()
        self._act()
        attacked = s.attacks and self._attack()
        if (self.activators and self._activate()) or attacked:
            self._act()
        if self.turn <= s.mid and not self.screw:
            self._check_screw()
        self.mana.append(len(pool))

    def _settle(self, count: int) -> None:
        """Shuffle the library down to its first `count` cards, which are then final: a Fisher-Yates from the
        front, one step per card, so a game pays for the cards it reads and no more."""
        _shuffle_front(self.lib, self.rand, self.settled, count)
        self.settled = max(self.settled, min(count, len(self.rand)))

    def _open_library(self) -> None:
        """Settle the whole library before a search reads or takes from it, and keep the order dealt."""
        if self.dealt is None:
            self._settle(len(self.rand))
            self.dealt = self.lib[: len(self.rand)]

    def _draw(self, count: int) -> None:
        lib = self.lib
        end = self.top + count
        if end > self.settled:
            self._settle(end)
        left = len(lib) - self.top
        self.hand.extend(lib[self.top : end])
        self.top += min(count, left)
        if count <= left:
            self.check_land = True

    # -- lands --------------------------------------------------------------------------------------------

    def _play_lands(self) -> None:
        self.check_land = False
        cards = self.s.cards
        while self.drops_left > 0:
            cid = self._choose_land()
            if cid is None:
                return
            self.hand.remove(cid)
            self.drops_left -= 1
            if self.lost_lands:
                self._lose_to_land_drop()
            self._put_land(cid, cards[cid], tapped=cards[cid].land_tapped)
            if cards[cid].land_lost:
                self.lost_lands.append(cid)

    def _lose_to_land_drop(self) -> None:
        """Sacrifice the lands lost when another is played; the mana they made this turn stays."""
        cards = self.s.cards
        for cid in self.lost_lands:
            card = cards[cid]
            self.bf.discard(cid)
            self.lands_in_play -= 1
            held = self.pending if cid in self.arrived and card.land_tapped else self.pool
            self._remove_mana(held, card.land_mask, card.land_amount)
        self.lost_lands.clear()

    def _put_land(self, cid: int, card: _Card, *, tapped: bool) -> None:
        self.lands_in_play += 1
        self.bf.add(cid)
        self.arrived.add(cid)
        if tapped:
            self.pending.extend([card.land_mask] * card.land_amount)
        else:
            self._add_now(card.land_mask, card.land_amount)
        if self.s.deaths and card.src.free_outlet:
            self._offer_deaths()

    def _choose_land(self) -> int | None:
        """An untapped land that pays a colour a spell in hand lacks, else a tapped one if the turn's spending
        does not need the mana, else the untapped land with the most colours; a modal card with a land face
        only when no other land is in hand."""
        cards = self.s.cards
        lands = {cid for cid in self.hand if cards[cid].is_land}
        if not lands:
            return next((cid for cid in self.hand if cards[cid].mdfc), None)
        if len(lands) == 1:
            return next(iter(lands))
        want, lack, extra = self._colour_wants()
        untapped = [cid for cid in lands if not cards[cid].land_tapped]
        tapped = [cid for cid in lands if cards[cid].land_tapped]

        def score(cid: int) -> tuple[int, int, int]:
            mask = cards[cid].land_mask
            return _POP[mask & lack], _POP[mask & want], _POP[mask] * cards[cid].land_amount

        best_untapped = max(untapped, key=score, default=None)
        if best_untapped is not None and _POP[cards[best_untapped].land_mask & lack]:
            return best_untapped
        if tapped and (best_untapped is None or not extra):
            return max(tapped, key=score)
        return best_untapped

    def _colour_wants(self) -> tuple[int, int, bool]:
        """The colours the spells in hand and the commanders ask for, those the mana lacks, and whether a
        spell would be cast only with one more mana."""
        cards = self.s.cards
        have = len(self.pool)
        cover = 0
        for unit in self.pool:
            cover |= unit
        want = 0
        unmet = 0
        extra = False
        for cid in (*self.hand, *self.cmd_left):
            card = cards[cid]
            if card.is_land or (card.rank >= _HELD and cid not in self.s.cmd_set):
                continue
            if card.cost.total <= have + 2:
                want |= card.pip_mask
                unmet |= card.pip_mask & ~cover
                for hybrid in card.hybrids:
                    want |= hybrid
                    # A hybrid pip is met by any one of its colours.
                    if not hybrid & cover:
                        unmet |= hybrid
            extra = extra or have < card.cost.total <= have + 1
        return want, unmet, extra

    def _find_land(self, *, basic_only: bool, types: frozenset[str]) -> int:
        """The index in the library of the land to fetch, or -1: the one best for the colours wanted, among
        the lands with one of the basic land types (any land when none are named)."""
        cards = self.s.cards
        self._open_library()
        first: dict[int, int] = {}
        for index in range(self.top, len(self.lib)):
            cid = self.lib[index]
            card = cards[cid]
            if (
                cid not in first
                and card.is_land
                and (card.basic or not basic_only)
                and (not types or not types.isdisjoint(card.land_types))
            ):
                first[cid] = index
        if not first:
            return -1
        want, lack, _ = self._colour_wants()

        def score(cid: int) -> tuple[int, int, int, bool]:
            card = cards[cid]
            return (
                _POP[card.land_mask & lack],
                _POP[card.land_mask & want],
                _POP[card.land_mask],
                card.land_tapped is False,
            )

        return first[max(first, key=score)]

    def _check_screw(self) -> None:
        """Colour screw: mana is left unspent at the end of the turn while a spell the policy would cast, or
        the uncast commander, costs no more than the turn's mana but cannot be paid for its colours."""
        if not self.units:
            return
        cards = self.s.cards
        for cid in (*self.hand, *self.cmd_left):
            card = cards[cid]
            cost = card.cost
            if (card.rank >= _HELD and cid not in self.cmd_left) or not cost.hall:
                continue
            if cost.total <= len(self.pool) and not can_pay(self.pool, cost):
                self.screw = self.turn
                return

    # -- mana ---------------------------------------------------------------------------------------------

    def _add_now(self, mask: int, count: int) -> None:
        added = [mask] * count
        self.pool.extend(added)
        self.pool.sort(key=_PAY_KEY)
        self.units.extend(added)
        self.units.sort(key=_PAY_KEY)

    def _remove_creature_mana(self, cid: int, mask: int, count: int) -> None:
        """Take a sacrificed mana creature's mana away from where it sits: next turn's mana while it is
        summoning sick, else the pool. Mana already in the turn's units can still be spent: the creature was
        tapped for it before it was sacrificed."""
        self._remove_mana(self.pending if cid in self.sick else self.pool, mask, count)

    @staticmethod
    def _remove_mana(source: list[int], mask: int, count: int) -> None:
        for _ in range(count):
            if mask in source:
                source.remove(mask)

    def _plan(
        self, units: list[int], cost: Cost, paying: Sequence[_Card], purpose: int
    ) -> tuple[int, ...] | None:
        """The one-shots to use first for a cost to be paid: none when the mana is enough, None when even they
        would not make it. `paying` holds the cards the cost is for, whose kinds a ritual's restricted mana
        must all allow. Rituals and bursts serve a purpose of `_FOR_PLAY` or more, Lion's Eye Diamond only
        `_FOR_WIN`; life is paid for anything. A plan whose rituals sacrifice a creature is tried again
        without them, as the creature may be the one paying life."""
        if can_pay(units, cost):
            return ()
        if not self.s.has_rituals:
            return None
        for sacrifices in (True, False):
            trial = list(units)
            used: list[int] = []
            gone: set[int] = set()
            if purpose >= _FOR_PLAY and self._plan_rituals(
                trial, used, gone, cost=cost, paying=paying, purpose=purpose, sacrifices=sacrifices
            ):
                return tuple(used)
            payments, mask = self._life_payments(gone)
            for _ in range(payments):
                trial.append(mask)
                trial.sort(key=_PAY_KEY)
                used.append(_LIFE)
                if can_pay(trial, cost):
                    return tuple(used)
            if not gone:
                return None
        return None

    def _plan_rituals(
        self,
        trial: list[int],
        used: list[int],
        gone: set[int],
        *,
        cost: Cost,
        paying: Sequence[_Card],
        purpose: int,
        sacrifices: bool,
    ) -> bool:
        """Add to `trial` and `used` the rituals in hand, cheapest first, then the creatures' bursts, until
        the cost can be paid; whether it can. The creatures the rituals sacrifice go into `gone`."""
        for _ in range(self._treasures_for(purpose)):
            trial.append(_ANY_COLOUR)
            trial.sort(key=_PAY_KEY)
            used.append(_TREASURE)
            if can_pay(trial, cost):
                return True
        cards = self.s.cards
        # The creatures the rituals may sacrifice, cheapest first as `_sacrifice` takes them, leaving those
        # the cards paid for sacrifice themselves.
        fodder = sorted(self.creatures)
        spare = len(fodder) - sum(card.needs_sac for card in paying)
        for cid in sorted(
            (cid for cid in self.hand if cards[cid].ritual), key=lambda cid: cards[cid].cost.total
        ):
            card = cards[cid]
            spend = card.spend_bits
            if (
                (card.discards and purpose < _FOR_WIN)
                or (card.needs_sac and (not sacrifices or len(gone) >= spare))
                or (spend and not all(c.kind_bits & spend for c in paying))
                or not can_pay(trial, card.cost)
            ):
                continue
            pay(trial, card.cost)
            amount = card.mana_amount
            if card.needs_sac:
                _, sacrificed, victim = fodder[len(gone)]
                gone.add(victim)
                amount += sacrificed if card.plus_sacrificed else 0
            trial.extend([card.mana_mask] * amount)
            trial.sort(key=_PAY_KEY)
            used.append(cid)
            if can_pay(trial, cost):
                return True
        for cid in self._bursts_ready():
            if cid in gone:
                continue
            card = cards[cid]
            trial.extend([card.mana_mask] * card.mana_amount)
            trial.sort(key=_PAY_KEY)
            used.append(_BURST - cid)
            if can_pay(trial, cost):
                return True
        return False

    def _use_ritual(self, code: int) -> None:
        """Use a one-shot of a plan: a ritual from the hand, a life payment or a creature's burst."""
        cards = self.s.cards
        if code == _LIFE:
            source = cards[min(self.life_sources, key=lambda cid: cards[cid].life_cost)]
            self.life -= source.life_cost
            self.units.append(source.mana_mask)
        elif code == _TREASURE:
            self._spend_treasures(1)
            self.units.append(_ANY_COLOUR)
        elif code <= _BURST:
            cid = _BURST - code
            self.burst_used.add(cid)
            self._sacrifice_land(cards[cid].burst_types)
            self.units.extend([cards[cid].mana_mask] * cards[cid].mana_amount)
        else:
            card = cards[code]
            self.hand.remove(code)
            pay(self.units, card.cost)
            amount = card.mana_amount
            if card.needs_sac:
                sacrificed = self._sacrifice()
                amount += sacrificed if card.plus_sacrificed else 0
            self.units.extend([card.mana_mask] * amount)
        self.units.sort(key=_PAY_KEY)

    def _treasures_for(self, purpose: int) -> int:
        """The Treasures a cost may spend: as rituals, none on a plain spell, and while a search paid for by
        Treasures is in play with something to find, none but on a win or the commander."""
        if purpose < _FOR_PLAY or not self.treasures:
            return 0
        if purpose < _FOR_WIN and (self.open_combos or self.open_fins):
            cards = self.s.cards
            for cid in self.activators:
                ability = cards[cid].src.activated_tutor
                if ability is not None and ability.treasures:
                    return 0
        return self.treasures

    def _bursts_ready(self) -> list[int]:
        """The creatures that can sacrifice a land for their mana now: in play since the turn began, not used
        this turn, each with a land of its type in play that no other of them takes."""
        if not self.bursts:
            return []
        cards = self.s.cards
        lands = [cid for cid in self.bf if cards[cid].is_land]
        ready: list[int] = []
        for cid in self.bursts:
            if cid in self.arrived or cid in self.burst_used:
                continue
            types = cards[cid].burst_types
            land = next((lid for lid in lands if not types.isdisjoint(cards[lid].land_types)), None)
            if land is not None:
                lands.remove(land)
                ready.append(cid)
        return ready

    def _life_payments(self, gone: Collection[int] = ()) -> tuple[int, int]:
        """How many life payments for mana the life allows down to the floor, and the mana each makes, from
        the cheapest source that is not among the creatures `gone`."""
        sources = [cid for cid in self.life_sources if cid not in gone]
        if not sources:
            return 0, 0
        cards = self.s.cards
        source = cards[min(sources, key=lambda cid: cards[cid].life_cost)]
        return max((self.life - _LIFE_FLOOR) // source.life_cost, 0), source.mana_mask

    def _sacrifice_land(self, types: frozenset[str]) -> None:
        """Sacrifice a land of one of the types, the one of the fewest colours: its mana this turn was made
        before, and from the next turn it is gone."""
        cards = self.s.cards
        land = min(
            (cid for cid in self.bf if cards[cid].is_land and not types.isdisjoint(cards[cid].land_types)),
            key=lambda cid: _POP[cards[cid].land_mask],
        )
        self.bf.discard(land)
        self.lands_in_play -= 1
        held = self.pending if land in self.arrived and cards[land].land_tapped else self.pool
        self._remove_mana(held, cards[land].land_mask, cards[land].land_amount)

    def _meet_needs(self) -> None:
        """Put the mana of each source that needs other permanents into the pool while they are there, and
        take it out when they are gone: each source taps a creature of its own, and metalcraft counts the
        artifacts. A crewed Vehicle that came this turn makes its mana from the next."""
        cards = self.s.cards
        creatures = len(self.creatures)
        for cid in self.needing:
            card = cards[cid]
            if card.need is ManaNeed.CREATURE:
                met = creatures > 0
                creatures -= met
            else:
                met = self.artifacts >= _METALCRAFT
            if met == (cid in self.needs_met):
                continue
            later = card.delayed and cid in self.arrived
            if met:
                self.needs_met.add(cid)
                if later:
                    self.pending.extend([card.mana_mask] * card.mana_amount)
                else:
                    self._add_now(card.mana_mask, card.mana_amount)
            else:
                self.needs_met.discard(cid)
                self._remove_mana(self.pending if later else self.pool, card.mana_mask, card.mana_amount)

    # -- what to do ---------------------------------------------------------------------------------------

    def _act(self) -> None:
        while True:
            if self.open_combos:
                self._check_combos()
            if self.open_fins:
                self._check_finishers()
            if self.check_land:
                self._play_lands()
            pick = self._pick()
            if pick is None:
                return
            self._cast(pick)

    def _pick(self) -> _Pick | None:
        """The next thing the policy does with the mana left: the card of the lowest rank that can be paid."""
        s = self.s
        cards = s.cards
        units = self.units
        reach = self._reach()
        best: _Pick | None = None
        best_rank = _HELD
        for cid in self.hand:
            card = cards[cid]
            if card.rank < best_rank and card.cost.total <= reach[card.reach_for]:
                found = self._eligible(cid, card, units, from_command=False)
                if found is not None:
                    best, best_rank = (cid, card, *found, False), card.rank
        for cid in self.cmd_left:
            card = cards[cid]
            rank = _COMMANDER_RANK + min(card.mv, _RANK_SPAN)
            if rank < best_rank and card.cost.total <= reach[_FOR_WIN]:
                found = self._eligible(cid, card, units, from_command=True)
                if found is not None:
                    best, best_rank = (cid, card, *found, True), rank
        return best

    def _reach(self) -> tuple[int, int, int]:
        """The most mana the turn could still make for each purpose, the one-shots it allows counted."""
        units = len(self.units)
        if not self.s.has_rituals:
            return units, units, units
        cards = self.s.cards
        anything = units + self._life_payments()[0]
        play = anything + sum(cards[cid].mana_amount for cid in self._bursts_ready())
        win = play + self.treasures
        play += self._treasures_for(_FOR_PLAY)
        biggest = max((mv for _, mv, _ in self.creatures), default=0)
        for cid in self.hand:
            card = cards[cid]
            if not card.ritual or (card.needs_sac and not self.creatures):
                continue
            gain = max(card.gain + (biggest if card.plus_sacrificed else 0), 0)
            win += gain
            play += 0 if card.discards else gain
        return anything, play, win

    def _eligible(
        self, cid: int, card: _Card, units: list[int], *, from_command: bool
    ) -> tuple[tuple[int, ...], tuple[int, int] | None, int] | None:
        """The plan to pay for a card, where its search goes and the card it costs from the hand, if it can be
        cast now. Rituals pay for the commander, a mana source, a combo piece or a tutor fetching one."""
        if (card.aura and not self.lands_in_play) or (card.needs_sac and not self.creatures):
            return None
        target = None
        purpose = _FOR_WIN if from_command else card.purpose
        if card.src.tutor is not None:
            target = self._target(card.src.tutor, units, card.cost.total)
            if target is None:
                return None
            if self.s.cards[target[1]].combo:
                purpose = max(purpose, _FOR_PLAY)
        entry = -1
        if card.entry is not None:
            entry = self._entry_card(cid, card)
            if entry < 0:
                return None
        plan = self._plan(units, card.cost, (card,), purpose)
        if plan is None:
            return None
        return plan, target, entry

    def _entry_card(self, cid: int, card: _Card) -> int:
        """The card in hand a permanent costs as it comes in: the dearest spell that is no combo piece or
        finisher, or a land of the fewest colours; -1 when there is none."""
        cards = self.s.cards
        others = list(self.hand)
        others.remove(cid)
        if card.entry is EntryCost.LAND_CARD:
            lands = [c for c in others if cards[c].is_land]
            return min(lands, key=lambda c: _POP[cards[c].land_mask], default=-1)
        spells = [c for c in others if not cards[c].is_land and not cards[c].combo and not cards[c].finisher]
        return max(spells, key=lambda c: cards[c].mv, default=-1)

    def _cast(self, pick: _Pick) -> None:
        cid, card, plan, target, entry, from_command = pick
        units = self.units
        for ritual in plan:
            self._use_ritual(ritual)
        pay(units, card.cost)
        if target is not None and card.src.tutor is not None and card.src.tutor.x_limited:
            del units[: self.s.cards[target[1]].mv]
        if units and units[0] & _RESTRICTED:
            units[:] = [unit for unit in units if not unit & _RESTRICTED]
        if from_command:
            self.cmd_left.remove(cid)
            self.cmd_turn[cid] = self.turn
        else:
            self.hand.remove(cid)
        if entry >= 0:
            self.hand.remove(entry)
        if any(code >= 0 and self.s.cards[code].discards for code in plan):
            self.hand.clear()
        if card.needs_sac:
            self._sacrifice()
        if card.src.land_ramp is not None:
            self._ramp_lands(card)
        if target is not None and card.src.tutor is not None:
            self._fetch(card.src.tutor, target)
        self._enter(cid, card, from_command=from_command)

    def _enter(self, cid: int, card: _Card, *, from_command: bool = False) -> None:
        """What a resolved card leaves behind: the permanent, its mana, land drops and cards drawn."""
        if card.permanent:
            self.bf.add(cid)
            self.arrived.add(cid)
        if card.creature:
            rank = 2 if from_command else (1 if card.source else 0)
            self.creatures.append((rank, card.mv, cid))
        self._track_sources(cid, card)
        if card.mana_perm:
            if card.delayed:
                self.sick.add(cid)
                self.pending.extend([card.mana_mask] * card.mana_amount)
            else:
                self._add_now(card.mana_mask, card.mana_amount)
        if card.extra_drops:
            self.extra_drops += card.extra_drops
            self.drops_left += card.extra_drops
            self.check_land = True
        if card.engine:
            self.engines += 1
        elif card.draw_count:
            self._draw(card.draw_count)
        if card.finisher and card.permanent:
            self._record_finisher(cid)
        self._arrival_triggers(card.src)

    def _arrival_triggers(self, card: GoldfishCard) -> None:
        """The Treasures a permanent makes as it comes, and the deaths a free outlet or a creature that
        searches as it dies brings on."""
        if card.treasures_on_entry:
            self._make_treasures(card.treasures_on_entry)
        if self.s.deaths and (card.free_outlet or card.dies_tutor is not None):
            self._offer_deaths()

    def _make_treasures(self, count: int) -> None:
        """Treasures wait in play as one-shot mana of any colour, and count as artifacts."""
        self.treasures += count
        self.artifacts += count
        if self.needing:
            self._meet_needs()

    def _attack(self) -> bool:
        """Attack with every creature in play since the turn began, for the Treasures that attacking and
        becoming tapped make (no damage is followed); whether any were made."""
        cards = self.s.cards
        attackers = [cards[cid].src for _, _, cid in self.creatures if cid not in self.arrived]
        made = sum(attacker.treasures_on_attack for attacker in attackers)
        for cid in self.bf:
            types = cards[cid].src.treasure_on_tap
            if types:
                made += sum(1 for attacker in attackers if _has_type(attacker, types))
        if made:
            self._make_treasures(made)
        return made > 0

    def _activate(self) -> bool:
        """Use each permanent's search paid for by Treasures or loyalty while it can pay and finds a card;
        whether any did."""
        cards = self.s.cards
        used = False
        for cid in list(self.activators):
            ability = cards[cid].src.activated_tutor
            if ability is None or cid not in self.bf:
                continue
            while self.treasures >= ability.treasures and cid not in self.loyalty_used:
                target = self._target(ability.tutor, self.units, 0)
                if target is None:
                    break
                if ability.loyalty:
                    self.loyalty_used.add(cid)
                self._spend_treasures(ability.treasures)
                self._fetch(ability.tutor, target)
                used = True
        return used

    def _spend_treasures(self, count: int) -> None:
        if not count:
            return
        self.treasures -= count
        self.artifacts -= count
        if self.needing:
            self._meet_needs()

    def _track_sources(self, cid: int, card: _Card, *, gone: bool = False) -> None:
        """Count a permanent coming or going among the artifacts and the mana sources that are no fixed part
        of each turn's mana, and settle which sources needing other permanents make mana."""
        if card.artifact:
            self.artifacts += -1 if gone else 1
        for held, kept in (
            (card.need is not None, self.needing),
            (bool(card.life_cost), self.life_sources),
            (bool(card.burst_types), self.bursts),
            (card.src.activated_tutor is not None, self.activators),
        ):
            if held:
                (kept.remove if gone else kept.append)(cid)
        if gone and cid in self.needs_met:
            self.needs_met.discard(cid)
            self._remove_creature_mana(cid, card.mana_mask, card.mana_amount)
        if self.needing:
            self._meet_needs()

    def _sacrifice(self, entry: tuple[int, int, int] | None = None) -> int:
        """Sacrifice a creature of `creatures`, by default the cheapest, one that makes no mana before a mana
        creature before a commander, and take away what it gave: its mana, its extra land drops and its draw
        engine. Its search as it dies happens. Returns its mana value. A commander is not cast again (ADR
        0054)."""
        entry = min(self.creatures) if entry is None else entry
        self.creatures.remove(entry)
        _, mv, cid = entry
        card = self.s.cards[cid]
        self.bf.discard(cid)
        self._track_sources(cid, card, gone=True)
        if card.mana_perm:
            self._remove_creature_mana(cid, card.mana_mask, card.mana_amount)
        if card.extra_drops:
            self.extra_drops -= card.extra_drops
            self.drops_left = max(self.drops_left - card.extra_drops, 0)
        if card.engine:
            self.engines -= 1
        if card.src.dies_tutor is not None:
            self._search_all(card.src.dies_tutor)
        return mv

    def _offer_deaths(self) -> None:
        """Sacrifice each creature whose death searches to a free outlet in play, while a combo or a finisher
        is still to come."""
        cards = self.s.cards
        if not (self.open_combos or self.open_fins) or not any(cards[cid].src.free_outlet for cid in self.bf):
            return
        for entry in [entry for entry in self.creatures if cards[entry[2]].src.dies_tutor is not None]:
            if entry in self.creatures:
                self._sacrifice(entry)

    def _search_all(self, tutor: Tutor) -> None:
        """A search for one card, or for any number whose mana values add up to the tutor's total."""
        if tutor.total_mana_value is None:
            target = self._target(tutor, self.units, 0)
            if target is not None:
                self._fetch(tutor, target)
            return
        self.tutor_look = True
        cards = self.s.cards
        left = tutor.total_mana_value
        chosen: list[int] = []
        for cid, _ in self._wanted():
            if cid not in chosen and cards[cid].mv <= left and _finds(cards[cid], tutor, None):
                chosen.append(cid)
                left -= cards[cid].mv
        for cid in chosen:
            self._fetch(tutor, (self.lib.index(cid, self.top), cid))

    # -- searching ----------------------------------------------------------------------------------------

    def _ramp_lands(self, card: _Card) -> None:
        ramp = card.src.land_ramp
        if ramp is None:
            return
        self.searched = self.land_search = True
        cards = self.s.cards
        for index in range(ramp.count):
            found = self._find_land(basic_only=ramp.basic_only, types=ramp.land_types)
            if found < 0:
                return
            cid = self.lib.pop(found)
            self.pops += 1
            if index >= ramp.count - ramp.to_hand:
                self.hand.append(cid)
                self.check_land = True
            else:
                tapped = ramp.destination is not LandDestination.UNTAPPED
                self._put_land(cid, cards[cid], tapped=tapped)

    def _target(self, tutor: Tutor, units: list[int], paid: int) -> tuple[int, int] | None:
        """The card a tutor would fetch and its place in the library: a piece of the combo nearest to landing
        that the tutor can find, else a finisher. `paid` is the mana the tutor itself takes from the units.
        A tutor to the hand or the top takes one the mana could pay for by the last turn, a land a turn,
        before one it could not."""
        self.tutor_look = True
        limit = tutor.max_mana_value
        if tutor.x_limited:
            limit = len(units) - paid
        elif tutor.value_over_sacrifice is not None:
            # With no creature to sacrifice nothing fits.
            limit = tutor.value_over_sacrifice + min(self.creatures)[1] if self.creatures else -1
        cards = self.s.cards
        anywhere = tutor.destination is TutorDestination.BATTLEFIELD
        reach = len(units) - paid + self.s.horizon - self.turn
        first = None
        for cid, index in self._wanted():
            found = cards[cid]
            if not _finds(found, tutor, limit):
                continue
            if anywhere or found.mv <= reach:
                return index, cid
            first = first or (index, cid)
        return first

    def _wanted(self) -> list[tuple[int, int]]:
        """What a tutor is after that is in the library, best first, with its place there: the missing
        pieces of the combos with the fewest missing, dearest first, then the finishers not castable yet."""
        s = self.s
        if not self.open_combos and not s.fin_ids:
            return []
        self._open_library()
        cards = s.cards
        where: dict[int, int] = {}

        def place(cid: int) -> bool:
            if cid not in where:
                try:
                    where[cid] = self.lib.index(cid, self.top)
                except ValueError:
                    return False
            return True

        options: list[list[int]] = []
        for k in self.open_combos:
            pieces = s.combo_pieces[k] or ()
            missing = [p for p in pieces if p not in self.bf and p not in self.hand and p not in s.cmd_set]
            if missing and all(place(p) for p in missing):
                options.append(sorted(missing, key=lambda c: -cards[c].mv))
        options.sort(key=len)
        wanted = [cid for missing in options for cid in missing]
        wanted += [
            cid
            for position, cid in enumerate(s.fin_ids)
            if not self.fin_turn[position] and cid not in self.hand and cid not in self.bf and place(cid)
        ]
        return [(cid, where[cid]) for cid in wanted]

    def _fetch(self, tutor: Tutor, target: tuple[int, int]) -> None:
        index, cid = target
        self.searched = True
        self.lib.pop(index)
        self.pops += 1
        self.fetched.add(cid)
        if tutor.destination is TutorDestination.HAND:
            self.hand.append(cid)
        elif tutor.destination is TutorDestination.TOP:
            self.lib.insert(self.top, cid)
        else:
            self._enter(cid, self.s.cards[cid])

    # -- ways to win --------------------------------------------------------------------------------------

    def _check_combos(self) -> None:
        """A combo lands once every piece is out, or can be cast with the mana left from the hand or, for a
        commander, the command zone; it is then recorded and the game goes on."""
        s = self.s
        for k in list(self.open_combos):
            cast = self._pieces_to_cast(k)
            if cast is None:
                continue
            pieces = tuple(s.cards[piece] for piece in cast)
            if cast and self._plan(self.units, s.merged_cost(cast), pieces, _FOR_WIN) is None:
                continue
            self.combo_turn[k] = self.turn
            if any(piece in self.fetched for piece in s.combo_pieces[k] or ()):
                self.combo_fetched |= 1 << k
            self.open_combos.remove(k)

    def _pieces_to_cast(self, k: int) -> tuple[int, ...] | None:
        """The pieces of combo k still to be cast, or None when one is neither out nor castable. A piece whose
        ability taps it must have been out since the turn began."""
        s = self.s
        free, taps = s.combo_free[k], s.tap_pieces
        cast: list[int] = []
        for piece in s.combo_pieces[k] or ():
            if piece in self.bf:
                if piece in taps and piece in self.arrived and piece not in free:
                    return None
                continue
            if piece in free:
                continue
            if piece in taps or (piece not in self.hand and piece not in self.cmd_left):
                return None
            cast.append(piece)
        return tuple(cast)

    def _check_finishers(self) -> None:
        s = self.s
        for position in list(self.open_fins):
            cid = s.fin_ids[position]
            card = s.cards[cid]
            if (cid in self.hand or cid in self.cmd_left) and self._plan(
                self.units, card.fin_cost, (card,), _FOR_WIN
            ) is not None:
                self._record_finisher(cid)

    def _record_finisher(self, cid: int) -> None:
        position = self.s.fin_ids.index(cid)
        if not self.fin_turn[position]:
            self.fin_turn[position] = self.turn
            self.open_fins.remove(position)


def _finds(card: _Card, tutor: Tutor, limit: int | None) -> bool:
    """Whether a tutor can fetch the card: a permanent for the battlefield, of a type, subtype and colour it
    searches, within the mana value and power limits, and no land."""
    src = card.src
    return (
        (limit is None or card.mv <= limit)
        and _finds_type(src, tutor)
        and _finds_colour(card.colours, tutor.colours)
        and (tutor.max_power is None or (src.power is not None and src.power <= tutor.max_power))
        and (card.permanent or tutor.destination is not TutorDestination.BATTLEFIELD)
        and not card.is_land
    )


def _finds_colour(colours: frozenset[str], wanted: frozenset[str]) -> bool:
    """Whether a card of these colours has one of the wanted ones, "C" standing for none."""
    return not wanted or bool(colours & wanted) or ("C" in wanted and not colours)


def _has_type(card: GoldfishCard, types: frozenset[str]) -> bool:
    """Whether a creature has one of the subtypes, "creature" standing for any."""
    return "creature" in types or CHANGELING in card.subtypes or bool(card.subtypes & types)


def _finds_type(card: GoldfishCard, tutor: Tutor) -> bool:
    """Whether the card is of a type and a subtype the tutor searches, or of either when it takes either."""
    of_type = not tutor.finds or bool(card.kinds & tutor.finds)
    if not tutor.subtypes:
        return of_type
    of_subtype = CHANGELING in card.subtypes or bool(card.subtypes & tutor.subtypes)
    if tutor.either and tutor.finds:
        return bool(card.kinds & tutor.finds) or of_subtype
    return of_type and of_subtype


# --- the run ----------------------------------------------------------------------------------------------


def simulate(
    slots: Sequence[GoldfishCard],
    commanders: Sequence[GoldfishCard],
    combos: Sequence[GoldfishCombo],
    *,
    bracket: int | None,
    average_mana_value: float,
    games: int = GAMES,
    seed: int,
    not_modelled: Sequence[str] = (),
) -> GoldfishReport:
    """Play `games` games of the deck and report the figures of ADR 0054.

    slots is the library, one entry per card (`deck_slots`); a card in several slots may be one object.
    """
    if games < 1:
        raise ValueError(f"games must be at least 1, got {games}")
    setup = _Setup(slots, commanders, combos, bracket, average_mana_value)
    outcomes = tuple(_Game(setup, game_rng(seed, index)).play() for index in range(games))
    return _report(setup, bracket, outcomes, not_modelled, combos)


def goldfish(deck: GoldfishDeck, *, bracket: int | None, seed: int, games: int = GAMES) -> GoldfishReport:
    """`simulate` for a compiled deck."""
    return simulate(
        deck_slots(deck),
        deck.commanders,
        deck.combos,
        bracket=bracket,
        average_mana_value=deck.average_mana_value,
        games=games,
        seed=seed,
        not_modelled=deck.not_modelled,
    )


def replay_game(
    slots: Sequence[GoldfishCard],
    commanders: Sequence[GoldfishCard],
    combos: Sequence[GoldfishCombo],
    *,
    bracket: int | None,
    average_mana_value: float,
    seed: int,
    index: int,
) -> GameOutcome:
    """Game `index` of a run alone, with the outcome it has in `simulate`."""
    setup = _Setup(slots, commanders, combos, bracket, average_mana_value)
    return _Game(setup, game_rng(seed, index)).play()


# --- figures ----------------------------------------------------------------------------------------------


def _quantile(values: Sequence[int], numerator: int, denominator: int) -> int:
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) * numerator // denominator]


def _share_by(turns: Sequence[int], shown: Sequence[int]) -> tuple[float, ...]:
    """Per turn shown, the share of the games whose turn (0 for none) is within it."""
    return tuple(sum(1 for t in turns if 0 < t <= limit) / len(turns) for limit in shown)


def _report(
    setup: _Setup,
    bracket: int | None,
    outcomes: tuple[GameOutcome, ...],
    not_modelled: Sequence[str],
    combos: Sequence[GoldfishCombo],
) -> GoldfishReport:
    count = len(outcomes)
    shown = setup.shown
    cards = setup.cards
    sizes = [o.hand_size for o in outcomes]
    commanders = tuple(
        _commander_figures(cards[cid].src.name, [o.commander_turns[i] for o in outcomes], setup)
        for i, cid in enumerate(setup.cmd_ids)
    )
    mana = [[o.mana[turn - 1] for o in outcomes] for turn in shown]
    ways = None
    if combos or setup.fin_ids:
        ways = _ways_to_win(setup, bracket, outcomes, combos)
    return GoldfishReport(
        games=count,
        bracket=bracket,
        keep_bracket=setup.keep_bracket,
        turns=shown,
        kept_on_seven=sum(1 for n in sizes if n == _HAND) / count,
        kept_on_six=sum(1 for n in sizes if n == _HAND - 1) / count,
        kept_on_five_or_fewer=sum(1 for n in sizes if n < _HAND - 1) / count,
        commanders=commanders,
        mana_median=tuple(_quantile(values, 1, 2) for values in mana),
        mana_worst_tenth=tuple(_quantile(values, 1, 10) for values in mana),
        colour_screw=sum(1 for o in outcomes if o.colour_screw_turn) / count,
        colour_screw_turn=setup.mid,
        ways_to_win=ways,
        not_modelled=tuple(not_modelled),
        outcomes=outcomes,
    )


def _commander_figures(name: str, turns: Sequence[int], setup: _Setup) -> CommanderFigures:
    never = setup.horizon + 1
    median = _quantile([t or never for t in turns], 1, 2)
    return CommanderFigures(
        name=name,
        cast_by=_share_by(turns, setup.shown),
        median_turn=None if median == never else median,
    )


def _ways_to_win(
    setup: _Setup, bracket: int | None, outcomes: tuple[GameOutcome, ...], combos: Sequence[GoldfishCombo]
) -> WaysToWin:
    count = len(outcomes)
    limit = BRACKET_TURN.get(bracket) if bracket is not None else None
    figures: list[ComboFigures] = []
    faster: list[str] = []
    for k, combo in enumerate(combos):
        turns = [o.combo_turns[k] for o in outcomes]
        landed = [o for o in outcomes if o.combo_turns[k]]
        fetched = sum(1 for o in landed if o.combo_fetched >> k & 1)
        figures.append(
            ComboFigures(
                combo.id,
                combo.pieces,
                _share_by(turns, setup.shown),
                fetched / len(landed) if landed else 0.0,
            )
        )
        if limit is not None and sum(1 for t in turns if 0 < t <= limit) / count >= WARN_SHARE:
            faster.append(combo.id)
    finishers = tuple(
        FinisherFigures(
            setup.cards[cid].src.name, _share_by([o.finisher_turns[i] for o in outcomes], setup.shown)
        )
        for i, cid in enumerate(setup.fin_ids)
    )
    return WaysToWin(tuple(figures), finishers, tuple(faster))


# --- swaps ------------------------------------------------------------------------------------------------


class PartialSwap(NamedTuple):
    """Some copies of a card cut and the card put in their slots: the last `copies` slots the card holds."""

    cut: GoldfishCard
    incoming: GoldfishCard
    copies: int


# A card cut from the deck and the card put in every slot it held, or in some of them.
type Swap = tuple[GoldfishCard, GoldfishCard] | PartialSwap

# A share has moved when its paired difference is more than this many standard errors.
_SIGMAS = 2


class FigureKind(Enum):
    KEPT_ON_SEVEN = "kept on seven"
    KEPT_ON_SIX = "kept on six"
    KEPT_ON_FIVE_OR_FEWER = "kept on five or fewer"
    COMMANDER_CAST_BY = "commander cast by"
    COMMANDER_MEDIAN = "commander median turn"
    MANA_MEDIAN = "median mana"
    MANA_WORST_TENTH = "worst tenth of mana"
    COLOUR_SCREW = "colour screw"
    COMBO_LANDED_BY = "combo landed by"
    COMBO_FETCHED = "combo pieces fetched"
    FINISHER_CASTABLE_BY = "finisher castable by"


@dataclass(slots=True, frozen=True)
class FigureChange:
    """One figure of the report before and after a swap. A figure that only one side has (a finisher put in
    or cut) is 0 on the other."""

    kind: FigureKind
    # The commander, combo id or finisher it belongs to; empty for the deck's own figures.
    subject: str
    # The turn of a per-turn figure.
    turn: int | None
    base: float | None
    swapped: float | None
    moved: bool


@dataclass(slots=True, frozen=True)
class GoldfishComparison:
    base: GoldfishReport
    swapped: GoldfishReport
    # Every figure in the order of the report, moved or not.
    figures: tuple[FigureChange, ...]
    # The games played again, as their indices.
    replayed: tuple[int, ...]
    # Why each swap asked for that could not be played was left out of the figures.
    left_out: tuple[str, ...] = ()

    @property
    def moved(self) -> tuple[FigureChange, ...]:
        return tuple(figure for figure in self.figures if figure.moved)

    @property
    def any_moved(self) -> bool:
        return any(figure.moved for figure in self.figures)


def is_real_shift(games: int, total: int, squares: int) -> bool:
    """Whether the mean of per-game differences is beyond twice its standard error, given their sum and the
    sum of their squares over all `games` games (a game the swap cannot change differs by 0)."""
    if total == 0:
        return False
    mean = total / games
    variance = (squares - total * mean) / (games - 1) if games > 1 else 0.0
    return abs(mean) > _SIGMAS * sqrt(max(variance, 0.0) / games)


def _is_spell(card: GoldfishCard) -> bool:
    return not (card.land is not None and CardKind.LAND in card.kinds)


def _is_plain(card: GoldfishCard) -> bool:
    """A spell the goldfish has nothing to say about: no mana, land, tutor, draw, combo or finisher role."""
    return (
        card.modelled
        and card.land is None
        and card.mana is None
        and card.land_ramp is None
        and card.tutor is None
        and card.draw is None
        and not card.combo
        and not card.finisher
        and not card.synergy
        and not card.extra_land_drops
        and not card.needs_sacrifice
        and card.entry_cost is None
    )


def _plays_alike(cut: GoldfishCard, incoming: GoldfishCard) -> bool:
    """Whether two plain spells are played the same in every game: the same mana value, colours and
    creature status, which are all a plain spell brings to the game."""
    return (
        _is_plain(cut)
        and _is_plain(incoming)
        and cut.mana_value == incoming.mana_value
        and cut.pips == incoming.pips
        and (CardKind.CREATURE in cut.kinds) == (CardKind.CREATURE in incoming.kinds)
    )


class _Cut(NamedTuple):
    """One swap read against the slots: the card put in, the slots it takes, and whether the card cut keeps
    other copies in the deck."""

    incoming: GoldfishCard
    slots: tuple[int, ...]
    partial: bool


def _cuts(slots: Sequence[GoldfishCard], swaps: Sequence[Swap]) -> dict[str, _Cut]:
    """The slots and incoming card for the key of each card cut."""
    held: dict[str, list[int]] = {}
    for i, card in enumerate(slots):
        held.setdefault(card_key(card.name), []).append(i)
    cuts: dict[str, _Cut] = {}
    for swap in swaps:
        cut, incoming = swap[0], swap[1]
        key = card_key(cut.name)
        at = held.get(key)
        if not at:
            raise ValueError(f"{cut.name} is not in the deck")
        if key in cuts:
            raise ValueError(f"{cut.name} is cut twice")
        copies = swap.copies if isinstance(swap, PartialSwap) else len(at)
        if not 1 <= copies <= len(at):
            raise ValueError(f"{copies} of {cut.name} are cut and the deck holds {len(at)}")
        cuts[key] = _Cut(incoming, tuple(at[-copies:]), copies < len(at))
    return cuts


def _replacements(cuts: Mapping[str, _Cut]) -> dict[int, GoldfishCard]:
    """The card each slot that a swap changes comes to hold."""
    return {i: cut.incoming for cut in cuts.values() for i in cut.slots}


def swapped_average(slots: Sequence[GoldfishCard], swaps: Sequence[Swap], average_mana_value: float) -> float:
    """The average mana value of the nonland cards after the swaps, as `manabase.land_count` rounds it."""
    spells = sum(1 for card in slots if _is_spell(card))
    total = average_mana_value * spells
    for i, incoming in _replacements(_cuts(slots, swaps)).items():
        card = slots[i]
        if _is_spell(card):
            spells -= 1
            total -= card.mana_value
        if _is_spell(incoming):
            spells += 1
            total += incoming.mana_value
    return round(total / spells, 2) if spells else 0.0


def compare(
    base: GoldfishReport,
    slots: Sequence[GoldfishCard],
    swaps: Sequence[Swap],
    commanders: Sequence[GoldfishCard],
    combos: Sequence[GoldfishCombo],
    *,
    average_mana_value: float,
    seed: int,
    skip_irrelevant: bool = True,
) -> GoldfishComparison:
    """The figures of the deck with `swaps` made, on the games of `base` (a run of `slots` on `seed`).

    Each incoming card takes every slot its cut held, or the last slots of a `PartialSwap`, so a game that
    never drew a slot that changed, and whose searches never looked at a land or a card a tutor wants when
    the swap puts one in or takes one out, plays out as before; only the others are played again. Their
    outcomes join the base ones to make the report `simulate` would give for the swapped slots. A swap that
    changes the keep rule or the numbering of the cards plays every game again. With `skip_irrelevant`, a
    swap of plain spells for plain spells that play alike (`_plays_alike`) under the same rules changes
    nothing and plays nothing.
    """
    cuts = _cuts(slots, swaps)
    held = {card_key(card.name): card for card in slots}
    unchanged = GoldfishComparison(base, base, _changes(base, base, ()), ())
    if not cuts:
        return unchanged
    replaced = _replacements(cuts)
    after_slots = [replaced.get(i, card) for i, card in enumerate(slots)]
    some = frozenset(i for cut in cuts.values() if cut.partial for i in cut.slots)
    before = _Setup(slots, commanders, combos, base.bracket, average_mana_value)
    after = _Setup(
        after_slots,
        commanders,
        combos,
        base.bracket,
        swapped_average(slots, swaps, average_mana_value),
        late=[cut.incoming for cut in cuts.values() if cut.partial],
    )
    same_rules = _same_rules(before, after, some)
    if (
        skip_irrelevant
        and same_rules
        and all(_plays_alike(held[key], cut.incoming) for key, cut in cuts.items())
    ):
        return unchanged
    if same_rules:
        whole = frozenset(before.ids[i] for cut in cuts.values() if not cut.partial for i in cut.slots)
        looked = _looked_at(before, after, frozenset(replaced))
        touched = _Touched(whole, some, frozenset(before.ids[i] for i in some))
        replayed = _reached(base, seed, len(slots), touched, looked)
    else:
        replayed = tuple(range(len(base.outcomes)))
    fresh = {index: _Game(after, game_rng(seed, index)).play() for index in replayed}
    old = [before.cards[cid].src.name for cid in before.fin_ids]
    new = [after.cards[cid].src.name for cid in after.fin_ids]
    outcomes = tuple(
        fresh[index] if index in fresh else _carried(outcome, old, new)
        for index, outcome in enumerate(base.outcomes)
    )
    gone = {held[key].name for key, cut in cuts.items() if not cut.partial}
    lacking = {cut.incoming.name for cut in cuts.values() if not cut.incoming.modelled}
    not_modelled = tuple(sorted((set(base.not_modelled) - gone) | lacking))
    swapped = _report(after, base.bracket, outcomes, not_modelled, combos)
    return GoldfishComparison(base, swapped, _changes(base, swapped, replayed), replayed)


def compare_deck(
    deck: GoldfishDeck,
    base: GoldfishReport,
    swaps: Sequence[Swap],
    *,
    seed: int,
    skip_irrelevant: bool = True,
) -> GoldfishComparison:
    """`compare` for a compiled deck, `base` being `goldfish(deck, ..., seed=seed)`."""
    return compare(
        base,
        deck_slots(deck),
        swaps,
        deck.commanders,
        deck.combos,
        average_mana_value=deck.average_mana_value,
        seed=seed,
        skip_irrelevant=skip_irrelevant,
    )


def _same_rules(before: _Setup, after: _Setup, some: frozenset[int]) -> bool:
    """Whether the swap leaves the rules every game is played by as they were: the same card number in each
    slot but those of `some` (a card put in some slots of another), which hold the new numbers after all the
    old ones; the same keep rule and the same free combo pieces."""

    def rules(setup: _Setup) -> tuple[int, int, int, bool, bool]:
        return (setup.min_lands, setup.max_lands, setup.min_sources, setup.need_win, setup.need_syn)

    old = len(before.cards)
    new = {after.ids[i] for i in some}
    return (
        all(a == b for i, (a, b) in enumerate(zip(before.ids, after.ids, strict=True)) if i not in some)
        and before.cmd_ids == after.cmd_ids
        and min(new, default=old) >= old
        and before.combo_free == after.combo_free
        and rules(before) == rules(after)
    )


class _Looked(NamedTuple):
    """What the searches of a game look at, so which of them a swap can change."""

    # A cut or incoming card is a land, which a land search looks at.
    lands: bool
    # A cut or incoming card is a piece of a combo or a finisher, which a tutor looks for.
    wanted: bool


def _looked_at(before: _Setup, after: _Setup, cut_slots: frozenset[int]) -> _Looked:
    swapped = {(before.ids[i], after.ids[i]) for i in cut_slots}
    wanted = {cid for setup in (before, after) for pieces in setup.combo_pieces for cid in pieces or ()}
    wanted.update(before.fin_ids)
    wanted.update(after.fin_ids)
    return _Looked(
        lands=any(before.cards[a].is_land or after.cards[b].is_land for a, b in swapped),
        wanted=any(a in wanted or b in wanted for a, b in swapped),
    )


class _Touched(NamedTuple):
    """The cards a swap takes out of the games, by how much of them it takes."""

    # The numbers of the cards cut from all their slots.
    whole: frozenset[int]
    # The slots that a card cut from only some of its slots gives up, and those cards' numbers.
    some: frozenset[int]
    some_cids: frozenset[int]


def _reached(
    base: GoldfishReport, seed: int, size: int, touched: _Touched, looked: _Looked
) -> tuple[int, ...]:
    """The games a swap can change: those that drew a card cut from all its slots, those that drew a slot
    given up by a card cut from only some, and those whose searches looked at a kind of card that was cut
    or put in. `size` is the number of slots."""
    whole, some, some_cids = touched
    return tuple(
        index
        for index, outcome in enumerate(base.outcomes)
        if (looked.lands and outcome.land_search)
        or (looked.wanted and outcome.tutor_look)
        or not whole.isdisjoint(outcome.reached)
        or (
            not some_cids.isdisjoint(outcome.reached)
            and _drew_slot(outcome.shuffle_depths, game_rng(seed, index), size, some)
        )
    )


def _drew_slot(depths: Sequence[int], rng: random.Random, size: int, slots: frozenset[int]) -> bool:
    """Whether a game with the stream `rng` drew one of `slots` of the `size` in some shuffle. The game's
    shuffles come from its stream alone, so they are dealt again from it down to the depths it read."""
    for depth in depths:
        order = list(range(size))
        _shuffle_front(order, memoryview(rng.randbytes(_STEP_BYTES * size)).cast("Q"), 0, depth)
        if not slots.isdisjoint(order[:depth]):
            return True
    return False


def _carried(outcome: GameOutcome, old: Sequence[str], new: Sequence[str]) -> GameOutcome:
    """An outcome of a game the swap cannot change, with a finisher cut or put in: the cut one was never in
    hand and the new one never will be."""
    if old == new:
        return outcome
    turns = dict(zip(old, outcome.finisher_turns, strict=True))
    return outcome._replace(finisher_turns=tuple(turns.get(name, 0) for name in new))


type _Key = tuple[FigureKind, str, int | None]
type _Seen = Callable[[GameOutcome], int]


class _Figure(NamedTuple):
    value: float | None
    # 1 or 0 for a game, for a share; None for a median, a worst tenth and the fetched share.
    seen: _Seen | None
    # For the fetched share: the combo it belongs to.
    combo: int | None = None


def _commander_turns(outcome: GameOutcome) -> tuple[int, ...]:
    return outcome.commander_turns


def _combo_turns(outcome: GameOutcome) -> tuple[int, ...]:
    return outcome.combo_turns


def _finisher_turns(outcome: GameOutcome) -> tuple[int, ...]:
    return outcome.finisher_turns


def _screwed(outcome: GameOutcome) -> int:
    return int(outcome.colour_screw_turn > 0)


def _within(turns_of: Callable[[GameOutcome], tuple[int, ...]], index: int, limit: int) -> _Seen:
    def seen(outcome: GameOutcome) -> int:
        return int(0 < turns_of(outcome)[index] <= limit)

    return seen


def _figures(report: GoldfishReport) -> dict[_Key, _Figure]:
    figures: dict[_Key, _Figure] = {}
    kept: tuple[tuple[FigureKind, float, _Seen], ...] = (
        (FigureKind.KEPT_ON_SEVEN, report.kept_on_seven, lambda o: int(o.hand_size == _HAND)),
        (FigureKind.KEPT_ON_SIX, report.kept_on_six, lambda o: int(o.hand_size == _HAND - 1)),
        (
            FigureKind.KEPT_ON_FIVE_OR_FEWER,
            report.kept_on_five_or_fewer,
            lambda o: int(o.hand_size < _HAND - 1),
        ),
    )
    for kind, value, seen in kept:
        figures[(kind, "", None)] = _Figure(value, seen)
    for i, commander in enumerate(report.commanders):
        for turn, value in zip(report.turns, commander.cast_by, strict=True):
            figures[(FigureKind.COMMANDER_CAST_BY, commander.name, turn)] = _Figure(
                value, _within(_commander_turns, i, turn)
            )
        figures[(FigureKind.COMMANDER_MEDIAN, commander.name, None)] = _Figure(commander.median_turn, None)
    for turn, median, worst in zip(report.turns, report.mana_median, report.mana_worst_tenth, strict=True):
        figures[(FigureKind.MANA_MEDIAN, "", turn)] = _Figure(median, None)
        figures[(FigureKind.MANA_WORST_TENTH, "", turn)] = _Figure(worst, None)
    figures[(FigureKind.COLOUR_SCREW, "", None)] = _Figure(report.colour_screw, _screwed)
    ways = report.ways_to_win
    if ways is not None:
        for k, combo in enumerate(ways.combos):
            for turn, value in zip(report.turns, combo.landed_by, strict=True):
                figures[(FigureKind.COMBO_LANDED_BY, combo.id, turn)] = _Figure(
                    value, _within(_combo_turns, k, turn)
                )
            figures[(FigureKind.COMBO_FETCHED, combo.id, None)] = _Figure(combo.fetched_share, None, k)
        for i, finisher in enumerate(ways.finishers):
            for turn, value in zip(report.turns, finisher.castable_by, strict=True):
                figures[(FigureKind.FINISHER_CASTABLE_BY, finisher.name, turn)] = _Figure(
                    value, _within(_finisher_turns, i, turn)
                )
    return figures


def _changes(
    base: GoldfishReport, swapped: GoldfishReport, replayed: Sequence[int]
) -> tuple[FigureChange, ...]:
    """Every figure before and after, and whether it moved: a share by its paired difference over the games,
    the rest when they differ. Games not replayed differ by 0 in every share."""
    before, after = _figures(base), _figures(swapped)
    keys = [*before, *(key for key in after if key not in before)]
    shares = {
        key: (before[key].seen if key in before else None, after[key].seen if key in after else None)
        for key in keys
        if (key in before and before[key].seen) or (key in after and after[key].seen)
    }
    sums: dict[_Key, list[int]] = {}
    for index in replayed:
        was, now = base.outcomes[index], swapped.outcomes[index]
        for key, (old_seen, new_seen) in shares.items():
            difference = (new_seen(now) if new_seen else 0) - (old_seen(was) if old_seen else 0)
            if difference:
                cell = sums.setdefault(key, [0, 0])
                cell[0] += difference
                cell[1] += difference * difference
    changes: list[FigureChange] = []
    for key in keys:
        old, new = before.get(key), after.get(key)
        value_before = old.value if old is not None else 0.0
        value_after = new.value if new is not None else 0.0
        if key in shares:
            total, squares = sums.get(key, (0, 0))
            moved = is_real_shift(len(base.outcomes), total, squares)
        elif old is not None and old.combo is not None:
            moved = _fetched_moved(base, swapped, old.combo)
        else:
            moved = value_before != value_after
        changes.append(FigureChange(key[0], key[1], key[2], value_before, value_after, moved))
    return tuple(changes)


def _fetched_moved(base: GoldfishReport, swapped: GoldfishReport, k: int) -> bool:
    """Whether the share of landed combos that a tutor helped has moved. It is a ratio of two counts, so its
    spread comes from each game's pull on the ratio: how far its fetched count is from what its landed
    count predicts."""
    games = len(base.outcomes)

    def counts(report: GoldfishReport) -> tuple[list[int], list[int]]:
        landed = [int(o.combo_turns[k] > 0) for o in report.outcomes]
        fetched = [int(o.combo_turns[k] > 0 and o.combo_fetched >> k & 1) for o in report.outcomes]
        return landed, fetched

    landed_before, fetched_before = counts(base)
    landed_after, fetched_after = counts(swapped)
    if not sum(landed_before) or not sum(landed_after):
        return sum(fetched_before) != sum(fetched_after)
    ratio_before = sum(fetched_before) / sum(landed_before)
    ratio_after = sum(fetched_after) / sum(landed_after)
    if ratio_before == ratio_after:
        return False
    mean_before = sum(landed_before) / games
    mean_after = sum(landed_after) / games
    squares = 0.0
    for fb, lb, fa, la in zip(fetched_before, landed_before, fetched_after, landed_after, strict=True):
        pull = (fa - ratio_after * la) / mean_after - (fb - ratio_before * lb) / mean_before
        squares += pull * pull
    variance = squares / (games - 1) if games > 1 else 0.0
    return abs(ratio_after - ratio_before) > _SIGMAS * sqrt(variance / games)
