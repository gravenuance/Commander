"""The suggested change to a deck's mana base (ADR 0024): the land count, basics, named lands and untapped
swaps for tap-lands. It is built on the analysis in `manabase` and checked by running it on the changed deck.
"""

import math
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace

from commander.carddb import COLORS, CardMana, back_is_land, front_type
from commander.manabase import (
    COLOUR_NAMES,
    CONDITIONAL_TAPPED,
    GOLD_EXTRA,
    RESTRICTED,
    TABLE_LANDS,
    ManaBase,
    Source,
    analyse_mana,
    count_sources,
    deck_entries,
    fixed_pips,
    info_is_land,
    is_mana_card,
    needed_sources,
    tapped_share,
    without_commanders,
)
from commander.models import Deck

# The suggestion (ADR 0024): a land count this close to Karsten's is left alone.
LAND_TOLERANCE = 1
# Needs are shown to a tenth; a gap smaller than that is no shortfall.
SHORT_MARGIN = 0.05
MAX_NAMED_LANDS = 4
MAX_TAPPED_SWAPS = 3
BASIC_NAMES = {"W": "Plains", "U": "Island", "B": "Swamp", "R": "Mountain", "G": "Forest"}
# Lines of a land's text that make it a plain tap-land, with nothing else worth a slower turn.
_PLAIN_LAND_LINES = (
    "enters tapped",
    "enters the battlefield tapped",
    "{t}: add",
    "choose a color",
    "gain 1 life",
)


@dataclass(slots=True, frozen=True)
class LandOption:
    """A land the deck could run, from the card data; options come most played first."""

    mana: CardMana
    cents: int | None


@dataclass(slots=True, frozen=True)
class Swap:
    """A change that keeps the deck's size: copies of add in, as many copies of cut out."""

    add: str
    copies: int
    cut: str
    reason: str
    # One added copy's cheapest price; None for basics and unpriced cards.
    cents: int | None = None
    # Whether it is within the deck's budget (ADR 0025); basics always are.
    within_budget: bool = True


@dataclass(slots=True, frozen=True)
class BasicCount:
    colour: str
    name: str
    now: int
    # After the land count and basic-for-basic moves alone.
    after: int
    # Of the rest, how many the named lands among the swaps replace; kept apart so the count never reads as
    # a cut when those lands are left out.
    replaced: int = 0


@dataclass(slots=True, frozen=True)
class ColourOutcome:
    colour: str
    short_now: int
    short_after: int


@dataclass(slots=True, frozen=True)
class ManaPlan:
    """How the mana base should change: land count, basics by type, then named lands (ADR 0024)."""

    lands: float
    target: float
    # Lands to add (positive) or cut (negative); 0 leaves the count as it is.
    land_change: int
    # For more lands: the spells that could make room, highest mana value first.
    cut_candidates: tuple[str, ...]
    basics: tuple[BasicCount, ...]
    swaps: tuple[Swap, ...]
    colours: tuple[ColourOutcome, ...]
    # Cards still short of sources once the plan is followed.
    still_short: tuple[str, ...]
    # The dearest a land within the deck's budget may be (ADR 0025); None when unknown.
    budget_cents: int | None = None

    @property
    def unchanged(self) -> bool:
        return not self.land_change and not self.swaps


@dataclass(slots=True, frozen=True)
class _Demand:
    """One card's call on a colour: Karsten's table number and the sources other than that colour's basics."""

    table: int
    gold: bool
    others: float

    def basics_needed(self, lands: float) -> float:
        return self.table * lands / TABLE_LANDS + (GOLD_EXTRA if self.gold else 0) - self.others


def suggest_mana(
    deck: Deck,
    cards: Mapping[str, CardMana],
    report: ManaBase,
    *,
    alternative_cost: Iterable[str] = (),
    options: Sequence[LandOption] = (),
    budget_cents: int | None = None,
) -> ManaPlan:
    """The smallest change that meets Karsten's numbers: the land count, then basics, then named lands.

    The land count moves only when it is more than LAND_TOLERANCE off his formula and outside the range of
    the compared lists. Basics then move from colours with sources to spare to colours short of them. What
    basics cannot close, untapped lands from options close, each in place of a basic of a colour it also
    makes; plain tap-lands get an untapped land of the same colours. A named land within budget_cents (its
    budget line, ADR 0025) comes before a more played one over it; one over it is taken only when none within
    fits, and is marked so. cards must hold every basic land type.
    """
    colours = [colour.colour for colour in report.colours]
    alternative = {name.casefold() for name in alternative_cost}
    the_99 = without_commanders(deck_entries(deck, cards))
    held = {info.name.casefold() for _, info in the_99}
    names = _basic_names(the_99, colours, cards)
    now = {c: sum(q for q, info in the_99 if info.name == names.get(c)) for c in colours}
    change = _carried_by_basics(_land_change(report), now, names)
    lands_after = report.lands.lands + change
    demands = _demands(deck, cards, colours, names, alternative)
    total = sum(now.values()) + change
    need = {
        c: max(
            (d.basics_needed(lands_after) for d in demands[c] if d.basics_needed(lands_after) <= total),
            default=0.0,
        )
        for c in colours
    }
    counted = _count_change(now, need, change, names)
    after = _rebalance(counted, need, names)
    swaps = _basic_swaps(counted, after, names)
    # _named_lands takes each replaced basic off after; basics_only keeps the count before it.
    basics_only = dict(after)
    budget = _Budget(budget_cents)
    named = _named_lands(after, need, names, options, budget=budget, held=held, deck_colours=len(colours))
    taken = held | {s.add.casefold() for s in named}
    tapped = _tapped_swaps(the_99, options, budget=budget, held=taken, colours=colours)
    plan_swaps = (*swaps, *named, *tapped)
    cut_candidates = _cut_candidates(the_99, change)
    followed = _followed(deck, names, now, after, swaps=plan_swaps, cuts=cut_candidates[: max(change, 0)])
    known = {**cards, **{o.mana.name.casefold(): o.mana for o in options}}
    report_after = analyse_mana(followed, known, alternative_cost=alternative_cost)
    short_after = {colour.colour: colour.shortfalls for colour in report_after.colours}
    return ManaPlan(
        lands=report.lands.lands,
        target=report.lands.suggested,
        land_change=change,
        cut_candidates=cut_candidates,
        basics=tuple(
            BasicCount(c, names[c], now[c], basics_only[c], basics_only[c] - after[c])
            for c in colours
            if c in names
        ),
        swaps=plan_swaps,
        colours=tuple(
            ColourOutcome(colour.colour, len(colour.shortfalls), len(short_after.get(colour.colour, ())))
            for colour in report.colours
        ),
        still_short=tuple(sorted({need.card for needs in short_after.values() for need in needs})),
        budget_cents=budget_cents,
    )


def _land_change(report: ManaBase) -> int:
    lands = report.lands.lands
    # Both counts are to a hundredth, so rounding the difference too keeps float noise off the edges.
    off = round(report.lands.suggested - lands, 2)
    if abs(off) <= LAND_TOLERANCE:
        return 0
    compared = report.comparison
    if compared is not None and compared.lands_low <= lands <= compared.lands_high:
        return 0
    # Python rounds halves to even; a half land short is a whole one, over or under.
    return int(math.copysign(math.floor(abs(off) + 0.5), off))


def _carried_by_basics(change: int, now: Mapping[str, int], names: Mapping[str, str]) -> int:
    """The land change basics can carry: added as a colour's basic, cut from the basics held. A colourless
    deck has no basic to add, and the spells made room for would be cut for nothing."""
    if change > 0:
        return change if names else 0
    return max(change, -sum(now.values()))


def _basic_names(
    the_99: Sequence[tuple[int, CardMana]], colours: Sequence[str], cards: Mapping[str, CardMana]
) -> dict[str, str]:
    """Each colour's basic: the one the deck runs most (a snow basic, say), else the plain one."""
    names: dict[str, str] = {}
    for colour in colours:
        runs = sorted(
            ((q, info.name) for q, info in the_99 if _is_basic(info) and info.produced == colour),
            reverse=True,
        )
        if runs:
            names[colour] = runs[0][1]
        elif BASIC_NAMES[colour].casefold() in cards:
            names[colour] = BASIC_NAMES[colour]
    return names


def _is_basic(info: CardMana) -> bool:
    return front_type(info.type_line).startswith("Basic ") and info_is_land(info)


def _demands(
    deck: Deck,
    cards: Mapping[str, CardMana],
    colours: Sequence[str],
    names: Mapping[str, str],
    alternative: set[str],
) -> dict[str, list[_Demand]]:
    """Every card's call on each colour, on curve and, for one-drops, from untapped lands on turn one."""
    entries = deck_entries(deck, cards)
    basics = set(names.values())
    others = [
        Source.of(info, quantity, len(colours))
        for quantity, info, commander in entries
        if not commander and info.name not in basics
    ]
    spells = [
        info for _, info, _ in entries if not info_is_land(info) and info.name.casefold() not in alternative
    ]
    found: dict[str, list[_Demand]] = {c: [] for c in colours}
    for info in spells:
        pips = fixed_pips(info.mana_cost)
        gold = sum(1 for c in pips if c in COLORS) > 1
        mana_value = max(int(info.cmc), 1)
        for colour in colours:
            if colour not in pips:
                continue
            table = needed_sources(mana_value, pips[colour])
            for untapped in (False, True) if mana_value == 1 else (False,):
                have = count_sources(others, colour, mana_value, untapped_only=untapped)
                found[colour].append(_Demand(table, gold, have))
    return found


def _deficit(colour: str, basics: Mapping[str, int], need: Mapping[str, float]) -> float:
    return need[colour] - basics[colour]


def _count_change(
    now: Mapping[str, int], need: Mapping[str, float], change: int, names: Mapping[str, str]
) -> dict[str, int]:
    """Added lands go to the colours shortest of sources; cut lands come from those with most to spare."""
    basics = dict(now)
    for _ in range(abs(change)):
        if change > 0:
            pool = [c for c in basics if c in names]
            pick = max(pool, key=lambda c: _deficit(c, basics, need), default=None)
            if pick is not None:
                basics[pick] += 1
        else:
            pool = [c for c in basics if basics[c] > 0]
            pick = min(pool, key=lambda c: _deficit(c, basics, need), default=None)
            if pick is not None:
                basics[pick] -= 1
    return basics


def _rebalance(
    start: Mapping[str, int], need: Mapping[str, float], names: Mapping[str, str]
) -> dict[str, int]:
    """Moves basics one at a time to the shortest colour from the colour with most to spare, never making a
    colour that meets its needs short."""
    basics = dict(start)
    for _ in range(sum(basics.values())):
        short = [c for c in basics if c in names and _deficit(c, basics, need) > SHORT_MARGIN]
        donors = [c for c in basics if basics[c] > 0 and _deficit(c, basics, need) + 1 <= SHORT_MARGIN]
        if not short or not donors:
            break
        to = max(short, key=lambda c: _deficit(c, basics, need))
        source = min(donors, key=lambda c: _deficit(c, basics, need))
        basics[source] -= 1
        basics[to] += 1
    return basics


def _basic_swaps(start: Mapping[str, int], end: Mapping[str, int], names: Mapping[str, str]) -> list[Swap]:
    """Pairs the colours that gain basics with those that give them up."""
    gains = {c: end[c] - start[c] for c in end if end[c] > start[c]}
    losses = {c: start[c] - end[c] for c in end if end[c] < start[c]}
    swaps: list[Swap] = []
    for gain, gained in gains.items():
        wanted = gained
        for loss in list(losses):
            moved = min(wanted, losses[loss])
            if not moved:
                continue
            swaps.append(Swap(names[gain], moved, names[loss], f"{COLOUR_NAMES[gain]} is short of sources"))
            wanted -= moved
            losses[loss] -= moved
            if not wanted:
                break
    return swaps


@dataclass(slots=True)
class _Budget:
    """The dearest a land within the deck's budget may be (ADR 0025); None when nothing in it has a price."""

    average: int | None

    def fits(self, option: LandOption) -> bool:
        return self.average is None or (option.cents is not None and option.cents <= self.average)

    def pick[T](
        self, tries: Sequence[Callable[[], Iterator[tuple[LandOption, T]]]]
    ) -> tuple[LandOption, T, bool] | None:
        """The first candidate within budget over all tries in order, else the first at any price; the flag
        says whether it is within budget."""
        for found in tries:
            for option, extra in found():
                if self.fits(option):
                    return option, extra, True
        for found in tries:
            for option, extra in found():
                return option, extra, False
        return None


def _named_lands(
    basics: dict[str, int],
    need: Mapping[str, float],
    names: Mapping[str, str],
    options: Sequence[LandOption],
    *,
    budget: _Budget,
    held: set[str],
    deck_colours: int,
) -> list[Swap]:
    """Untapped lands for the gaps basics leave, each in place of a basic of a colour it also makes."""
    swaps: list[Swap] = []
    taken = set(held)
    extra = dict.fromkeys(basics, 0.0)

    def gap(colour: str) -> float:
        return need[colour] - basics[colour] - extra[colour]

    def tries(colour: str) -> Callable[[], Iterator[tuple[LandOption, tuple[str, str]]]]:
        return lambda: (
            (option, (colour, partner))
            for option, partner in _lands_for(
                colour, gap, basics, options, taken=taken, deck_colours=deck_colours
            )
        )

    while len(swaps) < MAX_NAMED_LANDS:
        short = sorted((c for c in basics if gap(c) > SHORT_MARGIN), key=gap, reverse=True)
        found = budget.pick([tries(colour) for colour in short])
        if found is None:
            return swaps
        option, (colour, partner), within = found
        taken.add(option.mana.name.casefold())
        basics[partner] -= 1
        extra[colour] += 1
        extra[partner] += 1
        reason = f"{COLOUR_NAMES[colour]} is short of sources; enters untapped"
        swaps.append(Swap(option.mana.name, 1, names[partner], reason, option.cents, within))
    return swaps


def _lands_for(
    colour: str,
    gap: Callable[[str], float],
    basics: Mapping[str, int],
    options: Sequence[LandOption],
    *,
    taken: set[str],
    deck_colours: int,
) -> Iterator[tuple[LandOption, str]]:
    """Untapped lands making colour, most played first, each with a colour whose basic it can replace, the
    one with most to spare. A fetch land may replace only a basic of a colour that meets its needs, since it
    finds nothing once the basics it fetches are gone; a land that makes both colours keeps them level."""
    for option in options:
        if option.mana.name.casefold() in taken or not _offerable(option.mana):
            continue
        source = Source.of(option.mana, 1, deck_colours)
        if source.tapped > CONDITIONAL_TAPPED or source.colours.get(colour, 0.0) < 1:
            continue
        partners = [
            c
            for c in basics
            if c != colour
            and basics[c] > 0
            and source.colours.get(c, 0.0) >= 1
            and (option.mana.produced or gap(c) <= SHORT_MARGIN)
        ]
        if partners:
            yield option, min(partners, key=gap)


def _tapped_swaps(
    the_99: Sequence[tuple[int, CardMana]],
    options: Sequence[LandOption],
    *,
    budget: _Budget,
    held: set[str],
    colours: Sequence[str],
) -> list[Swap]:
    """Plain tap-lands, each with the most played untapped land that makes the same deck colours."""
    swaps: list[Swap] = []
    taken = set(held)

    def twins(makes: set[str]) -> Callable[[], Iterator[tuple[LandOption, None]]]:
        def found() -> Iterator[tuple[LandOption, None]]:
            for option in options:
                if option.mana.name.casefold() in taken or not _offerable(option.mana):
                    continue
                source = Source.of(option.mana, 1, len(colours))
                if source.tapped == 0 and all(source.colours.get(c, 0.0) >= 1 for c in makes):
                    yield option, None

        return found

    for _, info in the_99:
        if len(swaps) >= MAX_TAPPED_SWAPS:
            break
        text = info.oracle_text.casefold()
        if not info_is_land(info) or _is_basic(info) or tapped_share(text) < 1 or not _plain(text):
            continue
        makes = {
            c for c, worth in Source.of(info, 1, len(colours)).colours.items() if c in colours and worth > 0
        }
        # No colour of the deck's to match, so no land is the same colours.
        found = budget.pick([twins(makes)]) if makes else None
        if found is None:
            continue
        option, _, within = found
        taken.add(option.mana.name.casefold())
        swaps.append(
            Swap(option.mana.name, 1, info.name, "same colours, enters untapped", option.cents, within)
        )
    return swaps


def _offerable(info: CardMana) -> bool:
    return info_is_land(info) and not _is_basic(info) and RESTRICTED not in info.oracle_text.casefold()


def _plain(text: str) -> bool:
    return all(any(part in line for part in _PLAIN_LAND_LINES) for line in text.splitlines() if line.strip())


def _cut_candidates(the_99: Sequence[tuple[int, CardMana]], change: int) -> tuple[str, ...]:
    """For more lands, the costliest spells: Karsten's count trades the top of the curve for lands. Cards
    that make mana or fetch lands are ramp, which the count already weighs, so they stay."""
    if change <= 0:
        return ()
    spells = sorted(
        ((info.cmc, info.name) for _, info in the_99 if _cuttable(info)),
        key=lambda pair: (-pair[0], pair[1]),
    )
    return tuple(name for _, name in spells[: change + 2])


def _cuttable(info: CardMana) -> bool:
    if info_is_land(info) or back_is_land(info.type_line) or is_mana_card(info):
        return False
    text = info.oracle_text.casefold()
    return not ("search your library" in text and "land" in text)


def _followed(
    deck: Deck,
    names: Mapping[str, str],
    now: Mapping[str, int],
    after: Mapping[str, int],
    *,
    swaps: Sequence[Swap],
    cuts: Sequence[str],
) -> Deck:
    """The deck as the plan leaves it, so the analysis can be run on it again."""
    counts: dict[str, int] = {card.name: card.quantity for card in deck.main_cards}
    for colour, name in names.items():
        counts[name] = counts.get(name, 0) + after[colour] - now[colour]
    basics = set(names.values())
    for swap in swaps:
        if swap.add in basics and swap.cut in basics:
            continue  # already in after
        counts[swap.add] = counts.get(swap.add, 0) + swap.copies
        if swap.cut not in basics:
            counts[swap.cut] = counts.get(swap.cut, 0) - swap.copies
    for name in cuts:
        counts[name] = counts.get(name, 0) - 1
    commanders = [card for card in deck.main_cards if card.name in deck.commanders]
    kept = {card.name for card in commanders}
    others = [card for card in deck.main_cards if card.name not in kept]
    if not others:
        return deck
    # A plain row to copy, so no added card picks up the Commander category.
    template = replace(others[0], categories=())
    rows = [
        replace(template, name=name, quantity=quantity)
        for name, quantity in counts.items()
        if quantity > 0 and name not in kept
    ]
    return replace(deck, cards=(*commanders, *rows))


def plan_json(plan: ManaPlan) -> dict[str, object]:
    """The suggestion for the app's Mana tab."""
    return {
        "lands": plan.lands,
        "target": plan.target,
        "landChange": plan.land_change,
        "cutCandidates": list(plan.cut_candidates),
        "basics": [
            {"colour": b.colour, "name": b.name, "now": b.now, "after": b.after, "replaced": b.replaced}
            for b in plan.basics
        ],
        "swaps": [
            {
                "add": s.add,
                "copies": s.copies,
                "cut": s.cut,
                "reason": s.reason,
                "cents": s.cents,
                "withinBudget": s.within_budget,
            }
            for s in plan.swaps
        ],
        "colours": [
            {"colour": c.colour, "shortNow": c.short_now, "shortAfter": c.short_after} for c in plan.colours
        ],
        "stillShort": list(plan.still_short),
        "budgetCents": plan.budget_cents,
        "unchanged": plan.unchanged,
    }
