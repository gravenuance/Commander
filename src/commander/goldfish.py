"""The goldfish game (ADR 0054): a deck dealt and played many times by one fixed policy, with no opponents.

Pure: the input is the compiled cards of `goldfish_cards`, the output a `GoldfishReport` of figures plus the
outcome of every game. The game loop reads small ints and tuples built once per run, never rules text.

Determinism: game `i` of a run uses `game_rng(seed, i)` and nothing else, and its k-th shuffle is the k-th
`shuffle` of a list of `len(slots)` items on that stream, whatever was played before. A swap that puts a new
card into a cut card's slots (step 3) therefore replays the same shuffles, and a game whose outcome records
that the cut slot was never reached (`shuffle_depths`) and no search resolved (`searched`) is unchanged.
"""

import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import NamedTuple

from commander.answers import card_key
from commander.goldfish_cards import (
    CardKind,
    EntryCost,
    GoldfishCard,
    GoldfishCombo,
    GoldfishDeck,
    LandDestination,
    Tutor,
    TutorDestination,
)

# Raised whenever the policy or the recognition changes, so kept results are not mixed with new ones.
POLICY_VERSION = 1
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

# Colours as bits, so a mana unit is one small int: the set of colours it can pay.
_BITS = {"W": 1, "U": 2, "B": 4, "R": 8, "G": 16, "C": 32}
_POP = tuple(mask.bit_count() for mask in range(64))
_POP_KEY = _POP.__getitem__
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


# --- results ----------------------------------------------------------------------------------------------


class GameOutcome(NamedTuple):
    """What one game left behind, as plain numbers; turn 0 means "not within the turns played"."""

    # Cards in the kept opening hand: 7, 6 or 5.
    hand_size: int
    # Per commander, in the order given: the turn it was cast.
    commander_turns: tuple[int, ...]
    # Mana the permanents could make, on turn 1, 2, 3 ... up to the last turn played.
    mana: tuple[int, ...]
    # The first turn up to the middle one shown that ended with mana unspent while a spell in hand had the mana
    # for it but not the colours.
    colour_screw_turn: int
    # Per combo, in the order given: the turn it landed.
    combo_turns: tuple[int, ...]
    # Bit k is set when a tutor fetched a piece of combo k before it landed.
    combo_fetched: int
    # Per finisher, in `GoldfishReport` order: the first turn it could be cast.
    finisher_turns: tuple[int, ...]
    # Per shuffle: how many cards from the top were drawn or looked at (a search looks at all of them).
    shuffle_depths: tuple[int, ...]
    # Whether a land search or a tutor resolved, which reorders the library of the last shuffle.
    searched: bool


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


def _hall(needs: Mapping[int, int]) -> tuple[tuple[int, int], ...]:
    """Hall's condition for paying coloured pips from units that each pay some colours: for every set of
    pip colours, the units able to pay any of them must number as many as those pips."""
    terms: list[tuple[int, int]] = []
    for size in range(1, len(needs) + 1):
        for bits in combinations(sorted(needs), size):
            terms.append((sum(bits), sum(needs[b] for b in bits)))  # noqa: PERF401 — two sums per term
    return tuple(terms)


class Cost:
    """A cost in the form the payment checks read."""

    __slots__ = ("generic", "hall", "pip_bits", "single", "total")

    def __init__(self, total: int, needs: Mapping[int, int]) -> None:
        pips = sum(needs.values())
        self.total = max(total, pips)
        self.generic = self.total - pips
        self.hall = _hall(needs)
        self.pip_bits = tuple(bit for bit in sorted(needs) for _ in range(needs[bit]))
        self.single = next(iter(needs)) if len(needs) == 1 else 0


def cost_of(total: int, pips: Mapping[str, int]) -> Cost:
    needs: dict[int, int] = {}
    for colour, count in pips.items():
        bit = _BITS.get(colour, 0)
        if bit and count > 0:
            needs[bit] = needs.get(bit, 0) + count
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


def _match(units: Sequence[int], bits: Sequence[int]) -> list[int] | None:
    """Indexes of units paying each pip, found by trying the scarcest colour first and backing out."""
    scarcity = {bit: sum(1 for unit in units if unit & bit) for bit in set(bits)}
    order = sorted(bits, key=scarcity.__getitem__)
    used: list[int] = []

    def place(position: int) -> bool:
        if position == len(order):
            return True
        bit = order[position]
        tried: set[int] = set()
        for index, unit in enumerate(units):
            if not unit & bit or unit in tried or index in used:
                continue
            tried.add(unit)
            used.append(index)
            if place(position + 1):
                return True
            used.pop()
        return False

    return used if place(0) else None


def sort_units(units: list[int]) -> None:
    """Order mana units as `pay` expects them: the ones paying the fewest colours first."""
    units.sort(key=_POP_KEY)


def pay(units: list[int], cost: Cost) -> None:
    """Take the cost out of units, which are sorted by how many colours each pays, fewest first: coloured pips
    use the least flexible unit that can pay them, and generic mana the least flexible of what is left."""
    if cost.single:
        bit = cost.single
        for _ in cost.pip_bits:
            for index, unit in enumerate(units):
                if unit & bit:
                    del units[index]
                    break
    elif cost.pip_bits:
        chosen = _match(units, cost.pip_bits)
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
        "aura",
        "basic",
        "combo",
        "cost",
        "creature",
        "delayed",
        "draw_count",
        "engine",
        "entry",
        "extra_drops",
        "fast",
        "fin_cost",
        "finisher",
        "gain",
        "is_land",
        "keep_syn",
        "key",
        "land_amount",
        "land_mask",
        "land_tapped",
        "mana_amount",
        "mana_mask",
        "mana_perm",
        "mdfc",
        "mv",
        "needs_sac",
        "permanent",
        "pip_mask",
        "rank",
        "ritual",
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
        self.mana_perm = mana is not None and not self.ritual
        self.mana_amount = mana.amount if mana is not None else 0
        self.mana_mask = unit_mask(mana.colours) if mana is not None else 0
        self.delayed = mana is not None and mana.delayed
        self.aura = mana is not None and mana.on_land
        # Mana a ritual adds, net of the mana its own cost takes.
        self.gain = 0 if mana is None else mana.amount - (0 if spend_from_hand else card.mana_value)
        activation = card.land_ramp.activation if card.land_ramp is not None else 0
        self.cost = cost_of(0 if spend_from_hand else card.mana_value + activation, card.pips)
        fin = card.mana_value + FINISHER_X if card.x_cost else card.mana_value
        self.fin_cost = cost_of(fin, card.pips)
        self.pip_mask = unit_mask(colour for colour, count in card.pips.items() if count > 0)
        self.is_land = card.land is not None and CardKind.LAND in card.kinds
        self.mdfc = card.land is not None and not self.is_land
        land = card.land
        self.land_mask = unit_mask(land.colours) if land is not None else 0
        self.land_amount = land.amount if land is not None else 0
        self.land_tapped = land is not None and land.tapped
        name = card.name.casefold()
        self.basic = name in _BASICS or name.startswith("snow-covered ")
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
        self.win_piece = card.combo or card.tutor is not None
        self.rank = self._rank()

    def _rank(self) -> int:
        card = self.src
        if self.is_land or card.finisher or self.ritual:
            return _HELD
        cost = min(self.cost.total, _RANK_SPAN)
        if self.mana_perm or card.land_ramp is not None or card.extra_land_drops:
            return _MANA_RANK + cost
        if card.tutor is not None:
            return _TUTOR_RANK + cost
        if card.draw is not None:
            return _DRAW_RANK + cost
        if card.combo:
            return _COMBO_RANK + cost if card.permanent else _HELD
        return _OTHER_RANK + _RANK_SPAN - min(card.mana_value, _RANK_SPAN)


class _Setup:
    """Everything about a run that does not change from game to game."""

    __slots__ = (
        "average",
        "cards",
        "cmd_ids",
        "cmd_set",
        "combo_ids",
        "combo_pieces",
        "fin_ids",
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
    )

    def __init__(
        self,
        slots: Sequence[GoldfishCard],
        commanders: Sequence[GoldfishCard],
        combos: Sequence[GoldfishCombo],
        bracket: int | None,
        average_mana_value: float,
    ) -> None:
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

        self.ids = [add(card) for card in slots]
        self.cmd_ids = tuple(add(card) for card in commanders)
        self.cmd_set = frozenset(self.cmd_ids)
        self.combo_ids = tuple(combo.id for combo in combos)
        self.combo_pieces = tuple(_piece_ids(combo, by_key) for combo in combos)
        self.fin_ids = tuple(cid for cid, card in enumerate(self.cards) if card.finisher)
        self.has_rituals = any(card.ritual for card in self.cards)
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
        # A rule asking for what the deck has none of would send every hand to the bottom.
        self.need_win = rule >= _STRICT_KEEP and any(c.win_piece for c in library)
        self.need_syn = rule < _STRICT_KEEP and any(c.keep_syn and not c.is_land for c in library)

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
            pips: dict[str, int] = {}
            for cid in cids:
                card = self.cards[cid]
                total += card.cost.total
                for colour, count in card.src.pips.items():
                    pips[colour] = pips.get(colour, 0) + count
            found = self.merged[cids] = cost_of(total, pips)
        return found


def _piece_ids(combo: GoldfishCombo, by_key: Mapping[str, int]) -> tuple[int, ...] | None:
    """The cards of a combo, or None when one is not among the deck's cards."""
    found = [by_key.get(card_key(name)) for name in combo.pieces]
    ids = tuple(cid for cid in found if cid is not None)
    return ids if len(ids) == len(found) and ids else None


# --- one game ---------------------------------------------------------------------------------------------

type _Pick = tuple[int, _Card, tuple[int, ...], tuple[int, int] | None, int, bool]


class _Game:
    """The state of one game. Mana units are masks of the colours each can pay, sorted by how many that is."""

    __slots__ = (
        "bf",
        "check_land",
        "cmd_left",
        "cmd_turn",
        "combo_fetched",
        "combo_turn",
        "creatures",
        "drops_left",
        "engines",
        "extra_drops",
        "fetched",
        "fin_turn",
        "hand",
        "hand_size",
        "lands_in_play",
        "lib",
        "mana",
        "open_combos",
        "open_fins",
        "pending",
        "pool",
        "rng",
        "s",
        "screw",
        "searched",
        "shuffles",
        "top",
        "turn",
        "units",
    )

    def __init__(self, setup: _Setup, rng: random.Random) -> None:
        self.s = setup
        self.rng = rng
        self.lib: list[int] = []
        self.top = 0
        self.hand: list[int] = []
        self.hand_size = _HAND
        self.shuffles = 0
        self.bf: set[int] = set()
        self.pool: list[int] = []
        self.units: list[int] = []
        self.pending: list[int] = []
        self.creatures: list[tuple[int, int, int]] = []
        self.engines = 0
        self.extra_drops = 0
        self.drops_left = 0
        self.lands_in_play = 0
        self.check_land = False
        self.cmd_left = list(setup.cmd_ids)
        self.cmd_turn = dict.fromkeys(setup.cmd_ids, 0)
        self.fetched: set[int] = set()
        self.searched = False
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
        depth = len(s.ids) if self.searched else self.top
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
        )

    def _opening(self) -> None:
        """Deal, and mulligan until the keep rule is met: the first mulligan is free, the later ones draw
        seven and put one card on the bottom each past the free one, and a hand of five is kept as it is."""
        s = self.s
        while True:
            lib = list(s.ids)
            self.rng.shuffle(lib)
            self.shuffles += 1
            bottoms = max(self.shuffles - 1 - _FREE_MULLIGANS, 0)
            hand = lib[:_HAND]
            kept, bottomed = self._bottom(hand, bottoms)
            if bottoms >= 2 or s.keeps(kept):  # noqa: PLR2004 — five cards, kept whatever they hold
                break
        self.lib = lib
        self.lib.extend(bottomed)
        self.top = len(hand)
        self.hand = kept
        self.hand_size = _HAND - bottoms

    def _bottom(self, hand: list[int], count: int) -> tuple[list[int], list[int]]:
        """Put `count` cards on the bottom: lands past four first, then the dearest spell that is not a combo
        piece, a tutor or a finisher, then the dearest of the rest, then a land."""
        cards = self.s.cards
        kept = list(hand)
        bottomed: list[int] = []
        for _ in range(count):
            if not kept:
                break
            lands = [cid for cid in kept if cards[cid].is_land]
            spells = [cid for cid in kept if not cards[cid].is_land]
            free = [cid for cid in spells if not cards[cid].win_piece and not cards[cid].finisher]
            if len(lands) > _MAX_KEPT_LANDS or not spells:
                pick = min(
                    lands, key=lambda cid: (_POP[cards[cid].land_mask], cards[cid].land_tapped is False)
                )
            else:
                pick = max(free or spells, key=lambda cid: cards[cid].mv)
            kept.remove(pick)
            bottomed.append(pick)
        return kept, bottomed

    def _take_turn(self) -> None:
        s = self.s
        pool = self.pool
        if self.pending:
            pool.extend(self.pending)
            self.pending.clear()
            pool.sort(key=_POP_KEY)
        self._draw(1 + self.engines)
        self.drops_left = 1 + self.extra_drops
        self.units = list(pool)
        self._play_lands()
        self._act()
        if self.turn <= s.mid and not self.screw:
            self._check_screw()
        self.mana.append(len(pool))

    def _draw(self, count: int) -> None:
        lib = self.lib
        for _ in range(count):
            if self.top >= len(lib):
                return
            self.hand.append(lib[self.top])
            self.top += 1
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
            self._put_land(cid, cards[cid], tapped=cards[cid].land_tapped)

    def _put_land(self, cid: int, card: _Card, *, tapped: bool) -> None:
        self.lands_in_play += 1
        self.bf.add(cid)
        if tapped:
            self.pending.extend([card.land_mask] * card.land_amount)
        else:
            self._add_now(card.land_mask, card.land_amount)

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
        extra = False
        for cid in (*self.hand, *self.cmd_left):
            card = cards[cid]
            if card.is_land or (card.rank >= _HELD and cid not in self.s.cmd_set):
                continue
            if card.cost.total <= have + 2:
                want |= card.pip_mask
            extra = extra or have < card.cost.total <= have + 1
        return want, want & ~cover, extra

    def _find_land(self, *, basic_only: bool) -> int:
        """The index in the library of the land to fetch, or -1: the one best for the colours wanted."""
        cards = self.s.cards
        first: dict[int, int] = {}
        for index in range(self.top, len(self.lib)):
            cid = self.lib[index]
            card = cards[cid]
            if cid not in first and card.is_land and (card.basic or not basic_only):
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
        self.pool.sort(key=_POP_KEY)
        self.units.extend(added)
        self.units.sort(key=_POP_KEY)

    def _remove_source(self, mask: int, count: int) -> None:
        for _ in range(count):
            for source in (self.pool, self.units, self.pending):
                if mask in source:
                    source.remove(mask)

    def _plan(self, units: list[int], cost: Cost) -> tuple[int, ...] | None:
        """The rituals and spirit guides to use first for a cost to be paid: none when the mana is enough,
        None when even they would not make it."""
        if can_pay(units, cost):
            return ()
        if not self.s.has_rituals:
            return None
        cards = self.s.cards
        rituals = sorted(
            (cid for cid in self.hand if cards[cid].ritual), key=lambda cid: cards[cid].cost.total
        )
        trial = list(units)
        used: list[int] = []
        for cid in rituals:
            card = cards[cid]
            if not can_pay(trial, card.cost):
                continue
            pay(trial, card.cost)
            trial.extend([card.mana_mask] * card.mana_amount)
            trial.sort(key=_POP_KEY)
            used.append(cid)
            if can_pay(trial, cost):
                return tuple(used)
        return None

    def _use_ritual(self, cid: int) -> None:
        card = self.s.cards[cid]
        self.hand.remove(cid)
        pay(self.units, card.cost)
        self.units.extend([card.mana_mask] * card.mana_amount)
        self.units.sort(key=_POP_KEY)

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
        reach = len(units)
        if s.has_rituals:
            reach += sum(max(cards[cid].gain, 0) for cid in self.hand if cards[cid].ritual)
        best: _Pick | None = None
        best_rank = _HELD
        for cid in self.hand:
            card = cards[cid]
            if card.rank < best_rank and card.cost.total <= reach:
                found = self._eligible(cid, card, units)
                if found is not None:
                    best, best_rank = (cid, card, *found, False), card.rank
        for cid in self.cmd_left:
            card = cards[cid]
            rank = _COMMANDER_RANK + min(card.mv, _RANK_SPAN)
            if rank < best_rank and card.cost.total <= reach:
                found = self._eligible(cid, card, units)
                if found is not None:
                    best, best_rank = (cid, card, *found, True), rank
        return best

    def _eligible(
        self, cid: int, card: _Card, units: list[int]
    ) -> tuple[tuple[int, ...], tuple[int, int] | None, int] | None:
        """The plan to pay for a card, where its search goes and the card it costs from the hand, if it can be
        cast now."""
        if (card.aura and not self.lands_in_play) or (card.needs_sac and not self.creatures):
            return None
        target = None
        if card.src.tutor is not None:
            target = self._target(card, units)
            if target is None:
                return None
        entry = -1
        if card.entry is not None:
            entry = self._entry_card(cid, card)
            if entry < 0:
                return None
        plan = self._plan(units, card.cost)
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
        if from_command:
            self.cmd_left.remove(cid)
            self.cmd_turn[cid] = self.turn
        else:
            self.hand.remove(cid)
        if entry >= 0:
            self.hand.remove(entry)
        if card.needs_sac:
            self._sacrifice()
        if card.src.land_ramp is not None:
            self._ramp_lands(card)
        if target is not None:
            self._fetch(card, target)
        self._enter(cid, card, from_command=from_command)

    def _enter(self, cid: int, card: _Card, *, from_command: bool = False) -> None:
        """What a resolved card leaves behind: the permanent, its mana, land drops and cards drawn."""
        if card.permanent:
            self.bf.add(cid)
        if card.creature:
            rank = 2 if from_command else (1 if card.mana_perm else 0)
            self.creatures.append((rank, card.mv, cid))
        if card.mana_perm:
            if card.delayed:
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

    def _sacrifice(self) -> int:
        """Sacrifice the cheapest creature, one that makes no mana before a mana creature before a commander;
        returns its mana value."""
        entry = min(self.creatures)
        self.creatures.remove(entry)
        rank, mv, cid = entry
        self.bf.discard(cid)
        if rank == 1:
            card = self.s.cards[cid]
            self._remove_source(card.mana_mask, card.mana_amount)
        return mv

    # -- searching ----------------------------------------------------------------------------------------

    def _ramp_lands(self, card: _Card) -> None:
        ramp = card.src.land_ramp
        if ramp is None:
            return
        self.searched = True
        cards = self.s.cards
        for index in range(ramp.count):
            found = self._find_land(basic_only=ramp.basic_only)
            if found < 0:
                return
            cid = self.lib.pop(found)
            if index >= ramp.count - ramp.to_hand:
                self.hand.append(cid)
                self.check_land = True
            else:
                tapped = ramp.destination is not LandDestination.UNTAPPED
                self._put_land(cid, cards[cid], tapped=tapped)

    def _target(self, card: _Card, units: list[int]) -> tuple[int, int] | None:
        """The card a tutor would fetch and its place in the library: a piece of the combo nearest to landing
        that the tutor can find, else a finisher."""
        tutor = card.src.tutor
        if tutor is None:
            return None
        limit = tutor.max_mana_value
        if tutor.x_limited:
            limit = len(units) - card.cost.total
        elif tutor.value_over_sacrifice is not None:
            # With no creature to sacrifice nothing fits.
            limit = tutor.value_over_sacrifice + min(self.creatures)[1] if self.creatures else -1
        cards = self.s.cards
        return next(((index, cid) for cid, index in self._wanted() if _finds(cards[cid], tutor, limit)), None)

    def _wanted(self) -> list[tuple[int, int]]:
        """What a tutor is after that is in the library, best first, with its place there: the missing
        pieces of the combos with the fewest missing, dearest first, then the finishers not castable yet."""
        s = self.s
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

    def _fetch(self, card: _Card, target: tuple[int, int]) -> None:
        index, cid = target
        tutor = card.src.tutor
        if tutor is None:
            return
        self.searched = True
        self.lib.pop(index)
        self.fetched.add(cid)
        if tutor.destination is TutorDestination.HAND:
            self.hand.append(cid)
        elif tutor.destination is TutorDestination.TOP:
            self.lib.insert(self.top, cid)
        else:
            self._enter(cid, self.s.cards[cid])

    # -- ways to win --------------------------------------------------------------------------------------

    def _check_combos(self) -> None:
        """A combo lands once every piece is out, is a commander, or can be cast from the hand with the mana
        left; it is then recorded and the game goes on."""
        s = self.s
        for k in list(self.open_combos):
            pieces = s.combo_pieces[k]
            if pieces is None:
                continue
            cast: list[int] = []
            for piece in pieces:
                if piece in self.bf or piece in s.cmd_set:
                    continue
                if piece not in self.hand:
                    break
                cast.append(piece)
            else:
                if cast and self._plan(self.units, s.merged_cost(tuple(cast))) is None:
                    continue
                self.combo_turn[k] = self.turn
                if any(piece in self.fetched for piece in pieces):
                    self.combo_fetched |= 1 << k
                self.open_combos.remove(k)

    def _check_finishers(self) -> None:
        s = self.s
        for position in list(self.open_fins):
            cid = s.fin_ids[position]
            if (cid in self.hand or cid in self.cmd_left) and self._plan(
                self.units, s.cards[cid].fin_cost
            ) is not None:
                self._record_finisher(cid)

    def _record_finisher(self, cid: int) -> None:
        position = self.s.fin_ids.index(cid)
        if not self.fin_turn[position]:
            self.fin_turn[position] = self.turn
            self.open_fins.remove(position)


def _finds(card: _Card, tutor: Tutor, limit: int | None) -> bool:
    """Whether a tutor can fetch the card: a permanent for the battlefield, of a type it searches, within the
    mana value limit, and no land."""
    return (
        (limit is None or card.mv <= limit)
        and (not tutor.finds or bool(card.src.kinds & tutor.finds))
        and (card.permanent or tutor.destination is not TutorDestination.BATTLEFIELD)
        and not card.is_land
    )


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
