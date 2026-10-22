"""A deck compiled once into small frozen records for the goldfish game loop (ADR 0054).

Each card's rules text is read here, once, so no text is parsed during play. Lands, costs and two-part cards
come from `manabase`; Tagger roles and tags say what a card is for, and the text says how much and where.
A card the reading cannot place stays a plain spell, flagged `modelled=False` and named in `not_modelled`.
"""

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import NamedTuple

from commander.answers import card_key
from commander.carddb import COLORS, FACE_SEPARATOR, CardInfo, CardMana, Role, back_is_land, front_type
from commander.manabase import (
    ALTERNATIVE_COST_TAGS,
    BASIC_TYPES,
    PIP,
    REMINDER_TEXT,
    RESTRICTED,
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


class EntryCost(Enum):
    """A card from the hand that a permanent costs as it comes in, besides its mana."""

    NONLAND_CARD = "a nonland card exiled from the hand"
    LAND_CARD = "a land card discarded from the hand"


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
    # Sacrificed when another land is played (City of Traitors), after its mana that turn is made.
    lost_to_land_drop: bool = False


class ManaNeed(Enum):
    """What a permanent's mana ability needs on the battlefield besides itself."""

    # A creature to tap for it, summoning sick or not: Springleaf Drum, a Vehicle's crew.
    CREATURE = "a creature to tap"
    # Metalcraft: three or more artifacts, itself among them (Mox Opal).
    ARTIFACTS = "three artifacts"


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
    # Used from the hand by exiling the card, without casting it (Elvish Spirit Guide).
    from_hand: bool = False
    # An Aura that adds this mana whenever its land is tapped (Wild Growth): it needs a land in play and its
    # mana comes the turn it lands.
    on_land: bool = False
    # The kinds of spell the mana may be spent on (Geosurge: artifact and creature); empty for any.
    spend_on: frozenset[CardKind] = frozenset()
    needs: ManaNeed | None = None
    # Life paid for each use, with no tap, so as often as life allows (Treasonous Ogre: 3 for {R}).
    life: int = 0
    # The basic land types of a land sacrificed for each use (Orcish Lumberjack: a Forest).
    land_sacrificed: frozenset[str] = frozenset()
    # Discards the rest of the hand (Lion's Eye Diamond).
    discards_hand: bool = False
    # Adds the mana value of the creature sacrificed to cast it (Metamorphosis).
    plus_sacrificed: bool = False


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
    # The basic land types a found land must have (Nature's Lore: forest); empty for any land.
    land_types: frozenset[str] = frozenset()


@dataclass(slots=True, frozen=True)
class Tutor:
    """A search for a non-land card. `finds` is the card types it accepts; empty means any card."""

    finds: frozenset[CardKind]
    destination: TutorDestination
    # The most a found card may cost: a number, or X when `x_limited` (Green Sun's Zenith).
    max_mana_value: int | None = None
    x_limited: bool = False
    # The limit is the sacrificed creature's mana value plus this (Eldritch Evolution).
    value_over_sacrifice: int | None = None


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
    # False for a card the Tagger or the text says matters to the goldfish (mana, lands, draw, tutoring, a
    # combo piece or a finisher) and the reading could not follow. Any other card is a plain spell.
    modelled: bool
    # Extra land drops each turn while it is in play (Exploration 1, Azusa 2).
    extra_land_drops: int = 0
    # Casting it needs a creature to sacrifice (Diabolic Intent, Culling the Weak).
    needs_sacrifice: bool = False
    entry_cost: EntryCost | None = None
    # The basic land types of a land (Breeding Pool: forest and island), which a typed search matches.
    land_types: frozenset[str] = frozenset()

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
    # Pieces that need only the command zone (Spellbook zones C alone): a commander so named counts without
    # being cast. Any other commander piece must be on the battlefield or cast.
    command_zone: tuple[str, ...] = ()


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
    # The deck's lands with basic land types: the types each has and the colours it makes, for the lands that
    # fetch by type.
    typed_lands: tuple[tuple[frozenset[str], frozenset[str]], ...] = ()


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
    context, kept, left_out, average = _deck_context(
        deck,
        cards,
        infos,
        tags,
        combos=combos,
        synergy=synergy,
        finishers_added=finishers_added,
        finishers_removed=finishers_removed,
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


def _deck_context(
    deck: Deck,
    cards: Mapping[str, CardMana],
    infos: Mapping[str, CardInfo],
    tags: Mapping[str, frozenset[str]],
    *,
    combos: Sequence[ComboDetail],
    synergy: Collection[str],
    finishers_added: Iterable[str],
    finishers_removed: Iterable[str],
) -> tuple[_Context, list[GoldfishCombo], list[GoldfishCombo], float]:
    """What every card of the deck is read against, the deck's kept and left-out combos and its average mana
    value."""
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
    return replace(context, typed_lands=_typed_lands(deck, cards, context)), kept, left_out, average


def compile_card(
    name: str,
    deck: Deck,
    cards: Mapping[str, CardMana],
    infos: Mapping[str, CardInfo],
    tags: Mapping[str, frozenset[str]],
    *,
    copies: int = 1,
    combos: Sequence[ComboDetail] = (),
    synergy: Collection[str] = (),
    finishers_added: Iterable[str] = (),
    finishers_removed: Iterable[str] = (),
) -> GoldfishCard:
    """One card read as `compile_deck` reads the deck's own: its identity, the typed lands fetches find, the
    combos, synergy and finishers. For a card that is not in the deck, a swap's incoming card; `cards` and
    `infos` must hold it as well as the deck. A combo is the deck's own, so the card is no piece of one that
    names it. A card the card data lacks is a plain spell, not modelled."""
    context = _deck_context(
        deck,
        cards,
        infos,
        tags,
        combos=combos,
        synergy=synergy,
        finishers_added=finishers_added,
        finishers_removed=finishers_removed,
    )[0]
    return _compile_card(name, cards.get(name.casefold()), copies, context, commander=False)


def _identity(deck: Deck, cards: Mapping[str, CardMana], infos: Mapping[str, CardInfo]) -> frozenset[str]:
    """The commanders' colour identity; empty when the card data does not know them."""
    found: set[str] = set()
    for name in deck.commanders:
        info = cards.get(name.casefold())
        if info is not None and info.oracle_id in infos:
            found.update(infos[info.oracle_id].color_identity)
    return frozenset(found & _ALL_COLOURS)


def _land_types(info: CardMana) -> frozenset[str]:
    """The basic land types a land has, from its front face."""
    return frozenset(BASIC_TYPES).intersection(
        re.findall(r"[a-z]+", front_type(info.type_line).casefold().partition("—")[2])
    )


def _typed_lands(
    deck: Deck, cards: Mapping[str, CardMana], context: _Context
) -> tuple[tuple[frozenset[str], frozenset[str]], ...]:
    found: list[tuple[frozenset[str], frozenset[str]]] = []
    commanders = {card_key(name) for name in deck.commanders}
    for card in deck.main_cards:
        info = cards.get(card.name.casefold())
        if info is None or card_key(card.name) in commanders or not info_is_land(info):
            continue
        types = _land_types(info)
        if types:
            found.append((types, _land_face(info, context)[0].colours))
    return tuple(found)


def _split_combos(
    combos: Sequence[ComboDetail], deck: Deck
) -> tuple[list[GoldfishCombo], list[GoldfishCombo]]:
    """The combos whose pieces are all in the deck and can start in play, apart from the rest."""
    names = {card_key(card.name): card.name for card in deck.main_cards}
    kept: list[GoldfishCombo] = []
    left_out: list[GoldfishCombo] = []
    for combo in combos:
        pieces = tuple(names.get(card_key(p.name), p.name) for p in combo.pieces)
        command_zone = tuple(
            names.get(card_key(p.name), p.name) for p in combo.pieces if p.zones and set(p.zones) == {"C"}
        )
        found = GoldfishCombo(combo.id, pieces, combo.requirements, combo.results, command_zone)
        # A piece with no zones named may start anywhere.
        reachable = all(not p.zones or _REACHABLE_ZONES.intersection(p.zones) for p in combo.pieces)
        present = all(card_key(p.name) in names for p in combo.pieces)
        (kept if reachable and present else left_out).append(found)
    return kept, left_out


# --- one card ---------------------------------------------------------------------------------------------

_KIND_WORDS = {kind.value: kind for kind in CardKind}
# The additional cost of a creature to sacrifice, which the game meets from its creatures. A sacrifice of
# anything else (a land, an artifact) is a cost it does not model.
_SACRIFICE_CREATURE = (
    r"sacrifice (?:an?|one) (?:(?:\w+ )?creature|artifact or creature|creature or artifact)\."
)
_SACRIFICE_COST = re.compile(rf"as an additional cost to cast this \w+, {_SACRIFICE_CREATURE}")
# Wording that makes a card more than a plain spell with the cost printed on it: a cost it is cast for
# instead, or a cost to pay besides mana and a creature to sacrifice. Such a card is not modelled only when
# it matters to the goldfish.
_UNMODELLED = re.compile(
    r"rather than pay (?:this spell's|its) mana cost"
    r"|cast this spell without paying|^(?:affinity|convoke|delve|improvise|emerge|undaunted)\b"
    rf"|as an additional cost to cast this \w+, (?!you may|{_SACRIFICE_CREATURE})",
    re.MULTILINE,
)
# A cost reduction, for other spells (Herald's Horn) or this one (Ghalta): the card is never modelled.
_COST_REDUCER = re.compile(r"costs? (?:\{[^}]+\})+ less|less to cast")
# A land that returns a land to the hand on entering (Karoo).
_BOUNCE_LAND = re.compile(r"return (?:an? )?(?:untapped )?\w+ you control to its owner's hand")
# A land that costs other lands (Lotus Field, Glacial Chasm).
_LAND_SACRIFICE = re.compile(r"when this land enters, sacrifice (?:a|one|two|three) lands?\b")
# A land lost when another is played (City of Traitors).
_LOST_TO_LAND_DROP = re.compile(r"when you play another land, sacrifice this land")
# The cost of a filter land (Fetid Heath): one mana, of a colour or either of two.
_FILTER_COST = re.compile(r"\{[wubrg](?:/[wubrg])?\}")
# A land that is whatever land it copies (Vesuva): it makes mana, but no text says how much.
_COPY_LAND = re.compile(r"as a copy of any land")
_EXTRA_LAND_DROPS = re.compile(r"you may play (an|one|two|three) additional lands? on each of your turns")
# A card from the hand a permanent costs as it enters: Chrome Mox's imprint, Mox Diamond.
_ENTRY_COSTS = (
    (re.compile(r"you may exile an? (?:nonartifact, )?nonland card from your hand"), EntryCost.NONLAND_CARD),
    (re.compile(r"you may discard a land card instead"), EntryCost.LAND_CARD),
)
# An Aura that adds mana with its land's (Wild Growth, Utopia Sprawl, Fertile Ground).
_MANA_AURA = re.compile(
    r"whenever enchanted (?:land|forest) is tapped for mana, its controller adds an additional "
    r"(?P<what>[^.]*)\."
)
# Text that points the Tagger's ramp role at the opponent (Path to Exile).
_FOR_OPPONENT = re.compile(r"(?:its|that creature's|that permanent's) controller may search")


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
        return replace(card, land=land, land_types=_land_types(info), modelled=understood)
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
# "Add {R}, {G}, or {W}": one mana of a choice, so the options are split before their symbols are counted.
_MANA_OPTION_SEPARATOR = re.compile(r",\s*(?:or\s+)?|\s+or\s+")
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
_ANY_COLOUR = re.compile(
    r"\bany (?:one )?(?:color|type)\b|the chosen colou?r|any of the exiled card's colou?rs"
)
_NUMBER_NAMES = {n: word for word, n in _NUMBER_WORDS.items() if word not in {"a", "an"}}
_AMOUNT_WORDS = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r") (?:additional )?mana\b")
# A count, or a colour or use the game cannot tell: Mox Amber's colours need a legendary creature, and a
# permanent that must be a creature with no crew to make it one.
_NOT_FIXED_MANA = re.compile(
    rf"{VARIABLE_MANA.pattern}|among legendary|activate only if (?:this \w+|it) is a creature"
)
_CREWED = re.compile(r"activate only if this \w+ is a creature\.")
_CREW = re.compile(r"^crew \d+", re.MULTILINE)
_METALCRAFT = re.compile(r"activate only if you control three or more artifacts\.")
_COST_PART = re.compile(
    r"\{t\}|\{(?P<generic>\d+)\}|pay (?P<life>\d+) life|sacrifice this \w+|exile this card from your hand"
    r"|(?P<creature>tap an untapped creature you control)"
    rf"|sacrifice an? (?P<land>{'|'.join(BASIC_TYPES)})|(?P<discard>discard your hand)"
)
# Metamorphosis: "Add X mana of any one color, where X is 1 plus the sacrificed creature's mana value".
_PLUS_SACRIFICED = re.compile(
    r"x mana of any one colou?r, where x is (?P<n>\d) plus the sacrificed creature's mana value"
)
# "Three mana in any combination of {R} and/or {G}".
_COMBINATION = re.compile(r"(?P<n>\w+) mana in any combination of (?P<symbols>.*)")
# A mode of a modal spell ("• Add {R} for each ..."), read like a line of its own.
_MODE_BULLET = re.compile(r"^• ")
# Mana that grows with a count after a fixed part (Rite of Flame): the fixed part is what is sure.
_FIXED_THEN_MORE = re.compile(r"((?:\{[wubrgc\d]\})+), then add .*")
# Mana for each card in an opponent's hand (Jeska's Will), read against a hand of OPPONENT_HAND cards.
_PER_OPPONENT_CARD = re.compile(r"(\{[wubrgc]\}) for each card in target opponent's hand")
OPPONENT_HAND = 7
_SPEND_ON = re.compile(r"spend this mana only to cast ([a-z, ]+?) spells\b")
# Treasure a spell makes, spent at once as mana of any colour; a tapped one waits a turn and is left out.
_TREASURES = re.compile(r"\bcreate (?P<n>a|an|one|two|three|four|five) treasure tokens?\b")


@dataclass(slots=True, frozen=True)
class _Cost:
    """An ability's cost in the parts the game loop can pay."""

    mana: int
    taps: bool
    # Its own sacrifice.
    sacrifices: bool
    exiles_from_hand: bool = False
    life: int = 0
    taps_creature: bool = False
    # The basic land type of a land it sacrifices.
    land: str = ""
    discards_hand: bool = False


def _paid(cost: str) -> _Cost | None:
    """What an ability costs; None when it asks for more than mana, a tap, life, its own sacrifice, a creature
    to tap, a land of a basic type to sacrifice or the hand to discard."""
    parts = [p.strip() for p in cost.split(",") if p.strip()]
    found = [_COST_PART.fullmatch(part) for part in parts]
    matched = [m for m in found if m is not None]
    if len(matched) != len(found):
        return None
    return _Cost(
        sum(int(m.group("generic") or 0) for m in matched),
        taps="{t}" in parts,
        sacrifices=any(part.startswith("sacrifice this") for part in parts),
        exiles_from_hand=any(part.startswith("exile") for part in parts),
        life=sum(int(m.group("life") or 0) for m in matched),
        taps_creature=any(m.group("creature") for m in matched),
        land=next((m.group("land") for m in matched if m.group("land")), ""),
        discards_hand=any(m.group("discard") for m in matched),
    )


class _ManaLine(NamedTuple):
    """A line that adds mana: its cost, what it adds, whether that is no fixed amount of any colour (a count,
    or a colour or use the game cannot tell), the kinds of spell alone it may pay for (empty for any) and what
    it needs on the battlefield."""

    cost: str
    what: str
    varies: bool
    spend_on: frozenset[CardKind]
    needs: ManaNeed | None = None


def _abilities(text: str) -> list[_ManaLine]:
    """Each line that adds mana, after a mode's bullet or an ability word ("Metalcraft — "). A line whose
    mana can be spent only on something other than spells of some card types is left out, which undercounts
    rather than overcounts."""
    found: list[_ManaLine] = []
    crewed = _CREW.search(text) is not None
    for whole in (_MODE_BULLET.sub("", line) for line in text.split("\n")):
        line = _ABILITY_WORD.sub("", whole, count=1)
        matched = _MANA_ABILITY.match(line)
        # Behind an ability word only an activated ability is read: "Threshold — Add ... instead" is not more.
        if matched is None or (line != whole and matched.group("cost") is None):
            continue
        spend_on = frozenset[CardKind]()
        if RESTRICTED in line:
            spend_on = _spend_on(line)
            if not spend_on:
                continue
        cost, what = matched.group("cost") or "", _fixed_mana(matched.group("what"))
        after = line[matched.end() :]
        needs = None
        if _METALCRAFT.search(after):
            needs, after = ManaNeed.ARTIFACTS, _METALCRAFT.sub("", after)
        elif crewed and _CREWED.search(after):
            needs, after = ManaNeed.CREATURE, _CREWED.sub("", after)
        said = f"{cost}: add {what}.{after}"
        found.append(_ManaLine(cost, what, _NOT_FIXED_MANA.search(said) is not None, spend_on, needs))
    return found


def _spend_on(line: str) -> frozenset[CardKind]:
    """The card types of the spells a line's mana is restricted to; empty for any other restriction."""
    matched = _SPEND_ON.search(line)
    if matched is None:
        return frozenset()
    words = [w for w in re.findall(r"[a-z]+", matched.group(1)) if w != "or"]
    if not words or any(w not in _KIND_WORDS for w in words):
        return frozenset()
    return frozenset(_KIND_WORDS[w] for w in words)


def _fixed_mana(what: str) -> str:
    """What an ability adds, with a count the game can settle replaced by its value: the fixed part of mana
    that grows after it, a mana for each card of an opponent's hand of OPPONENT_HAND."""
    if fixed := _FIXED_THEN_MORE.fullmatch(what):
        return fixed.group(1)
    if each := _PER_OPPONENT_CARD.fullmatch(what):
        return each.group(1) * OPPONENT_HAND
    if plus := _PLUS_SACRIFICED.fullmatch(what):
        # The rest is the sacrificed creature's, which the game adds when it sacrifices one.
        return f"{_NUMBER_NAMES[int(plus.group('n'))]} mana of any one color"
    return what


def _net_mana(cost: _Cost, what: str, identity: frozenset[str]) -> tuple[int, frozenset[str]] | None:
    """The net mana of one ability and the colours it can make; None when it nets nothing or is no fixed
    amount of mana."""
    if _ANY_COLOUR.search(what):
        # "Two mana of any one color and two mana of any other color" is four.
        said = _AMOUNT_WORDS.findall(what)
        made, colours = (sum(_NUMBER_WORDS[n] for n in said) or 1), (identity or _ALL_COLOURS)
    elif (mixed := _COMBINATION.fullmatch(what)) and mixed.group("n") in _NUMBER_WORDS:
        made = _NUMBER_WORDS[mixed.group("n")]
        colours = frozenset(s.upper() for s in _SYMBOL.findall(mixed.group("symbols")) if not s.isdigit())
    else:
        made = 0
        found: set[str] = set()
        for option in _MANA_OPTION_SEPARATOR.split(what):
            symbols = _SYMBOL.findall(option)
            made = max(made, sum(int(s) if s.isdigit() else 1 for s in symbols))
            found.update(_COLOURLESS if s.isdigit() else s.upper() for s in symbols)
        colours = frozenset(found)
    net = made - cost.mana
    return (net, colours) if net > 0 else None


def _land_understood(text: str) -> bool:
    """Whether a land's mana is a fixed amount and the land costs or does nothing the game cannot follow: it
    does not bounce or sacrifice lands and does not search for several lands."""
    return (
        _BOUNCE_LAND.search(text) is None
        and _LAND_SACRIFICE.search(text) is None
        and _SEARCHES_SEVERAL.search(text) is None
        and not any(line.varies or line.needs for line in _abilities(text) if not line.spend_on)
    )


def _land_faces(info: CardMana) -> list[str]:
    """The casefolded text of each face a card is played as a land by: a land's own land faces (a Pathway's
    two), a modal card's land back."""
    faces = [REMINDER_TEXT.sub("", face).casefold() for face in info.oracle_text.split(FACE_SEPARATOR)]
    if not info_is_land(info):
        return faces[-1:]
    types = info.type_line.split(" // ")
    return [face for face, kind in zip(faces, types, strict=False) if "Land" in kind.split()] or faces[:1]


def _land_nets(text: str, identity: frozenset[str]) -> list[tuple[int, frozenset[str]]]:
    """The net mana and colours of a land's abilities that make more than they cost and leave the land."""
    return [
        found
        for cost, what, varies, spend_on, needs in _abilities(text)
        if not varies
        and not spend_on
        and needs is None
        and (paid := _paid(_FILTER_COST.sub("{1}", cost))) is not None
        and not paid.sacrifices
        and (found := _net_mana(paid, what, identity))
    ]


def _land_face(info: CardMana, context: _Context) -> tuple[LandFace, bool]:
    """The land a card is played as and whether its text is understood; a modal card whose back is a land is
    played as its back.

    Colours and amount come from the mana abilities read from its text, as the game can use them: Scryfall's
    produced mana is only the fallback for a land with none (a basic land, whose ability is reminder text),
    and a land whose mana is all restricted (Ancient Ziggurat) makes none."""
    faces = _land_faces(info)
    text = faces[0]
    nets = [net for face in faces for net in _land_nets(face, context.identity)]
    tapped = not mostly_untapped(tapped_share(text, context.lands))
    if nets or RESTRICTED in text:
        colours = frozenset(c for _, got in nets for c in got)
    else:
        colours = frozenset(land_colours(info, text, set(info.produced), len(context.identity)))
    if _SEARCHES_SEVERAL.search(text) is None:
        colours |= _fetchable_colours(text, context)
    if context.identity:
        colours = colours & (context.identity | {_COLOURLESS}) or colours
    makes_mana = bool(colours) or _COPY_LAND.search(text) is not None
    amount = max((net for net, _ in nets), default=int(makes_mana))
    lost = _LOST_TO_LAND_DROP.search(text) is not None
    return LandFace(colours, tapped, amount, lost_to_land_drop=lost), _land_understood(text)


_BASIC_SEARCH = re.compile(r"search your library for [^.]*\bbasic\b")
# A land that finds more than one land (Krosan Verge, Myriad Landscape) is no stand-in for the one it finds.
_SEARCHES_SEVERAL = re.compile(r"search your library for [^.]*(?:\b(?:two|three|four)\b|put them)")


def _fetchable_colours(text: str, context: _Context) -> frozenset[str]:
    """The colours of the deck's lands that a land searching by basic land type can fetch: those with one of
    the types it names. A search for basic lands finds only those types' basics."""
    if "search your library" not in text:
        return frozenset()
    searched = {land_type for land_type in BASIC_TYPES if land_type in text}
    if _BASIC_SEARCH.search(text):
        return frozenset(BASIC_TYPES[land_type] for land_type in searched)
    return frozenset(c for types, made in context.typed_lands if types & searched for c in made)


def _mana(text: str, kinds: frozenset[CardKind], identity: frozenset[str]) -> ManaAbility | None:
    """The best mana ability of a nonland card: the most mana, the colours of all its abilities."""
    spell = CardKind.INSTANT in kinds or CardKind.SORCERY in kinds
    best: ManaAbility | None = None
    colours: set[str] = set()
    abilities = _abilities(text)
    treasures = _TREASURES.search(text) if spell else None
    if treasures is not None:
        made = f"{treasures.group('n')} mana of any color"
        abilities.append(_ManaLine("", made, varies=False, spend_on=frozenset()))
    for cost, what, varies, spend_on, needs in abilities:
        paid = _paid(cost)
        # An ability with none of these costs could be used again and again (Three Tree Mascot's {1}); life
        # paid without a tap is limited by the life.
        usable = spell or (
            paid is not None and (paid.taps or paid.exiles_from_hand or paid.sacrifices or paid.life > 0)
        )
        found = None if varies or paid is None or not usable else _net_mana(paid, what, identity)
        if paid is None or found is None:
            continue
        # Restricted mana is followed only for one shot, spent on the spell it is made for.
        if spend_on and not (spell or paid.sacrifices):
            continue
        colours |= found[1]
        if best is None or found[0] > best.amount:
            # A Vehicle is crewed to make mana, and it is summoning sick the turn it comes like a creature.
            crewed = needs is ManaNeed.CREATURE
            best = ManaAbility(
                found[0],
                found[1],
                delayed=(CardKind.CREATURE in kinds and paid.taps) or crewed,
                ritual=spell or paid.sacrifices or paid.exiles_from_hand or "doesn't untap" in text,
                from_hand=paid.exiles_from_hand,
                spend_on=spend_on,
                needs=ManaNeed.CREATURE if paid.taps_creature else needs,
                life=0 if paid.taps else paid.life,
                land_sacrificed=frozenset({paid.land}) if paid.land else frozenset(),
                discards_hand=paid.discards_hand,
                plus_sacrificed=spell and _PLUS_SACRIFICED.search(text) is not None,
            )
    if best is None:
        return _mana_aura(text, identity)
    return replace(best, colours=frozenset(colours))


def _mana_aura(text: str, identity: frozenset[str]) -> ManaAbility | None:
    matched = _MANA_AURA.search(text)
    if matched is None:
        return None
    found = _net_mana(_Cost(0, taps=False, sacrifices=False), matched.group("what"), identity)
    if found is None:
        return None
    return ManaAbility(found[0], found[1], delayed=False, ritual=False, on_land=True)


# --- searches and draws -------------------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _Search:
    """The first library search in a card's text."""

    # The words before "card", which say what is searched for.
    head: str
    sentence: str
    # The text from the search to the end of its line, where the cards go.
    rest: str
    # The cost of the ability that searches, "" for a spell or a trigger.
    cost: str
    # Happens again and again ("whenever ...", "at the beginning of ...").
    repeating: bool
    # Does more than find one set of cards: it sacrifices something first (Entish Restoration) or looks for
    # a second kind of card (Yasharn).
    compound: bool


_SEARCH = re.compile(r"search your library(?: and/or graveyard)? for (?P<what>[^.]*)\.")
# A leading ability word ("Enrage — ", "Constellation — "), which says nothing about when the text happens.
_ABILITY_WORD = re.compile(r"^[^—\n]+ — ")
_REPEATING = ("whenever", "at the beginning")
_SECOND_CARD = re.compile(r"\bcards? and (?:an?|one) ")
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
_UNSUPPORTED_CONDITION = re.compile(r"\bwith\b(?! mana value (?:x|\d+) or less\b)")
_SACRIFICE_VALUE = re.compile(r"where x is (\d+) plus the sacrificed creature's mana value")
_TAPPED = re.compile(r"\btapped\b")
_DRAW = re.compile(r"\bdraws? (?:(?P<n>an?|one|two|three|four|five|six|seven|x) cards?|cards)")
_OPPONENT_DRAWS = re.compile(r"opponents? draws?")


def _search(text: str) -> _Search | None:
    for line in (_ABILITY_WORD.sub("", line, count=1) for line in text.split("\n")):
        matched = _SEARCH.search(line)
        if matched is None:
            continue
        before = line[: matched.start()]
        cost = before.partition(":")[0] if ":" in before else ""
        head = re.split(r"\bcards?\b", matched.group("what"), maxsplit=1)[0]
        repeating = line.startswith(_REPEATING)
        compound = (not cost and "sacrifice" in before) or _SECOND_CARD.search(matched.group(0)) is not None
        return _Search(head, matched.group(0), line[matched.start() :], cost, repeating, compound)
    return None


def _land_words(search: _Search | None) -> list[str] | None:
    """The words of a search that looks for lands only; None for any other search."""
    if search is None:
        return None
    words = re.findall(r"[a-z]+", search.head)
    if not _LAND_WORDS.intersection(words) or not _LAND_WORDS.issuperset(set(words) - _FILLER - {"basic"}):
        return None
    return words


def _land_ramp(search: _Search | None, kinds: frozenset[CardKind]) -> LandRamp | None:
    """A search for lands put onto the battlefield or into the hand, when the game can follow it."""
    words = _land_words(search)
    if search is None or search.repeating or search.compound or words is None:
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
    # "A Forest card" names the types; "a land card" or "a basic land card" takes any.
    types = frozenset[str]() if "land" in words else frozenset(BASIC_TYPES).intersection(words)
    return LandRamp(count, "basic" in words, destination, to_hand, cost.mana, types)


def _tutor(search: _Search | None) -> Tutor | None:
    """A search for a card of some type, when the game can follow where it goes."""
    if search is None or search.cost or search.repeating or search.compound:
        return None
    named = re.findall(r"[a-z]+", search.head)
    words = [w for w in named if w not in _FILLER]
    finds = frozenset(_TUTOR_KINDS[w] for w in words if w in _TUTOR_KINDS)
    destination = _tutor_destination(search.rest)
    if len(finds) != len(words) or destination is None or "legendary" in named:
        return None
    # What the card must have, before the comma that goes on to reveal and place it.
    condition = re.split(r"\bcards?\b", search.sentence, maxsplit=1)[-1].partition(",")[0]
    if _UNSUPPORTED_CONDITION.search(condition):
        return None
    limit = _VALUE_LIMIT.search(search.rest)
    over = _SACRIFICE_VALUE.search(search.rest)
    x_limited = limit is not None and limit.group(1) == "x" and over is None
    value = int(limit.group(1)) if limit is not None and limit.group(1) != "x" else None
    bonus = int(over.group(1)) if over is not None else None
    return Tutor(finds, destination, value, x_limited=x_limited, value_over_sacrifice=bonus)


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
    for line in (_ABILITY_WORD.sub("", line, count=1) for line in text.split("\n")):
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
        elif line.startswith(_REPEATING):
            return Draw(count, engine=True)
        elif "enters" in line:
            arrival = Draw(count, engine=False)
    return arrival


def _read_spell(card: GoldfishCard, info: CardMana, text: str, context: _Context) -> GoldfishCard:
    """What a nonland card does, from its half's text. A card is not modelled only when it matters to the
    goldfish (it is ramp, draw or a tutor, makes mana or fetches lands, or is a combo piece or a finisher)
    and the reading cannot follow it; every other card is a plain spell."""
    roles: frozenset[Role] = (
        context.infos[info.oracle_id].roles if info.oracle_id in context.infos else frozenset()
    )
    search = _search(text)
    matters = _matters(text, roles, search)
    key_card = card.combo or card.finisher
    if _COST_REDUCER.search(text):
        return replace(card, modelled=False)
    if _UNMODELLED.search(text):
        return replace(card, modelled=not (matters or key_card))
    ramp = _land_ramp(search, card.kinds)
    land_drops = _EXTRA_LAND_DROPS.search(text)
    found = {
        "mana": _mana(text, card.kinds, context.identity),
        "land_ramp": ramp,
        "tutor": None if ramp is not None or Role.TUTOR not in roles else _tutor(search),
        "draw": _draw(text, card.kinds) if Role.DRAW in roles else None,
    }
    extra = _NUMBER_WORDS[land_drops.group(1)] if land_drops else 0
    placed = extra > 0 or any(value is not None for value in found.values())
    # A key card cast for a cost the Tagger calls alternative may be cast sooner than its mana value says.
    alternative = bool(context.tags.get(info.oracle_id, frozenset()) & ALTERNATIVE_COST_TAGS)
    return replace(
        card,
        modelled=(placed or not matters) and not (alternative and key_card),
        extra_land_drops=extra,
        needs_sacrifice=_SACRIFICE_COST.search(text) is not None,
        entry_cost=next((cost for pattern, cost in _ENTRY_COSTS if pattern.search(text)), None),
        **found,
    )


def _matters(text: str, roles: frozenset[Role], search: _Search | None) -> bool:
    """Whether the card is about what the goldfish measures: the Tagger's ramp (for its owner), draw or tutor
    role, mana in its text, or a search for lands."""
    ramp = Role.RAMP in roles and _FOR_OPPONENT.search(text) is None
    draws = Role.DRAW in roles and re.search(r"\bdraws?\b", text) is not None
    return ramp or draws or Role.TUTOR in roles or bool(_abilities(text)) or _land_words(search) is not None
