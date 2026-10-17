"""A deck's mana base against Frank Karsten's published maths (ADR 0019).

Land count: his 99-card regression, 31.42 + 3.13 x average mana value - 0.28 x cheap draw and ramp
("How Many Lands Do You Need in Your Deck? An Updated Analysis", TCGplayer, 2022). Colour sources: his
99-card table of sources needed to cast a spell on curve ("How Many Sources Do You Need to Consistently
Cast Your Spells? A 2022 Update", TCGplayer, 2022), with his rules for gold cards, partial sources and
other land counts. Both assume casual Commander; cEDH decks are compared with tournament lists instead.
"""

import math
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from statistics import median

from commander.carddb import FACE_SEPARATOR, CardMana, back_is_land, front_face, front_type
from commander.models import Deck

COLOURS = "WUBRG"
# Karsten's 99-card table: sources needed for a spell of mana value M with N pips of one colour, keyed
# (M, N). It assumes 41 lands; other land counts scale it.
SOURCES_NEEDED: Mapping[tuple[int, int], int] = {
    (1, 1): 19, (2, 1): 19, (3, 1): 18, (4, 1): 16, (5, 1): 15, (6, 1): 14,
    (2, 2): 30, (3, 2): 28, (4, 2): 26, (5, 2): 23, (6, 2): 22, (7, 2): 20,
    (3, 3): 36, (4, 3): 33, (5, 3): 30, (6, 3): 28, (7, 3): 26,
    (4, 4): 39, (5, 4): 36,
}  # fmt: skip
TABLE_LANDS = 41
# The land count formula's coefficients for 99-card decks.
LAND_BASE = 31.42
LAND_PER_MANA_VALUE = 3.13
LAND_PER_CHEAP_SPELL = 0.28
CHEAP = 2
# A land/spell card (Shatterskull Smashing) counts as part of a land, more when mythic (it enters untapped).
MDFC_LAND = 0.38
MYTHIC_MDFC_LAND = 0.74
MDFC_SOURCE = 0.8
MYTHIC_MDFC_SOURCE = 1.0
# Partial sources: each counts only for spells at or above its mana value.
DORK = 0.5
ROCK = 0.75
CHEAP_LAND_RAMP = 0.75
LAND_RAMP = 0.5
LATE_RAMP_FROM = 5
# A card that is no source at all.
NEVER = 99
OPPONENT_DEPENDENT = 0.75
# A Fabled Passage or Pathway in a deck of three or more colours, where it has to choose.
CHOOSING_SOURCE = 2 / 3
# A land that enters tapped only sometimes is about a quarter of a tap-land.
CONDITIONAL_TAPPED = 0.25
GOLD_EXTRA = 1
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

_TAPPED = ("enters tapped", "enters the battlefield tapped", "onto the battlefield tapped")
# Check lands, reveal lands, Fabled Passage: tapped only when a condition fails.
_SOMETIMES_TAPPED = ("unless", "if you control", "if you don't", "untap that land")
# Cavern of Souls and its kind: their coloured mana casts only some spells.
_RESTRICTED = "spend this mana only"
_PIP = re.compile(r"\{([^}]+)\}")
_BASIC_TYPES = {"plains": "W", "island": "U", "swamp": "B", "mountain": "R", "forest": "G"}
_CHEAP_DRAW = (
    "draw a card",
    "draw two cards",
    "draw three cards",
    "draws cards",
    "draws two cards",
    "draws three cards",
)
_ONE_MANA_CYCLING = re.compile(r"cycling \{[^}]+\}(?!\{)")


@dataclass(slots=True, frozen=True)
class LandCount:
    lands: float
    # Land/spell cards among them, counted as part of a land.
    land_spells: int
    average_mana_value: float
    cheap_draw: int
    cheap_ramp: int
    # Karsten's formula for this deck.
    suggested: float


@dataclass(slots=True, frozen=True)
class Comparison:
    """What other lists of the commander run: recent Archidekt decks or tournament top cuts."""

    label: str
    lists: int
    lands: float
    lands_low: float
    lands_high: float
    # Nonland cards that make mana: rocks, dorks, rituals, fast mana.
    mana_cards: float
    deck_mana_cards: int


@dataclass(slots=True, frozen=True)
class Need:
    card: str
    pattern: str
    needed: float
    have: float

    @property
    def short(self) -> bool:
        return self.have < self.needed


@dataclass(slots=True, frozen=True)
class ColourSources:
    colour: str
    # Lands and land/spell cards that make it, as Karsten counts them.
    lands: float
    # Every spell needing it, most demanding first; the shortfalls are the cards to look at.
    needs: tuple[Need, ...]

    @property
    def shortfalls(self) -> tuple[Need, ...]:
        return tuple(need for need in self.needs if need.short)


@dataclass(slots=True, frozen=True)
class ManaBase:
    lands: LandCount
    comparison: Comparison | None
    colours: tuple[ColourSources, ...]
    # One-drops needing a colour on turn one against the untapped lands that make it.
    turn_one: tuple[Need, ...]
    tapped_lands: float
    # Cards the colour check leaves out: cast for an alternative cost, or paid with hybrid or Phyrexian mana.
    skipped: tuple[str, ...]


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
    the_99 = _the_99(_entries(deck, cards))
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
    entries = _entries(deck, cards)
    basics = set(names.values())
    others = [
        _Source.of(info, quantity, len(colours))
        for quantity, info, commander in entries
        if not commander and info.name not in basics
    ]
    spells = [
        info for _, info, _ in entries if not info_is_land(info) and info.name.casefold() not in alternative
    ]
    found: dict[str, list[_Demand]] = {c: [] for c in colours}
    for info in spells:
        pips = _pips(info.mana_cost)
        gold = sum(1 for c in pips if c in COLOURS) > 1
        mana_value = max(int(info.cmc), 1)
        for colour in colours:
            if colour not in pips:
                continue
            table = needed_sources(mana_value, pips[colour])
            for untapped in (False, True) if mana_value == 1 else (False,):
                have = _have(others, colour, mana_value, untapped_only=untapped)
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
        source = _Source.of(option.mana, 1, deck_colours)
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
                source = _Source.of(option.mana, 1, len(colours))
                if source.tapped == 0 and all(source.colours.get(c, 0.0) >= 1 for c in makes):
                    yield option, None

        return found

    for _, info in the_99:
        if len(swaps) >= MAX_TAPPED_SWAPS:
            break
        text = info.oracle_text.casefold()
        if not info_is_land(info) or _is_basic(info) or _tapped(text) < 1 or not _plain(text):
            continue
        makes = {
            c for c, worth in _Source.of(info, 1, len(colours)).colours.items() if c in colours and worth > 0
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
    return info_is_land(info) and not _is_basic(info) and _RESTRICTED not in info.oracle_text.casefold()


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
    if info_is_land(info) or back_is_land(info.type_line) or _mana_card(info):
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


COLOUR_NAMES = {"W": "White", "U": "Blue", "B": "Black", "R": "Red", "G": "Green"}


def analyse_mana(
    deck: Deck,
    cards: Mapping[str, CardMana],
    *,
    alternative_cost: Iterable[str] = (),
    comparison: tuple[str, Sequence[Mapping[str, int]]] | None = None,
) -> ManaBase:
    """The deck's land count and colour sources against Karsten's numbers.

    cards holds every card in the deck (and in the compared lists) by casefolded name. alternative_cost
    names the cards Tagger tags as cast for an alternative cost, which Karsten leaves out of colour needs.
    comparison is a label and the lists to compare the land count with, each by name and count.
    """
    entries = _entries(deck, cards)
    the_99 = _the_99(entries)
    lands = land_count(the_99)
    identity = {c for _, info, _ in entries for c in _pips_colours(info.mana_cost)} | _identity(deck, cards)
    colours = sorted(identity, key=COLOURS.index)
    scale = lands.lands / TABLE_LANDS
    skip = {name.casefold() for name in alternative_cost}
    sources = [_Source.of(info, quantity, len(colours)) for quantity, info in the_99]
    checked = [info for _, info, _ in entries if not info_is_land(info) and info.name.casefold() not in skip]
    found = tuple(
        ColourSources(colour, _land_sources(sources, colour), _needs(checked, colour, sources, scale))
        for colour in colours
    )
    one_drops = [info for info in checked if info.cmc == 1]
    turn_one = tuple(
        need
        for colour in colours
        for need in _needs(one_drops, colour, sources, scale, untapped_only=True)
        if need.short
    )
    skipped = tuple(
        sorted(
            info.name
            for _, info, _ in entries
            if not info_is_land(info) and (info.name.casefold() in skip or _only_flexible_pips(info))
        )
    )
    tapped = sum(source.tapped * source.quantity for source in sources if source.land)
    return ManaBase(
        lands,
        _compare(comparison, cards, the_99),
        found,
        turn_one,
        round(tapped, 2),
        skipped,
    )


def land_count(cards: Sequence[tuple[int, CardMana]]) -> LandCount:
    """Karsten's land count for the 99: land/spell cards as part land, nonland cards' mana value."""
    lands = 0.0
    land_spells = 0
    spells: list[float] = []
    draw = ramp = 0
    for quantity, info in cards:
        if info_is_land(info):
            lands += quantity
            continue
        if back_is_land(info.type_line):
            land_spells += quantity
            lands += quantity * (MYTHIC_MDFC_LAND if info.rarity == "mythic" else MDFC_LAND)
        spells += [info.cmc] * quantity
        if cheap_draw(info):
            draw += quantity
        elif cheap_ramp(info):
            ramp += quantity
    average = sum(spells) / len(spells) if spells else 0.0
    suggested = LAND_BASE + LAND_PER_MANA_VALUE * average - LAND_PER_CHEAP_SPELL * (draw + ramp)
    return LandCount(round(lands, 2), land_spells, round(average, 2), draw, ramp, round(suggested, 1))


def cheap_draw(info: CardMana) -> bool:
    """Karsten's "cheap card draw spell", from the card's rules text as he defined it."""
    text = info.oracle_text.casefold()
    if info_is_land(info):
        return False
    # Any spell that cycles for one mana counts, whatever it costs to cast.
    if _ONE_MANA_CYCLING.search(text):
        return True
    if info.cmc > CHEAP:
        return False
    draws = any(phrase in text for phrase in _CHEAP_DRAW) and not any(
        word in text for word in ("{4}", "blood token", "investigate")
    )
    if draws and "Creature" in front_type(info.type_line):
        draws = "when" in text and "enters" in text
    looks = (
        "Creature" not in front_type(info.type_line)
        and all(word in text for word in ("look", "library", "put", "your hand"))
        and not any(word in text for word in ("pay ", "pays"))
    )
    return draws or looks


def cheap_ramp(info: CardMana) -> bool:
    """Karsten's "cheap mana ramp spell": a cheap card that adds mana or puts a land into play."""
    text = info.oracle_text.casefold()
    if info.cmc > CHEAP or info_is_land(info) or cheap_draw(info):
        return False
    adds = "add " in text and not any(word in text for word in ("add its ability", "add a lore counter"))
    if adds and "Creature" in front_type(info.type_line) and "dies" in text:
        adds = False
    fetches = "search" in text and "your library" in text and ("land" in text or "basic" in text)
    fetches = fetches and "sacrifice" not in text
    aura = "enchanted land is tapped" in text and "adds an additional" in text
    cheats = "put a creature card with" in text and "from your hand onto the battlefield" in text
    return adds or fetches or aura or cheats


def needed_sources(mana_value: int, pips: int) -> int:
    """Karsten's 99-card number for a spell; beyond his table, the nearest entry for that many pips."""
    pips = min(pips, 4)
    mana_value = max(mana_value, pips)
    known = [m for m, n in SOURCES_NEEDED if n == pips]
    return SOURCES_NEEDED[(min(max(mana_value, min(known)), max(known)), pips)]


def info_is_land(info: CardMana) -> bool:
    return "Land" in front_type(info.type_line).split()


@dataclass(slots=True, frozen=True)
class _Source:
    """One card's worth to each colour, and the least mana value a spell needs before it counts."""

    name: str
    quantity: int
    land: bool
    colours: Mapping[str, float]
    # Spells below this mana value can't use it (a dork helps from two mana up).
    from_mana_value: int
    tapped: float = 0.0

    @staticmethod
    def of(info: CardMana, quantity: int, deck_colours: int) -> "_Source":
        text = info.oracle_text.casefold()
        made = set(info.produced)
        if info_is_land(info):
            colours = _land_colours(info, text, made, deck_colours)
            return _Source(
                info.name, quantity, land=True, colours=colours, from_mana_value=1, tapped=_tapped(text)
            )
        if back_is_land(info.type_line):
            worth = MYTHIC_MDFC_SOURCE if info.rarity == "mythic" else MDFC_SOURCE
            back = info.oracle_text.split(FACE_SEPARATOR)[-1].casefold()
            colours = dict.fromkeys(made or _searched(back), worth)
            return _Source(
                info.name, quantity, land=True, colours=colours, from_mana_value=1, tapped=_tapped(back)
            )
        mana_value = int(info.cmc)
        front = front_type(info.type_line)
        permanent = any(word in front for word in ("Artifact", "Creature", "Enchantment"))
        if made and permanent and "{t}" in text and "sacrifice" not in text:
            worth = DORK if "Creature" in front else ROCK
            if "opponent" in text:
                worth = min(worth, OPPONENT_DEPENDENT)
            colours = dict.fromkeys(made, worth)
            return _Source(info.name, quantity, land=False, colours=colours, from_mana_value=mana_value + 1)
        searched: set[str] = _searched(text) if "search your library" in text and "land" in text else set()
        if searched and not permanent:
            worth = CHEAP_LAND_RAMP if mana_value <= CHEAP else LAND_RAMP
            # Karsten: two-mana ramp counts from three-mana spells, three-mana ramp from five.
            start = mana_value + 1 if mana_value <= CHEAP else max(mana_value + 2, LATE_RAMP_FROM)
            colours = dict.fromkeys(searched, worth)
            return _Source(info.name, quantity, land=False, colours=colours, from_mana_value=start)
        return _Source(info.name, quantity, land=False, colours={}, from_mana_value=NEVER)


def _entries(deck: Deck, cards: Mapping[str, CardMana]) -> list[tuple[int, CardMana, bool]]:
    """The deck's main cards the card data knows: copies, the card, and whether it is a commander."""
    commander_names = {name.casefold() for name in deck.commanders}
    return [
        (card.quantity, info, card.name.casefold() in commander_names)
        for card in deck.main_cards
        if (info := cards.get(card.name.casefold())) is not None
    ]


def _the_99(entries: Sequence[tuple[int, CardMana, bool]]) -> list[tuple[int, CardMana]]:
    return [(quantity, info) for quantity, info, commander in entries if not commander]


def _land_colours(info: CardMana, text: str, made: set[str], deck_colours: int) -> dict[str, float]:
    if "opponent" in text and "could produce" in text:
        return dict.fromkeys("WUBRG", OPPONENT_DEPENDENT)
    if made:
        # A Pathway makes one of its two colours, chosen as it is played.
        choosing = " // " in info.type_line and deck_colours >= 3  # noqa: PLR2004 — three colours or more
        return dict.fromkeys(made, CHOOSING_SOURCE if choosing else 1.0)
    searched = _searched(text)
    if "basic land card" in text and not any(t in text for t in _BASIC_TYPES):
        worth = CHOOSING_SOURCE if deck_colours >= 3 else 1.0  # noqa: PLR2004 — as above
        return dict.fromkeys("WUBRG", worth)
    return dict.fromkeys(searched, 1.0)


def _searched(text: str) -> set[str]:
    """The colours a land search can find, by the basic land types it names; any basic finds every colour."""
    found = {colour for land_type, colour in _BASIC_TYPES.items() if land_type in text}
    if not found and "basic land" in text:
        return set(COLOURS)
    return found


def _tapped(text: str) -> float:
    # Evolving Wilds and its kind put the land they find onto the battlefield tapped.
    if not any(phrase in text for phrase in _TAPPED):
        return 0.0
    # A shock land enters untapped whenever its owner pays the life.
    if "you may pay" in text:
        return 0.0
    return CONDITIONAL_TAPPED if any(phrase in text for phrase in _SOMETIMES_TAPPED) else 1.0


def _needs(
    spells: Sequence[CardMana],
    colour: str,
    sources: Sequence[_Source],
    scale: float,
    *,
    untapped_only: bool = False,
) -> tuple[Need, ...]:
    found: list[Need] = []
    for info in spells:
        pips = _pips(info.mana_cost)
        if colour not in pips:
            continue
        mana_value = max(int(info.cmc), 1)
        gold = sum(1 for c in pips if c in COLOURS) > 1
        needed = needed_sources(mana_value, pips[colour]) * scale + (GOLD_EXTRA if gold else 0)
        have = _have(sources, colour, mana_value, untapped_only=untapped_only)
        found.append(Need(info.name, _pattern(mana_value, pips[colour]), round(needed, 1), round(have, 1)))
    found.sort(key=lambda n: (n.have - n.needed, n.card))
    return tuple(found)


def _have(sources: Sequence[_Source], colour: str, mana_value: int, *, untapped_only: bool) -> float:
    """The sources of a colour that help a spell of this mana value; on turn one only untapped lands do."""
    return sum(
        s.colours.get(colour, 0.0) * s.quantity * (1 - s.tapped if untapped_only else 1)
        for s in sources
        if (s.land or not untapped_only) and mana_value >= s.from_mana_value
    )


def _land_sources(sources: Sequence[_Source], colour: str) -> float:
    return round(sum(s.colours.get(colour, 0.0) * s.quantity for s in sources if s.land), 1)


def _compare(
    comparison: tuple[str, Sequence[Mapping[str, int]]] | None,
    cards: Mapping[str, CardMana],
    the_99: Sequence[tuple[int, CardMana]],
) -> Comparison | None:
    if comparison is None or not comparison[1]:
        return None
    label, lists = comparison
    counts: list[float] = []
    mana: list[float] = []
    for listed in lists:
        known = [
            (quantity, info) for name, quantity in listed.items() if (info := cards.get(name.casefold()))
        ]
        counts.append(land_count(known).lands)
        mana.append(sum(q for q, info in known if _mana_card(info)))
    return Comparison(
        label,
        len(lists),
        round(median(counts), 1),
        min(counts),
        max(counts),
        round(median(mana), 1),
        sum(q for q, info in the_99 if _mana_card(info)),
    )


def _mana_card(info: CardMana) -> bool:
    return bool(info.produced) and not info_is_land(info) and not back_is_land(info.type_line)


def _pips(mana_cost: str) -> dict[str, int]:
    """Fixed coloured and colourless pips of the front face; hybrid, Phyrexian and X pips are left out."""
    pips: dict[str, int] = {}
    for symbol in _PIP.findall(front_face(mana_cost)):
        if symbol in COLOURS or symbol == "C":
            pips[symbol] = pips.get(symbol, 0) + 1
    return pips


def _only_flexible_pips(info: CardMana) -> bool:
    """Coloured mana in the cost, but all of it hybrid or Phyrexian, so no colour has a fixed pip to check."""
    symbols = _PIP.findall(front_face(info.mana_cost))
    return not _pips_colours(info.mana_cost) and any(set(symbol) & set(COLOURS) for symbol in symbols)


def _pips_colours(mana_cost: str) -> set[str]:
    return {c for c in _pips(mana_cost) if c in COLOURS}


def _identity(deck: Deck, cards: Mapping[str, CardMana]) -> set[str]:
    commanders = [cards.get(name.casefold()) for name in deck.commanders]
    return {c for info in commanders if info for c in _pips_colours(info.mana_cost)}


def _pattern(mana_value: int, pips: int) -> str:
    generic = mana_value - pips
    return (str(generic) if generic else "") + "C" * pips


# Tagger tags for cards usually cast for an alternative cost, which Karsten leaves out of colour needs.
ALTERNATIVE_COST_TAGS = frozenset({"alternative-cost", "manaless-value"})


def mana_json(report: ManaBase) -> dict[str, object]:
    """The report for the app's Mana tab."""
    lands = report.lands
    compared = report.comparison
    return {
        "lands": {
            "lands": lands.lands,
            "landSpells": lands.land_spells,
            "averageManaValue": lands.average_mana_value,
            "cheapDraw": lands.cheap_draw,
            "cheapRamp": lands.cheap_ramp,
            "suggested": lands.suggested,
        },
        "comparison": None
        if compared is None
        else {
            "label": compared.label,
            "lists": compared.lists,
            "lands": compared.lands,
            "low": compared.lands_low,
            "high": compared.lands_high,
            "manaCards": compared.mana_cards,
            "deckManaCards": compared.deck_mana_cards,
        },
        "colours": [
            {
                "colour": colour.colour,
                "lands": colour.lands,
                "needs": [_need_json(need) for need in colour.needs],
                "short": len(colour.shortfalls),
            }
            for colour in report.colours
        ],
        "turnOne": [_need_json(need) for need in report.turn_one],
        "tappedLands": report.tapped_lands,
        "skipped": list(report.skipped),
    }


def _need_json(need: Need) -> dict[str, object]:
    return {
        "card": need.card,
        "pattern": need.pattern,
        "needed": need.needed,
        "have": need.have,
        "short": need.short,
    }


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
