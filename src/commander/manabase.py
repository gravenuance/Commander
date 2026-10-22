"""A deck's mana base against Frank Karsten's published maths (ADR 0019).

Land count: his 99-card regression, 31.42 + 3.13 x average mana value - 0.28 x cheap draw and ramp
("How Many Lands Do You Need in Your Deck? An Updated Analysis", TCGplayer, 2022). Colour sources: his
99-card table of sources needed to cast a spell on curve ("How Many Sources Do You Need to Consistently
Cast Your Spells? A 2022 Update", TCGplayer, 2022), with his rules for gold cards, partial sources and
other land counts. Both assume casual Commander; cEDH decks are compared with tournament lists instead.
"""

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from statistics import median

from commander.carddb import (
    COLORS,
    FACE_SEPARATOR,
    CardMana,
    back_is_land,
    front_face,
    front_type,
    has_nonland_face,
)
from commander.models import Deck

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
# A land that makes what an opponent's land could (Exotic Orchard), and the rock that does (Fellwar Stone).
OPPONENT_LAND = 0.75
OPPONENT_ROCK = 0.5
# A Fabled Passage or Pathway in a deck of three or more colours, where it has to choose.
CHOOSING_SOURCE = 2 / 3
# A land that enters tapped only sometimes, on a condition that is no basic land type (a fast land), is
# about a quarter of a tap-land.
CONDITIONAL_TAPPED = 0.25
# A land that enters tapped this seldom counts as untapped when planning; fast lands just do.
MOSTLY_UNTAPPED = CONDITIONAL_TAPPED
# The cards seen by turn three: the opening seven and two draws.
CARDS_SEEN = 9
GOLD_EXTRA = 1
_TAPPED = ("enters tapped", "enters the battlefield tapped", "onto the battlefield tapped")
# Check lands, reveal lands, Fabled Passage: tapped only when a condition fails.
_SOMETIMES_TAPPED = ("unless", "if you control", "if you don't", "untap that land")
# Check lands and reveal lands: untapped given a land of the basic types they name.
_TYPED_CONDITION = ("unless you control a", "you may reveal a")
_TYPE_WORD = re.compile(r"\b(plains|island|swamp|mountain|forest)\b")
# What a land that does nothing but make mana may say, a line at a time (see `mana_only_land`). A mana
# ability: a tap cost and perhaps others (a life payment, a filter land's mana), then "Add", and a painland's
# damage to its controller.
_COST = r"(?:\{[^}]+\}|pay \d+ life)"
_MANA_ABILITY = re.compile(
    rf"{_COST}(?:, {_COST})*: add [^.]+\.(?: this (?:land|permanent) deals \d+ damage to you\.)?"
)
# Enters tapped, unconditionally or unless a condition holds (check, fast, slow and similar lands).
_ENTERS_TAPPED = re.compile(r"this (?:land|permanent) enters (?:the battlefield )?tapped(?: unless [^.]+)?\.")
# Reveal lands and shock lands: a choice as the land enters, tapped when it is not made.
_ENTERS_BY_CHOICE = re.compile(
    r"as this (?:land|permanent) enters, you may (?:reveal [^.]+|pay \d+ life)\."
    r" if you don't, (?:it|this land) enters (?:the battlefield )?tapped\."
)
REMINDER_TEXT = re.compile(r"\([^)]*\)")
# A mana ability whose amount varies or is chosen ("for each", "equal to", X): more than one mana.
VARIABLE_MANA = re.compile(r"for each|equal to|amount of mana|that much|twice|additional mana|\bx mana|\{x\}")
# Mana symbols side by side after "add" ({C}{C}, {R}{G}): two or more mana from one ability.
_SEVERAL_PIPS_ADDED = re.compile(r"add [^.]*\{[^}]+\}\{[^}]+\}")
# A cost of mana beyond the tap: a filter land then turns one mana into two, a net gain of one.
_MANA_PAID = re.compile(r"\{(?!t\})[^}]+\}")
# Cavern of Souls and its kind: their coloured mana casts only some spells.
RESTRICTED = "spend this mana only"
PIP = re.compile(r"\{([^}]+)\}")
_HALF_SEPARATOR = " // "
# An aftermath card has layout "split" like Fire // Ice; only the Aftermath keyword line of its text tells.
_AFTERMATH = re.compile(r"^aftermath\b", re.IGNORECASE | re.MULTILINE)
BASIC_TYPES = {"plains": "W", "island": "U", "swamp": "B", "mountain": "R", "forest": "G"}
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
class Casting:
    """One way to cast a card: the mana value it is cast at and its fixed coloured pips (`fixed_pips`)."""

    mana_value: int
    pips: Mapping[str, int]


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
    entries = deck_entries(deck, cards)
    the_99 = without_commanders(entries)
    lands = land_count(the_99)
    identity = {c for _, info, _ in entries for c in _pips_colours(info.mana_cost)} | _identity(deck, cards)
    colours = sorted(identity, key=COLORS.index)
    scale = lands.lands / TABLE_LANDS
    skip = {name.casefold() for name in alternative_cost}
    deck_lands = DeckLands.of(the_99)
    sources = [Source.of(info, quantity, len(colours), deck_lands) for quantity, info in the_99]
    checked = [info for _, info, _ in entries if not info_is_land(info) and info.name.casefold() not in skip]
    found = tuple(
        ColourSources(colour, _land_sources(sources, colour), _needs(checked, colour, sources, scale))
        for colour in colours
    )
    turn_one = tuple(
        need
        for colour in colours
        for need in _needs(checked, colour, sources, scale, untapped_only=True)
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
class DeckLands:
    """The 99's size and its cards with basic land types, which decide when a check land enters untapped."""

    cards: int
    # Each set of basic land types that cards have, with the copies in the deck.
    typed: Mapping[frozenset[str], int]

    @staticmethod
    def of(the_99: Sequence[tuple[int, CardMana]]) -> "DeckLands":
        typed: dict[frozenset[str], int] = {}
        for quantity, info in the_99:
            # In hand or on the battlefield a two-faced card has only its front face.
            types = frozenset(_TYPE_WORD.findall(front_type(info.type_line).casefold().partition("—")[2]))
            if types:
                typed[types] = typed.get(types, 0) + quantity
        return DeckLands(sum(quantity for quantity, _ in the_99), typed)

    def missed(self, types: frozenset[str]) -> float:
        """The chance that none of the cards seen by turn three has one of these types, from the 99 less the
        land itself: C(N - K, n) / C(N, n), the hypergeometric chance of drawing none of K among N cards."""
        others = max(self.cards - 1, 0)
        wanted = min(sum(n for found, n in self.typed.items() if found & types), others)
        seen = min(CARDS_SEEN, others)
        return math.comb(others - wanted, seen) / math.comb(others, seen)


@dataclass(slots=True, frozen=True)
class Source:
    """One card's worth to each colour, and the least mana value a spell needs before it counts."""

    name: str
    quantity: int
    land: bool
    colours: Mapping[str, float]
    # Spells below this mana value can't use it (a dork helps from two mana up).
    from_mana_value: int
    tapped: float = 0.0

    @staticmethod
    def of(info: CardMana, quantity: int, deck_colours: int, lands: DeckLands) -> "Source":
        text = info.oracle_text.casefold()
        made = set(info.produced)
        if info_is_land(info):
            colours = land_colours(info, text, made, deck_colours)
            tapped = tapped_share(text, lands)
            return Source(info.name, quantity, land=True, colours=colours, from_mana_value=1, tapped=tapped)
        if back_is_land(info.type_line):
            worth = MYTHIC_MDFC_SOURCE if info.rarity == "mythic" else MDFC_SOURCE
            back = info.oracle_text.split(FACE_SEPARATOR)[-1].casefold()
            colours = dict.fromkeys(made or searched_colours(back), worth)
            tapped = tapped_share(back, lands)
            return Source(info.name, quantity, land=True, colours=colours, from_mana_value=1, tapped=tapped)
        mana_value = int(info.cmc)
        front = front_type(info.type_line)
        permanent = any(word in front for word in ("Artifact", "Creature", "Enchantment"))
        if made and permanent and "{t}" in text and "sacrifice" not in text:
            worth = DORK if "Creature" in front else ROCK
            if "opponent" in text:
                worth = min(worth, OPPONENT_ROCK)
            colours = dict.fromkeys(made, worth)
            return Source(info.name, quantity, land=False, colours=colours, from_mana_value=mana_value + 1)
        searching = "search your library" in text and "land" in text
        searched: set[str] = searched_colours(text) if searching else set()
        if searched and not permanent:
            worth = CHEAP_LAND_RAMP if mana_value <= CHEAP else LAND_RAMP
            # Karsten: two-mana ramp counts from three-mana spells, three-mana ramp from five.
            start = mana_value + 1 if mana_value <= CHEAP else max(mana_value + 2, LATE_RAMP_FROM)
            colours = dict.fromkeys(searched, worth)
            return Source(info.name, quantity, land=False, colours=colours, from_mana_value=start)
        return Source(info.name, quantity, land=False, colours={}, from_mana_value=NEVER)


def deck_entries(deck: Deck, cards: Mapping[str, CardMana]) -> list[tuple[int, CardMana, bool]]:
    """The deck's main cards the card data knows: copies, the card, and whether it is a commander."""
    commander_names = {name.casefold() for name in deck.commanders}
    return [
        (card.quantity, info, card.name.casefold() in commander_names)
        for card in deck.main_cards
        if (info := cards.get(card.name.casefold())) is not None
    ]


def without_commanders(entries: Sequence[tuple[int, CardMana, bool]]) -> list[tuple[int, CardMana]]:
    return [(quantity, info) for quantity, info, commander in entries if not commander]


def land_colours(info: CardMana, text: str, made: set[str], deck_colours: int) -> dict[str, float]:
    if "opponent" in text and "could produce" in text:
        return dict.fromkeys("WUBRG", OPPONENT_LAND)
    if made:
        # A Pathway makes one of its two colours, chosen as it is played.
        choosing = " // " in info.type_line and deck_colours >= 3  # noqa: PLR2004 — three colours or more
        return dict.fromkeys(made, CHOOSING_SOURCE if choosing else 1.0)
    searched = searched_colours(text)
    if "basic land card" in text and not any(t in text for t in BASIC_TYPES):
        worth = CHOOSING_SOURCE if deck_colours >= 3 else 1.0  # noqa: PLR2004 — as above
        return dict.fromkeys("WUBRG", worth)
    return dict.fromkeys(searched, 1.0)


def searched_colours(text: str) -> set[str]:
    """The colours a land search can find, by the basic land types it names; any basic finds every colour."""
    found = {colour for land_type, colour in BASIC_TYPES.items() if land_type in text}
    if not found and "basic land" in text:
        return set(COLORS)
    return found


def tapped_share(text: str, lands: DeckLands) -> float:
    # Evolving Wilds and its kind put the land they find onto the battlefield tapped.
    if not any(phrase in text for phrase in _TAPPED):
        return 0.0
    # A shock land enters untapped whenever its owner pays the life.
    if "you may pay" in text:
        return 0.0
    if not any(phrase in text for phrase in _SOMETIMES_TAPPED):
        return 1.0
    types = frozenset(_TYPE_WORD.findall(text))
    if types and any(phrase in text for phrase in _TYPED_CONDITION):
        return lands.missed(types)
    return CONDITIONAL_TAPPED


def mana_only_land(info: CardMana) -> bool:
    """Whether the land's front face does nothing but make mana and perhaps enter tapped: every line of its
    text, reminder text in parentheses left out, is a one-mana ability, an enters-tapped line or empty.

    A fetch land, a copy land, a man-land, Field of the Dead, a bounce land, a land with cycling or any
    other ability, and a land that can make more than one mana (Ancient Tomb, Gaea's Cradle, Cabal Coffers,
    Nykthos, Urza's Tower) are utility lands. A land with a spell half (an Adventure) is a
    utility land too, and one with no oracle text is not known, so not mana-only.
    """
    if not info.oracle_text.strip() or has_nonland_face(info.type_line):
        return False
    front = info.oracle_text.split(FACE_SEPARATOR)[0].casefold()
    lines = (line.strip() for line in REMINDER_TEXT.sub("", front).splitlines())
    return all(
        not line
        or (
            any(p.fullmatch(line) for p in (_MANA_ABILITY, _ENTERS_TAPPED, _ENTERS_BY_CHOICE))
            and not _makes_several_mana(line)
        )
        for line in lines
    )


def _makes_several_mana(line: str) -> bool:
    """Whether the mana ability can make more than one mana, a filter land's two for one paid aside."""
    if VARIABLE_MANA.search(line):
        return True
    cost, _, effect = line.partition(":")
    return not _MANA_PAID.search(cost) and _SEVERAL_PIPS_ADDED.search(effect) is not None


def mostly_untapped(tapped: float) -> bool:
    """Whether a land with this tapped share counts as an untapped one in the mana plan."""
    return tapped <= MOSTLY_UNTAPPED


def _needs(
    spells: Sequence[CardMana],
    colour: str,
    sources: Sequence[Source],
    scale: float,
    *,
    untapped_only: bool = False,
) -> tuple[Need, ...]:
    """What each spell asks of a colour, its most demanding way of casting it; with untapped_only, the
    turn-one check of the one-mana ways."""
    found: list[Need] = []
    for info in spells:
        ways = [
            (casting, _sources_asked(casting, colour, scale))
            for casting in castings(info)
            if colour in casting.pips and (casting.mana_value == 1 or not untapped_only)
        ]
        if not ways:
            continue
        casting, needed = max(ways, key=lambda way: way[1])
        have = count_sources(sources, colour, casting.mana_value, untapped_only=untapped_only)
        pattern = _pattern(casting.mana_value, casting.pips[colour])
        found.append(Need(info.name, pattern, round(needed, 1), round(have, 1)))
    found.sort(key=lambda n: (n.have - n.needed, n.card))
    return tuple(found)


def _sources_asked(casting: Casting, colour: str, scale: float) -> float:
    gold = sum(1 for c in casting.pips if c in COLORS) > 1
    return needed_sources(casting.mana_value, casting.pips[colour]) * scale + (GOLD_EXTRA if gold else 0)


def count_sources(sources: Sequence[Source], colour: str, mana_value: int, *, untapped_only: bool) -> float:
    """The sources of a colour that help a spell of this mana value; on turn one only untapped lands do."""
    return sum(
        s.colours.get(colour, 0.0) * s.quantity * (1 - s.tapped if untapped_only else 1)
        for s in sources
        if (s.land or not untapped_only) and mana_value >= s.from_mana_value
    )


def _land_sources(sources: Sequence[Source], colour: str) -> float:
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
        mana.append(sum(q for q, info in known if is_mana_card(info)))
    return Comparison(
        label,
        len(lists),
        round(median(counts), 1),
        min(counts),
        max(counts),
        round(median(mana), 1),
        sum(q for q, info in the_99 if is_mana_card(info)),
    )


def is_mana_card(info: CardMana) -> bool:
    return bool(info.produced) and not info_is_land(info) and not back_is_land(info.type_line)


def fixed_pips(mana_cost: str) -> dict[str, int]:
    """Fixed coloured and colourless pips of the front face; hybrid, Phyrexian and X pips are left out."""
    pips: dict[str, int] = {}
    for symbol in PIP.findall(front_face(mana_cost)):
        if symbol in COLORS or symbol == "C":
            pips[symbol] = pips.get(symbol, 0) + 1
    return pips


def castings(info: CardMana) -> list[Casting]:
    """How a card is judged for colour. A split card (Fire // Ice, cost "{1}{R} // {1}{U}") by its most
    expensive half at that half's own mana value, since its printed mana value is the halves added up (rule
    202.3c); halves of equal value both count. An aftermath card (Cut // Ribbons), whose second half can only
    be cast from the graveyard, by its first half at that half's own value. Any other card, an adventure
    (Bonecrusher Giant // Stomp) included, by its front face's cost at its mana value."""
    halves = cost_halves(info.mana_cost)
    if info.layout != "split" or len(halves) == 1:
        return [Casting(max(int(info.cmc), 1), fixed_pips(info.mana_cost))]
    if _AFTERMATH.search(info.oracle_text):
        return [Casting(max(cost_value(halves[0]), 1), fixed_pips(halves[0]))]
    values = [max(cost_value(half), 1) for half in halves]
    highest = max(values)
    return [
        Casting(value, fixed_pips(half))
        for half, value in zip(halves, values, strict=True)
        if value == highest
    ]


def cost_halves(mana_cost: str) -> list[str]:
    halves = mana_cost.split(_HALF_SEPARATOR)
    return halves if len(halves) > 1 and all(halves) else [mana_cost]


def cost_value(mana_cost: str) -> int:
    """The mana value of a cost; X counts for nothing, a hybrid symbol for its larger side ({2/W} is 2)."""
    total = 0
    for symbol in PIP.findall(mana_cost):
        first = symbol.split("/")[0]
        if first.isdigit():
            total += int(first)
        elif first not in ("X", "Y", "Z"):
            total += 1
    return total


def _only_flexible_pips(info: CardMana) -> bool:
    """Coloured mana in the cost, but all of it hybrid or Phyrexian, so no colour has a fixed pip to check."""
    symbols = PIP.findall(info.mana_cost)
    return not _pips_colours(info.mana_cost) and any(set(symbol) & set(COLORS) for symbol in symbols)


def _pips_colours(mana_cost: str) -> set[str]:
    """The colours with a fixed pip in any half of the cost."""
    return {c for half in cost_halves(mana_cost) for c in fixed_pips(half) if c in COLORS}


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
