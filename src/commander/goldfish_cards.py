"""A deck compiled once into small frozen records for the goldfish game loop (ADR 0054).

Each card's rules text is read here, once, so no text is parsed during play. Lands, costs and two-part cards
come from `manabase`; Tagger roles and tags say what a card is for, and the text says how much and where.
A card the reading cannot place stays a plain spell, flagged `modelled=False` and named in `not_modelled`.
"""

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum

from commander.answers import card_key
from commander.carddb import COLORS, FACE_SEPARATOR, CardInfo, CardMana, Role, back_is_land
from commander.manabase import (
    ALTERNATIVE_COST_TAGS,
    PIP,
    REMINDER_TEXT,
    VARIABLE_MANA,
    DeckLands,
    castings,
    cost_halves,
    cost_value,
    deck_entries,
    info_is_land,
    land_colours,
    land_count,
    mostly_untapped,
    tapped_share,
    without_commanders,
)
from commander.models import Deck
from commander.spellbook import ComboDetail

# Tagger tags of cards that end the game by themselves, before the owner's additions and removals.
FINISHER_TAGS = frozenset({"overrun", "alternate-win-condition", "theft-mass"})
# Combo pieces start on the battlefield, in hand, in the command zone or in the library; nothing here fills
# the graveyard or exile.
_REACHABLE_ZONES = frozenset("BHCL")
_COLOURLESS = "C"
_ALL_COLOURS = frozenset(COLORS)


class CardKind(Enum):
    LAND = "land"
    CREATURE = "creature"
    ARTIFACT = "artifact"
    ENCHANTMENT = "enchantment"
    INSTANT = "instant"
    SORCERY = "sorcery"
    PLANESWALKER = "planeswalker"


class TutorDestination(Enum):
    HAND = "hand"
    BATTLEFIELD = "battlefield"
    # Drawn on the next turn.
    TOP = "top of the library"


class LandDestination(Enum):
    TAPPED = "battlefield, tapped"
    UNTAPPED = "battlefield, untapped"
    HAND = "hand"


@dataclass(slots=True, frozen=True)
class LandFace:
    """The land a card is played as. Its colours are the ones one tap can pay with (a dual's two, a fetch
    land's searchable ones), limited to the commander's colour identity. A land that enters tapped only
    sometimes (a check land, Fabled Passage) is tapped when that share is over `manabase.MOSTLY_UNTAPPED`."""

    colours: frozenset[str]
    tapped: bool
    # Mana per tap, net of any mana paid for it (Ancient Tomb: 2).
    amount: int = 1


@dataclass(slots=True, frozen=True)
class ManaAbility:
    """A mana source other than a land. `amount` is the net mana per use after mana paid in the ability's own
    cost (a Signet makes 2 for 1: 1). Each mana may be any of `colours`. A spell's `amount` is its gross mana,
    its own cost still to be taken off."""

    amount: int
    colours: frozenset[str]
    # A creature's mana comes the turn after it is cast (summoning sickness).
    delayed: bool
    # One shot: an instant or sorcery, or a permanent that sacrifices itself (Lotus Petal) or never untaps.
    ritual: bool


@dataclass(slots=True, frozen=True)
class LandRamp:
    """A card that searches lands out of the library. `count` lands in all: `to_hand` of them go to the hand
    and the rest to the battlefield as `destination` says (Cultivate: count 2, one in hand, the other
    tapped). A search that only fetches to the hand has destination HAND and `to_hand == count`."""

    count: int
    # Basic land cards only, against a Forest card or any land card.
    basic_only: bool
    destination: LandDestination
    to_hand: int = 0
    # Mana to pay after casting a permanent that sacrifices itself to search (Wayfarer's Bauble).
    activation: int = 0


@dataclass(slots=True, frozen=True)
class Tutor:
    """A search for a non-land card. `finds` is the card types it accepts; empty means any card."""

    finds: frozenset[CardKind]
    destination: TutorDestination
    # The most a found card may cost: a number, or X when `x_limited` (Green Sun's Zenith).
    max_mana_value: int | None = None
    x_limited: bool = False


@dataclass(slots=True, frozen=True)
class Draw:
    """Cards drawn on casting; an `engine` is a permanent drawing again each turn."""

    count: int
    engine: bool


@dataclass(slots=True, frozen=True)
class GoldfishCard:
    """One card of the deck as the game loop reads it. The cost is the one it is cast for: a two-part card by
    the rule of ADR 0019 (for halves of equal value the first, as in Fire // Ice), and a modal card with a
    land face by its spell half, its land face apart. A land has mana value 0."""

    name: str
    oracle_id: str
    copies: int
    commander: bool
    mana_value: int
    pips: Mapping[str, int]
    x_cost: bool
    # The types of the half it is cast as, or of its land face.
    kinds: frozenset[CardKind]
    land: LandFace | None
    mana: ManaAbility | None
    land_ramp: LandRamp | None
    tutor: Tutor | None
    draw: Draw | None
    # A named piece of one of the deck's combos the game can assemble.
    combo: bool
    finisher: bool
    # Matches one of the deck's dominant tags.
    synergy: bool
    modelled: bool

    @property
    def permanent(self) -> bool:
        return CardKind.INSTANT not in self.kinds and CardKind.SORCERY not in self.kinds


@dataclass(slots=True, frozen=True)
class GoldfishCombo:
    id: str
    # The deck's cards it is made of, by the names the deck gives them.
    pieces: tuple[str, ...]
    # Pieces described instead of named ("Vehicle with Crew 4 or less"); the tab shows them beside the combo.
    requirements: tuple[str, ...]
    results: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class GoldfishDeck:
    cards: tuple[GoldfishCard, ...]
    commanders: tuple[GoldfishCard, ...]
    combos: tuple[GoldfishCombo, ...]
    # Combos the game cannot assemble: a piece must start in the graveyard or exile, or is not in the deck.
    left_out_combos: tuple[GoldfishCombo, ...]
    not_modelled: tuple[str, ...]
    # As the Mana tab counts it: the nonland cards of the 99.
    average_mana_value: float


@dataclass(slots=True, frozen=True)
class _Context:
    identity: frozenset[str]
    lands: DeckLands
    infos: Mapping[str, CardInfo]
    tags: Mapping[str, frozenset[str]]
    synergy: frozenset[str]
    finishers_added: frozenset[str]
    finishers_removed: frozenset[str]
    combo_pieces: frozenset[str]
    unknown_mana_value: int


def compile_deck(
    deck: Deck,
    cards: Mapping[str, CardMana],
    infos: Mapping[str, CardInfo],
    tags: Mapping[str, frozenset[str]],
    *,
    combos: Sequence[ComboDetail] = (),
    synergy: Collection[str] = (),
    finishers_added: Iterable[str] = (),
    finishers_removed: Iterable[str] = (),
) -> GoldfishDeck:
    """The deck's cards as records, from data already loaded.

    cards holds the deck's cards by casefolded name, infos their Tagger roles by oracle id (`CardDb.cards`),
    and tags each card's tag slugs with every tag above them (`CardDb.tag_lineage`). synergy is the oracle ids
    of the cards matching the deck's dominant tags (`ideas.search_tags`). The finisher names are added to and
    taken from the cards the Tagger tags make finishers. A card the card data lacks is a plain spell of the
    deck's average mana value, not modelled.
    """
    entries = deck_entries(deck, cards)
    average = land_count(without_commanders(entries)).average_mana_value
    kept, left_out = _split_combos(combos, deck)
    context = _Context(
        identity=_identity(deck, cards, infos),
        lands=DeckLands.of(without_commanders(entries)),
        infos=infos,
        tags=tags,
        synergy=frozenset(synergy),
        finishers_added=frozenset(card_key(name) for name in finishers_added),
        finishers_removed=frozenset(card_key(name) for name in finishers_removed),
        combo_pieces=frozenset(card_key(name) for combo in kept for name in combo.pieces),
        unknown_mana_value=max(round(average), 1),
    )
    commanders = {card_key(name) for name in deck.commanders}
    compiled: dict[str, GoldfishCard] = {}
    for card in deck.main_cards:
        key = card.name.casefold()
        info = cards.get(key)
        record = _compile_card(
            card.name, info, card.quantity, context, commander=card_key(card.name) in commanders
        )
        # Archidekt lists a card in two categories as two rows.
        compiled[key] = (
            replace(record, copies=compiled[key].copies + record.copies) if key in compiled else record
        )
    records = list(compiled.values())
    return GoldfishDeck(
        cards=tuple(r for r in records if not r.commander),
        commanders=tuple(r for r in records if r.commander),
        combos=tuple(kept),
        left_out_combos=tuple(left_out),
        not_modelled=tuple(sorted({r.name for r in records if not r.modelled})),
        average_mana_value=average,
    )


def _identity(deck: Deck, cards: Mapping[str, CardMana], infos: Mapping[str, CardInfo]) -> frozenset[str]:
    """The commanders' colour identity; empty when the card data does not know them."""
    found: set[str] = set()
    for name in deck.commanders:
        info = cards.get(name.casefold())
        if info is not None and info.oracle_id in infos:
            found.update(infos[info.oracle_id].color_identity)
    return frozenset(found & _ALL_COLOURS)


def _split_combos(
    combos: Sequence[ComboDetail], deck: Deck
) -> tuple[list[GoldfishCombo], list[GoldfishCombo]]:
    """The combos whose pieces are all in the deck and can start in play, apart from the rest."""
    names = {card_key(card.name): card.name for card in deck.main_cards}
    kept: list[GoldfishCombo] = []
    left_out: list[GoldfishCombo] = []
    for combo in combos:
        pieces = tuple(names.get(card_key(p.name), p.name) for p in combo.pieces)
        found = GoldfishCombo(combo.id, pieces, combo.requirements, combo.results)
        # A piece with no zones named may start anywhere.
        reachable = all(not p.zones or _REACHABLE_ZONES.intersection(p.zones) for p in combo.pieces)
        present = all(card_key(p.name) in names for p in combo.pieces)
        (kept if reachable and present else left_out).append(found)
    return kept, left_out


# --- one card ---------------------------------------------------------------------------------------------

_KIND_WORDS = {kind.value: kind for kind in CardKind}
# Wording that makes a card more than a plain spell with the cost printed on it: a cost it is cast for
# instead, a cost reduction, a cost to pay besides mana, or mana with strings attached.
_UNMODELLED = re.compile(
    r"costs? (?:\{[^}]+\})+ less|less to cast|rather than pay (?:this spell's|its) mana cost"
    r"|cast this spell without paying|as an additional cost to cast this \w+, (?!you may)"
    r"|\bimprint\b|if this \w+ would enter|^(?:affinity|convoke|delve|improvise|emerge|undaunted)\b"
    r"|spend this mana only|return (?:an? )?(?:untapped )?\w+ you control to its owner's hand",
    re.MULTILINE,
)


def _compile_card(
    name: str, info: CardMana | None, copies: int, context: _Context, *, commander: bool
) -> GoldfishCard:
    if info is None:
        return _plain(
            name,
            "",
            context,
            copies,
            commander,
            mana_value=context.unknown_mana_value,
            pips={},
            kinds=frozenset(),
        )
    face = _cast_face(info)
    kinds = _kinds(info.type_line.split(" // ")[min(face, info.type_line.count(" // "))])
    cost: tuple[int, Mapping[str, int]] = (0, {}) if info_is_land(info) else _cost(info)
    card = _plain(
        name, info.oracle_id, context, copies, commander, mana_value=cost[0], pips=cost[1], kinds=kinds
    )
    card = replace(
        card, x_cost=any(symbol == "X" for symbol in PIP.findall(cost_halves(info.mana_cost)[face]))
    )
    if info_is_land(info):
        land, understood = _land_face(info, context)
        return replace(card, land=land, modelled=understood)
    card = _read_spell(card, info, _face_text(info, face), context)
    if back_is_land(info.type_line):
        land, understood = _land_face(info, context)
        return replace(card, land=land, modelled=card.modelled and understood)
    return card


def _plain(
    name: str,
    oracle_id: str,
    context: _Context,
    copies: int,
    commander: bool,  # noqa: FBT001 — filled by the one caller, beside copies
    *,
    mana_value: int,
    pips: Mapping[str, int],
    kinds: frozenset[CardKind],
) -> GoldfishCard:
    """The card as a spell with its cost and the flags the deck gives it, nothing else read from its text."""
    key = card_key(name)
    finisher = (
        bool(context.tags.get(oracle_id, frozenset()) & FINISHER_TAGS) or key in context.finishers_added
    )
    return GoldfishCard(
        name=name,
        oracle_id=oracle_id,
        copies=copies,
        commander=commander,
        mana_value=mana_value,
        pips=pips,
        x_cost=False,
        kinds=kinds,
        land=None,
        mana=None,
        land_ramp=None,
        tutor=None,
        draw=None,
        combo=key in context.combo_pieces,
        finisher=finisher and key not in context.finishers_removed,
        synergy=oracle_id in context.synergy,
        modelled=bool(oracle_id),
    )


def _cast_face(info: CardMana) -> int:
    """The index of the half a card is cast as, the one `manabase.castings` takes its cost from."""
    halves = cost_halves(info.mana_cost)
    if info.layout != "split" or len(halves) == 1:
        return 0
    cast = castings(info)[0].mana_value
    return next(i for i, half in enumerate(halves) if max(cost_value(half), 1) == cast)


def _cost(info: CardMana) -> tuple[int, Mapping[str, int]]:
    """The mana value and fixed pips of the cost a card is cast for."""
    cast = castings(info)[0]
    split = info.layout == "split" and len(cost_halves(info.mana_cost)) > 1
    return (cast.mana_value if split else int(info.cmc)), cast.pips


def _face_text(info: CardMana, face: int) -> str:
    """The casefolded rules text of the half a card is cast as, without reminder text."""
    faces = info.oracle_text.split(FACE_SEPARATOR)
    return REMINDER_TEXT.sub("", faces[min(face, len(faces) - 1)]).casefold()


def _kinds(type_line: str) -> frozenset[CardKind]:
    types = type_line.casefold().partition("—")[0].split()
    return frozenset(_KIND_WORDS[word] for word in types if word in _KIND_WORDS)


# --- mana -------------------------------------------------------------------------------------------------

# A mana ability or a spell's added mana, from the start of a line: an optional cost, then "add".
_MANA_ABILITY = re.compile(r"(?:(?P<cost>[^:]+): )?add (?P<what>[^.]*)\.")
_SYMBOL = re.compile(r"\{([wubrgc]|\d+)\}")
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
_ANY_COLOUR = re.compile(r"\bany (?:one )?(?:color|type)\b")
_AMOUNT_WORD = re.compile(r"^(" + "|".join(_NUMBER_WORDS) + r") (?:additional )?mana\b")
_COST_PART = re.compile(r"\{t\}|\{(?P<generic>\d+)\}|pay \d+ life|sacrifice this \w+")


@dataclass(slots=True, frozen=True)
class _Cost:
    """An ability's cost in the parts the game loop can pay."""

    mana: int
    taps: bool
    sacrifices: bool


def _paid(cost: str) -> _Cost | None:
    """What an ability costs; None when it asks for more than mana, a tap, life or its own sacrifice."""
    mana = 0
    taps = sacrifices = False
    for part in (p.strip() for p in cost.split(",") if p.strip()):
        matched = _COST_PART.fullmatch(part)
        if matched is None:
            return None
        mana += int(matched.group("generic") or 0)
        taps = taps or part == "{t}"
        sacrifices = sacrifices or part.startswith("sacrifice")
    return _Cost(mana, taps=taps, sacrifices=sacrifices)


def _abilities(text: str) -> list[tuple[str, str, bool]]:
    """Each line that adds mana: its cost, what it adds, and whether the amount varies."""
    found: list[tuple[str, str, bool]] = []
    for line in text.split("\n"):
        matched = _MANA_ABILITY.match(line)
        if matched is not None:
            found.append(
                (matched.group("cost") or "", matched.group("what"), VARIABLE_MANA.search(line) is not None)
            )
    return found


def _net_mana(cost: _Cost, what: str, identity: frozenset[str]) -> tuple[int, frozenset[str]] | None:
    """The net mana of one ability and the colours it can make; None when it nets nothing or is no fixed
    amount of mana."""
    if _ANY_COLOUR.search(what):
        said = _AMOUNT_WORD.match(what)
        made, colours = (_NUMBER_WORDS[said.group(1)] if said else 1), (identity or _ALL_COLOURS)
    else:
        made = 0
        found: set[str] = set()
        for option in what.split(" or "):
            symbols = _SYMBOL.findall(option)
            made = max(made, sum(int(s) if s.isdigit() else 1 for s in symbols))
            found.update(_COLOURLESS if s.isdigit() else s.upper() for s in symbols)
        colours = frozenset(found)
    net = made - cost.mana
    return (net, colours) if net > 0 else None


def _understood(text: str) -> bool:
    """Whether nothing in a land's or mana source's text is wording the reading cannot follow."""
    return _UNMODELLED.search(text) is None and not any(varies for _, _, varies in _abilities(text))


def _land_face(info: CardMana, context: _Context) -> tuple[LandFace, bool]:
    """The land a card is played as and whether its text is understood; a modal card whose back is a land is
    played as its back."""
    faces = info.oracle_text.split(FACE_SEPARATOR)
    text = REMINDER_TEXT.sub("", faces[0] if info_is_land(info) else faces[-1]).casefold()
    made = set(info.produced)
    colours = frozenset(land_colours(info, text, made, len(context.identity)))
    if context.identity:
        colours = colours & (context.identity | {_COLOURLESS}) or colours
    tapped = not mostly_untapped(tapped_share(text, context.lands))
    amounts = [
        found[0]
        for cost, what, varies in _abilities(text)
        if not varies
        and (paid := _paid(cost)) is not None
        and (found := _net_mana(paid, what, context.identity))
    ]
    return LandFace(colours, tapped, max(amounts, default=1)), _understood(text)


def _mana(text: str, kinds: frozenset[CardKind], identity: frozenset[str]) -> ManaAbility | None:
    """The best mana ability of a nonland card: the most mana, the colours of all its abilities."""
    spell = CardKind.INSTANT in kinds or CardKind.SORCERY in kinds
    best = 0
    colours: set[str] = set()
    delayed = ritual = False
    for cost, what, varies in _abilities(text):
        paid = _paid(cost)
        found = (
            None if varies or paid is None or not (spell or paid.taps) else _net_mana(paid, what, identity)
        )
        if paid is None or found is None:
            continue
        colours |= found[1]
        if found[0] > best:
            best = found[0]
            delayed = CardKind.CREATURE in kinds and paid.taps
            ritual = spell or paid.sacrifices or "doesn't untap" in text
    if best == 0:
        return None
    return ManaAbility(best, frozenset(colours), delayed=delayed, ritual=ritual)


# --- searches and draws -------------------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _Search:
    """The first library search in a card's text."""

    # The words before "card", which say what is searched for.
    head: str
    sentence: str
    # The cost of the ability that searches, "" for a spell or a trigger.
    cost: str
    # Happens again and again ("whenever ...", "at the beginning of ...").
    repeating: bool


_SEARCH = re.compile(r"search your library(?: and/or graveyard)? for (?P<what>[^.]*)\.")
_LAND_WORDS = frozenset({"land", "plains", "island", "swamp", "mountain", "forest"})
_COUNT_WORDS = {"two": 2, "three": 3, "four": 4}
_FILLER = frozenset(
    {"a", "an", "one", "two", "three", "four", "up", "to", "or", "and", "legendary", "nonland"}
    | {"white", "blue", "black", "red", "green", "colorless"}
)
_TUTOR_KINDS = {
    kind.value: kind
    for kind in (
        CardKind.CREATURE,
        CardKind.ARTIFACT,
        CardKind.ENCHANTMENT,
        CardKind.INSTANT,
        CardKind.SORCERY,
        CardKind.PLANESWALKER,
    )
}
_VALUE_LIMIT = re.compile(r"mana value (x|\d+) or less")
_TAPPED = re.compile(r"\btapped\b")
_DRAW = re.compile(r"\bdraws? (?:(?P<n>an?|one|two|three|four|five|six|seven|x) cards?|cards)")
_OPPONENT_DRAWS = re.compile(r"opponents? draws?")


def _search(text: str) -> _Search | None:
    for line in text.split("\n"):
        matched = _SEARCH.search(line)
        if matched is None:
            continue
        before = line[: matched.start()]
        cost = before.partition(":")[0] if ":" in before else ""
        head = re.split(r"\bcards?\b", matched.group("what"), maxsplit=1)[0]
        return _Search(head, matched.group(0), cost, line.startswith(("whenever", "at the beginning")))
    return None


def _land_ramp(search: _Search | None, kinds: frozenset[CardKind]) -> LandRamp | None:
    """A search for lands put onto the battlefield or into the hand, when the game can follow it."""
    if search is None or search.repeating:
        return None
    words = re.findall(r"[a-z]+", search.head)
    if not _LAND_WORDS.intersection(words) or not _LAND_WORDS.issuperset(set(words) - _FILLER - {"basic"}):
        return None
    cost = _paid(search.cost)
    # An ability must sacrifice its permanent for the lands, and a creature cannot tap the turn it comes.
    if cost is None or (search.cost and not cost.sacrifices) or (cost.taps and CardKind.CREATURE in kinds):
        return None
    battlefield = "onto the battlefield" in search.sentence
    count = next((n for word, n in _COUNT_WORDS.items() if word in words), 1)
    if "into your hand" in search.sentence:
        to_hand = count - 1 if battlefield else count
    elif battlefield:
        to_hand = 0
    else:
        return None
    if not battlefield:
        destination = LandDestination.HAND
    elif _TAPPED.search(search.sentence):
        destination = LandDestination.TAPPED
    else:
        destination = LandDestination.UNTAPPED
    return LandRamp(count, "basic" in words, destination, to_hand, cost.mana)


def _tutor(search: _Search | None) -> Tutor | None:
    """A search for a card of some type, when the game can follow where it goes."""
    if search is None or search.cost or search.repeating:
        return None
    words = [w for w in re.findall(r"[a-z]+", search.head) if w not in _FILLER]
    finds = frozenset(_TUTOR_KINDS[w] for w in words if w in _TUTOR_KINDS)
    destination = _tutor_destination(search.sentence)
    if len(finds) != len(words) or destination is None:
        return None
    limit = _VALUE_LIMIT.search(search.sentence)
    x_limited = limit is not None and limit.group(1) == "x"
    value = int(limit.group(1)) if limit is not None and not x_limited else None
    return Tutor(finds, destination, value, x_limited=x_limited)


def _tutor_destination(sentence: str) -> TutorDestination | None:
    if "on top" in sentence:
        return TutorDestination.TOP
    if "onto the battlefield" in sentence:
        return TutorDestination.BATTLEFIELD
    if "into your hand" in sentence:
        return TutorDestination.HAND
    return None


def _draw(text: str, kinds: frozenset[CardKind]) -> Draw | None:
    """Cards drawn by a spell, on a permanent's arrival, or each turn by a permanent that keeps drawing; None
    for a count that varies."""
    spell = CardKind.INSTANT in kinds or CardKind.SORCERY in kinds
    arrival: Draw | None = None
    for line in text.split("\n"):
        matched = _DRAW.search(line)
        if matched is None or _OPPONENT_DRAWS.search(line):
            continue
        number = matched.group("n")
        if number is None or number == "x":
            return None
        count = _NUMBER_WORDS[number]
        cost, activated, _ = line.partition(": ")
        if spell:
            return Draw(count, engine=False)
        if activated:
            # An ability that costs mana or a sacrifice each time is not a free draw engine.
            paid = _paid(cost)
            if paid is not None and not paid.mana and not paid.sacrifices:
                return Draw(count, engine=True)
        elif line.startswith(("whenever", "at the beginning")):
            return Draw(count, engine=True)
        elif "enters" in line:
            arrival = Draw(count, engine=False)
    return arrival


def _read_spell(card: GoldfishCard, info: CardMana, text: str, context: _Context) -> GoldfishCard:
    """What a nonland card does, from its half's text; a card the reading cannot place is not modelled."""
    if _UNMODELLED.search(text) or context.tags.get(info.oracle_id, frozenset()) & ALTERNATIVE_COST_TAGS:
        return replace(card, modelled=False)
    roles: frozenset[Role] = (
        context.infos[info.oracle_id].roles if info.oracle_id in context.infos else frozenset()
    )
    search = _search(text)
    ramp = _land_ramp(search, card.kinds)
    found = {
        "mana": _mana(text, card.kinds, context.identity),
        "land_ramp": ramp,
        "tutor": None if ramp is not None or Role.TUTOR not in roles else _tutor(search),
        "draw": _draw(text, card.kinds) if Role.DRAW in roles else None,
    }
    # The Tagger says what the card is for, so a role the reading finds nothing for is a card it cannot place.
    expected = (
        Role.RAMP in roles
        or Role.TUTOR in roles
        or (Role.DRAW in roles and re.search(r"\bdraws?\b", text) is not None)
    )
    modelled = not expected or any(value is not None for value in found.values())
    return replace(card, modelled=modelled, **found)
