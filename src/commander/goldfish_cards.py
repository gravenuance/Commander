"""A deck compiled once into small frozen records for the goldfish game loop (ADR 0054).

Each card's rules text is read here, once, so no text is parsed during play. Lands, costs and two-part cards
come from `manabase`; Tagger roles and tags say what a card is for, and the text says how much and where.
A card the reading cannot place stays a plain spell, flagged `modelled=False` and named in `not_modelled`.
"""

import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
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
# The subtypes of a card with changeling: every creature type.
CHANGELING = "changeling"
_CHANGELING = re.compile(r"^changeling\b", re.MULTILINE)
# The subtypes of the other card types, which a changeling does not have: its types are the creature types.
NON_CREATURE_SUBTYPES = frozenset(
    word
    for words in (
        # Artifact
        "attraction blood bobblehead clue contraption equipment food fortification gold incubator junk",
        "lander map powerstone spacecraft treasure vehicle",
        # Enchantment
        "aura background cartouche case class curse role room rune saga shrine",
        # Land, without the words of Power-Plant that are also creature types
        "cave desert forest gate island lair locus mine mountain plains planet sphere swamp tower town",
        # Instant, sorcery and battle
        "adventure arcane lesson omen trap siege",
    )
    for word in words.split()
)


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
    # The colours a found card must have one of (Natural Order: green); "C" for a colourless card.
    colours: frozenset[str] = frozenset()
    max_power: int | None = None
    # Subtypes a found card must have one of: besides one of `finds` ("a Dragon creature card"), or instead
    # of one when `either` ("an artifact or Dragon card").
    subtypes: frozenset[str] = frozenset()
    either: bool = False
    # Finds any number of cards whose mana values add up to this or less (Protean Hulk: 6).
    total_mana_value: int | None = None


@dataclass(slots=True, frozen=True)
class ActivatedTutor:
    """A permanent's ability that searches, paid for by sacrificing Treasures (Magda, Brazen Outlaw) or by
    loyalty (Tezzeret, Cruel Captain's -3). Loyalty is not in the card data: a -N ability is taken as payable
    once, when N is at most the card's mana value."""

    tutor: Tutor
    treasures: int = 0
    loyalty: int = 0


@dataclass(slots=True, frozen=True)
class Draw:
    """Cards drawn on casting; an `engine` is a permanent drawing again each turn."""

    count: int
    engine: bool


class Measure(Enum):
    """What a spell that costs less by its creatures counts among the creatures you control."""

    GREATEST_POWER = "greatest power"
    TOTAL_POWER = "total power"
    # The greatest mana value among those with a subtype (Sunderflock: Elementals).
    GREATEST_VALUE = "greatest mana value"
    CREATURES = "number of creatures"


@dataclass(slots=True, frozen=True)
class Discount:
    """A spell that costs generic mana less by what the creatures its controller has are: `step` less for
    each unit of the measure (Ghalta: total power), or, with `at_least`, `step` less once when the measure
    reaches it (Bolt Bend: {3} less with a creature of power 4 or more)."""

    measure: Measure
    step: int = 1
    subtype: str = ""
    at_least: int | None = None


@dataclass(slots=True, frozen=True)
class SpellKind:
    """The spells a cost reducer names. A spell is of the kind when it meets every criterion set."""

    kinds: frozenset[CardKind] = frozenset()
    not_kinds: frozenset[CardKind] = frozenset()
    # The colours the spell must have.
    colours: frozenset[str] = frozenset()
    # A subtype the spell must have, casefolded.
    subtype: str = ""


@dataclass(slots=True, frozen=True)
class Reducer:
    """A permanent that takes `amount` generic mana off each spell of any of its kinds while it is in play."""

    spells: tuple[SpellKind, ...]
    amount: int


class CopyKind(Enum):
    """When and how long a card becomes a copy of another permanent."""

    # At the beginning of combat, until end of turn (Gogo, Mysterious Mime).
    COMBAT = "becomes a copy at the beginning of combat"
    # By paying mana, until end of turn (Hall of Echoes).
    ACTIVATED = "becomes a copy for mana"
    # As it enters, until end of turn (Cursed Mirror).
    TURN = "becomes a copy as it enters, until end of turn"
    # Enters as a copy and stays one (Sculpting Steel, Phantasmal Image, Vesuva).
    ENTERS = "enters as a copy"


class CopyOf(Enum):
    CREATURE = "creature"
    ARTIFACT = "artifact"
    ARTIFACT_OR_CREATURE = "artifact or creature"
    LAND = "land"


@dataclass(slots=True, frozen=True)
class CopyEffect:
    """A card that becomes a copy of a permanent in play."""

    kind: CopyKind
    of: CopyOf
    # Generic mana paid to become the copy (Hall of Echoes: 5).
    cost: int = 0
    # The copy has haste (Cursed Mirror), so it attacks the turn it comes.
    haste: bool = False
    # The legend rule does not apply to it: it copies a legendary permanent and both stay (Gogo is renamed,
    # Hall of Echoes lifts the rule). Any other copy of a legendary permanent is sacrificed.
    legend_exempt: bool = False
    # The copy enters tapped (Vesuva).
    tapped: bool = False
    # Only a permanent of a mana value up to the mana spent on the copy can be copied (Mockingbird).
    mana_spent: bool = False


class TriggerCause(Enum):
    """What a trigger doubler waits for."""

    # Any triggered ability of the creatures it names (Roaming Throne, Delney, Twinflame Travelers).
    ANY = "any trigger"
    # A permanent entering (Panharmonicon, Elesh Norn, Yarok).
    ENTERING = "a permanent entering"
    # A creature attacking (Windcrag Siege's Mardu mode).
    ATTACKING = "a creature attacking"
    # A creature dealing combat damage: no card names it, so only a doubler of any trigger repeats those.
    DAMAGING = "a creature dealing combat damage"


@dataclass(slots=True, frozen=True)
class TriggerDoubler:
    """A permanent under which a triggered ability of the permanents you control triggers an additional time,
    for each doubler in play. `cause` says which triggers, and the rest limit them to the triggers of some
    creatures."""

    cause: TriggerCause
    # What the entering permanent must be one of, for ENTERING; empty for any permanent.
    entering: frozenset[CardKind] = frozenset()
    # Only the triggers of creatures with this subtype, with the type this creature names as it enters
    # (Roaming Throne), or with power at most this.
    subtype: str | None = None
    chosen_type: bool = False
    max_power: int | None = None
    # Not the doubler's own triggers.
    another: bool = False


class Strike(Enum):
    """When a creature deals its combat damage: in the first-strike step, the regular one or both."""

    REGULAR = "regular damage"
    FIRST = "first strike"
    DOUBLE = "double strike"


class DamageScope(Enum):
    """Whose combat damage sets a trigger off."""

    # The creature itself (Ragavan, Goldlust Triad).
    SELF = "this creature"
    # One or more creatures you control, once for each step they deal damage in (Professional Face-Breaker).
    ANY = "one or more creatures you control"
    # Each creature you control (of the types) once for each step it deals damage in.
    EACH = "a creature you control"


@dataclass(slots=True, frozen=True)
class DamageTreasures:
    """Treasures made when creatures deal combat damage to a player. `types` limits ANY and EACH to creatures
    with one of these subtypes; empty for any creature."""

    scope: DamageScope
    count: int
    types: frozenset[str] = frozenset()


class Unread(Enum):
    """An effect the Tagger says a card has that changes what the goldfish counts, which the reading of the
    card leaves out."""

    # More tokens, so more Treasures.
    TOKENS = "makes more tokens"
    # A trigger that happens an additional time.
    TRIGGERS = "makes triggers happen again"
    # More mana from a permanent tapped for it.
    MANA = "makes more mana"
    # A copy of a permanent, or of a land.
    COPY = "copies a permanent"
    COPY_LAND = "copies a land"


@dataclass(slots=True, frozen=True)
class AbilityCopy:
    """A permanent that copies an activated or triggered ability you control for `cost` generic mana and a
    counter taken from it, and so many times as it has `counters` (Peter Parker's Camera)."""

    cost: int
    counters: int


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
    # The front face's power, None for a card with none or a power that varies (*).
    power: int | None = None
    # The subtypes of the type line, casefolded; a changeling's are CHANGELING, which is every creature type.
    subtypes: frozenset[str] = frozenset()
    # A creature with an activated ability that taps it and makes no mana (Hermit Druid): as a combo piece it
    # must have been in play since the turn began.
    tap_ability: bool = False
    # Treasures it makes as it comes (Plundering Barbarian) and each time it attacks.
    treasures_on_entry: int = 0
    treasures_on_attack: int = 0
    # The creature subtypes ("creature" for any) that make a Treasure as one becomes tapped (Magda: Dwarf),
    # which a creature does as it attacks.
    treasure_on_tap: frozenset[str] = frozenset()
    # Treasures it makes as creatures deal combat damage to a player, which with no blocker is every attacker
    # whose power is not 0, and the keyword that says in which steps a creature deals it.
    on_damage: DamageTreasures | None = None
    strike: Strike = Strike.REGULAR
    # A creature with the haste keyword: it attacks, and taps for mana, the turn it comes.
    haste: bool = False
    activated_tutor: ActivatedTutor | None = None
    # A search as the creature dies (Protean Hulk, Academy Rector), which a free sacrifice outlet brings on.
    dies_tutor: Tutor | None = None
    # Sacrifices a creature for no mana (Carrion Feeder, Phyrexian Tower, Ashnod's Altar).
    free_outlet: bool = False
    # Takes generic mana off other spells while in play (Jet Medallion), and off its own cost by what the
    # creatures in play are (Ghalta).
    reducers: tuple[Reducer, ...] = ()
    discount: Discount | None = None
    # Every creature its controller has gets -N/-N while it is in play (Heartless Summoning: 1).
    shrink: int = 0
    # The front face's toughness, None for a card with none or one that varies (*).
    toughness: int | None = None
    legendary: bool = False
    # A permanent whose arrival effects the goldfish counts are triggers of it entering: Treasures, a search,
    # lands fetched or cards drawn, which a trigger doubler repeats.
    entry_trigger: bool = False
    # Treasures made are this many more per event (Xorn), and this many times as many (Parallel Lives).
    treasure_extra: int = 0
    treasure_factor: int = 1
    trigger_doubler: TriggerDoubler | None = None
    copy: CopyEffect | None = None
    ability_copy: AbilityCopy | None = None
    # What the Tagger says it does that the record leaves out. A deck lists the card as not modelled when it
    # has what the effect works on.
    unread: frozenset[Unread] = frozenset()
    # Not modelled only because the deck has what an unread effect works on, so it is modelled again in a
    # deck without (`flag_unread`).
    flagged: bool = False

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
    # Each card's printed power and toughness by oracle id.
    powers: Mapping[str, str | None] = field(default_factory=dict[str, str | None])
    toughnesses: Mapping[str, str | None] = field(default_factory=dict[str, str | None])
    # The subtypes the cards have, which a tutor may name.
    subtypes: frozenset[str] = frozenset()


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
    powers: Mapping[str, str | None] | None = None,
    toughnesses: Mapping[str, str | None] | None = None,
) -> GoldfishDeck:
    """The deck's cards as records, from data already loaded.

    cards holds the deck's cards by casefolded name, infos their Tagger roles by oracle id (`CardDb.cards`),
    and tags each card's tag slugs with every tag above them (`CardDb.tag_lineage`). synergy is the oracle ids
    of the cards matching or feeding the deck's dominant tags, or that its commander wants
    (`ideas.dominant_card_ids`). The finisher names are added to and taken from the cards the Tagger tags
    make finishers. powers and toughnesses hold the cards' printed power and toughness by oracle id
    (`CardDb.card_texts`); a card they lack has none. A card the card data lacks is a plain spell of the
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
        powers=powers or {},
        toughnesses=toughnesses or {},
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
    records = flag_unread(compiled.values())
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
    powers: Mapping[str, str | None],
    toughnesses: Mapping[str, str | None],
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
        powers=powers,
        toughnesses=toughnesses,
        subtypes=frozenset(word for info in cards.values() for word in _subtypes(info.type_line)),
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
    powers: Mapping[str, str | None] | None = None,
    toughnesses: Mapping[str, str | None] | None = None,
) -> GoldfishCard:
    """One card read as `compile_deck` reads the deck's own: its identity, the typed lands fetches find, the
    combos, synergy and finishers. For a card that is not in the deck, a swap's incoming card; `cards`,
    `infos`, `powers` and `toughnesses` must hold it as well as the deck. A combo is the deck's own, so the
    card is no piece of one that names it. A card the card data lacks is a plain spell, not modelled. One with
    an effect the goldfish does not follow (`_unread_effects`) is flagged with the cards of the swapped deck
    (`flag_unread`)."""
    context = _deck_context(
        deck,
        cards,
        infos,
        tags,
        combos=combos,
        synergy=synergy,
        finishers_added=finishers_added,
        finishers_removed=finishers_removed,
        powers=powers or {},
        toughnesses=toughnesses or {},
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


def _subtypes(type_line: str) -> frozenset[str]:
    """The subtypes of a card's front face, casefolded."""
    return frozenset(re.findall(r"[a-z]+", front_type(type_line).casefold().partition("—")[2]))


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
# An ability that sacrifices a creature for no mana, tapping its permanent at most (Phyrexian Tower).
_FREE_OUTLET = re.compile(r"^(?:\{t\}, )?sacrifice (?:a|another) creature(?:, \{t\})?: ", re.MULTILINE)
# An activated ability that taps its permanent and adds no mana (Hermit Druid's "{G}, {T}: Reveal ...").
_TAP_ABILITY = re.compile(r"^[^:\n]*\{t\}[^:\n]*: (?!add\b)", re.MULTILINE)
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
    card = _read_card(name, info, copies, context, commander=commander)
    if info is None:
        return card
    tags = context.tags.get(info.oracle_id, frozenset())
    return replace(card, unread=_unread_effects(card, info.oracle_text.casefold(), tags))


def _read_card(
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
    text = info.oracle_text.casefold()
    changeling = _CHANGELING.search(text) is not None
    card = replace(
        card,
        x_cost=any(symbol == "X" for symbol in PIP.findall(cost_halves(info.mana_cost)[face])),
        power=_stat(context.powers.get(info.oracle_id)),
        # A printed toughness of 0 is a creature whose toughness is set by other cards (Faeburrow Elder).
        toughness=_stat(context.toughnesses.get(info.oracle_id)) or None,
        subtypes=frozenset({CHANGELING}) if changeling else _subtypes(info.type_line),
        free_outlet=card.permanent and _FREE_OUTLET.search(text) is not None,
        legendary=_legendary(info.type_line),
    )
    if info_is_land(info):
        land, understood = _land_face(info, context)
        return replace(
            card, land=land, land_types=_land_types(info), modelled=understood, copy=_copy_effect(text)
        )
    card = _read_spell(card, info, _face_text(info, face), context)
    if back_is_land(info.type_line):
        land, understood = _land_face(info, context)
        return replace(card, land=land, modelled=card.modelled and understood)
    return card


def _stat(printed: str | None) -> int | None:
    """A printed power or toughness, None when the card has none or it varies (*)."""
    return int(printed) if printed is not None and printed.isdigit() else None


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
# What limits an ability beyond the metalcraft and crew conditions the game follows (Fanatic of Rhonas'
# ferocious mana, Kozilek's Translator): its mana is left out of a nonland card, the rest of the card stays.
_CONDITION = re.compile(r"activate only if|only once each turn")
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
# "Three mana in any combination of {R} and/or {G}", or of "colors", which are the deck's.
_COMBINATION = re.compile(r"(?P<n>\w+) mana in any combination of (?P<symbols>.*)")
_COLOURS_WORD = re.compile(r"colou?rs\b")
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
    or a colour or use the game cannot tell), the kinds of spell alone it may pay for (empty for any), what
    it needs on the battlefield and whether a condition the game cannot follow limits when it is used (a land
    with one is still counted, as Hushwood Verge's second colour is)."""

    cost: str
    what: str
    varies: bool
    spend_on: frozenset[CardKind]
    needs: ManaNeed | None = None
    conditional: bool = False


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
        varies = _NOT_FIXED_MANA.search(said) is not None
        found.append(_ManaLine(cost, what, varies, spend_on, needs, _CONDITION.search(after) is not None))
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
        if _COLOURS_WORD.match(mixed.group("symbols")):
            colours = identity or _ALL_COLOURS
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
        for cost, what, varies, spend_on, needs, _ in _abilities(text)
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


def _spell_treasures(text: str, kinds: frozenset[CardKind]) -> int:
    """The Treasures an instant or sorcery makes; 0 for any other card."""
    found = _TREASURES.search(text) if CardKind.INSTANT in kinds or CardKind.SORCERY in kinds else None
    return _NUMBER_WORDS[found.group("n")] if found is not None else 0


def _mana(
    text: str, kinds: frozenset[CardKind], identity: frozenset[str], mana_value: int
) -> ManaAbility | None:
    """The best mana ability of a nonland card: the most mana, the colours of all its abilities. The Treasures
    of a spell are mana only when they are more than its cost; else it is cast for what else it does."""
    spell = CardKind.INSTANT in kinds or CardKind.SORCERY in kinds
    best: ManaAbility | None = None
    colours: set[str] = set()
    abilities = _abilities(text)
    treasures = _spell_treasures(text, kinds)
    if treasures > mana_value:
        made = f"{_NUMBER_NAMES[treasures]} mana of any color"
        abilities.append(_ManaLine("", made, varies=False, spend_on=frozenset()))
    for cost, what, varies, spend_on, needs, conditional in abilities:
        paid = _paid(cost)
        # An ability with none of these costs could be used again and again (Three Tree Mascot's {1}); life
        # paid without a tap is limited by the life.
        usable = spell or (
            paid is not None and (paid.taps or paid.exiles_from_hand or paid.sacrifices or paid.life > 0)
        )
        skipped = varies or conditional or paid is None or not usable
        found = None if skipped or paid is None else _net_mana(paid, what, identity)
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
        return _mana_aura(text, identity) or _main_phase_mana(text, identity)
    return replace(best, colours=frozenset(colours))


# Carpet of Flowers: mana as a main phase begins, once a turn, for the Islands an opponent controls.
_MAIN_PHASE_MANA = re.compile(
    r"^at the beginning of each of your main phases, if you haven't added mana with this ability this turn, "
    r"you may add x mana of any one colou?r, where x is the number of islands target opponent controls\.",
    re.MULTILINE,
)
# The owner decided that an opponent counts two Islands, in every bracket.
OPPONENT_ISLANDS = 2


def _main_phase_mana(text: str, identity: frozenset[str]) -> ManaAbility | None:
    """The mana a permanent makes once a turn as a main phase begins, so from the turn after it comes: the
    first main phase it is in play for is the next turn's, or the second one's of this."""
    if _MAIN_PHASE_MANA.search(text) is None:
        return None
    return ManaAbility(OPPONENT_ISLANDS, identity or _ALL_COLOURS, delayed=True, ritual=False)


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
    # Happens on a trigger other than the card's arrival or casting, such as its death (Academy Rector).
    deferred: bool = False
    # The line before the search: the trigger or the cost and what else happens first.
    before: str = ""


_SEARCH = re.compile(r"search your library(?: and/or graveyard)? for (?P<what>[^.]*)\.")
# A one-time trigger that is the card's arrival or casting, which happens as it is cast.
_ON_CAST = re.compile(r"^when (?:you cast\b|[^,.]* enters\b)")
# A leading ability word ("Enrage — ", "Constellation — "), which says nothing about when the text happens.
_ABILITY_WORD = re.compile(r"^[^—\n]+ — ")
_REPEATING = ("whenever", "at the beginning")
_SECOND_CARD = re.compile(r"\bcards? and (?:an?|one) ")
_LAND_WORDS = frozenset({"land", "plains", "island", "swamp", "mountain", "forest"})
_COUNT_WORDS = {"two": 2, "three": 3, "four": 4}
_COLOUR_WORDS = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G", "colorless": "C"}
_FILLER = frozenset(
    {"a", "an", "one", "two", "three", "four", "up", "to", "or", "and", "legendary", "nonland"}
    | set(_COLOUR_WORDS)
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
_POWER_LIMIT = re.compile(r"with power (\d+) or less")
_UNSUPPORTED_CONDITION = re.compile(
    r"\bwith\b(?! (?:mana value (?:x|\d+)|total mana value \d+|power \d+) or less\b)"
)
_TOTAL_LIMIT = re.compile(r"with total mana value (\d+) or less")
_ANY_NUMBER = "any number of "
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
        deferred = line.startswith("when ") and _ON_CAST.match(line) is None
        rest = line[matched.start() :]
        return _Search(
            head, matched.group(0), rest, cost, repeating, compound, deferred=deferred, before=before
        )
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


def _tutor(search: _Search | None, known_subtypes: frozenset[str]) -> Tutor | None:
    """A search for a card of some types, colours or subtypes, when the game can follow where it goes. A
    subtype must be one of `known_subtypes`, the deck's, so a word that is none is not read for one, unless
    it is the other choice beside a card type ("an artifact or Dragon card"), which only widens the search."""
    if search is None or search.cost or search.repeating or search.compound or search.deferred:
        return None
    # "Any number of creature cards with total mana value 6 or less" (Protean Hulk).
    many = search.head.startswith(_ANY_NUMBER)
    named = re.findall(r"[a-z]+", search.head.removeprefix(_ANY_NUMBER))
    words = [w for w in named if w not in _FILLER]
    finds = frozenset(_TUTOR_KINDS[w] for w in words if w in _TUTOR_KINDS)
    subtypes = frozenset(w for w in words if w not in _TUTOR_KINDS)
    either = bool(finds and subtypes) and "or" in named
    known = subtypes <= known_subtypes or either
    destination = _tutor_destination(search.rest)
    if not known or subtypes & _LAND_WORDS or destination is None or "legendary" in named:
        return None
    # What the card must have, before the comma that goes on to reveal and place it.
    condition = re.split(r"\bcards?\b", search.sentence, maxsplit=1)[-1].partition(",")[0]
    total = _TOTAL_LIMIT.search(condition)
    if _UNSUPPORTED_CONDITION.search(condition) or many != (total is not None):
        return None
    limit = None if total is not None else _VALUE_LIMIT.search(search.rest)
    over = _SACRIFICE_VALUE.search(search.rest)
    power = _POWER_LIMIT.search(condition)
    x_limited = limit is not None and limit.group(1) == "x" and over is None
    value = int(limit.group(1)) if limit is not None and limit.group(1) != "x" else None
    bonus = int(over.group(1)) if over is not None else None
    return Tutor(
        finds,
        destination,
        value,
        x_limited=x_limited,
        value_over_sacrifice=bonus,
        colours=frozenset(_COLOUR_WORDS[w] for w in named if w in _COLOUR_WORDS),
        max_power=int(power.group(1)) if power is not None else None,
        subtypes=subtypes,
        either=either,
        total_mana_value=int(total.group(1)) if total is not None else None,
    )


def _dies_tutor(search: _Search | None, card: GoldfishCard, known_subtypes: frozenset[str]) -> Tutor | None:
    """A creature's search as it dies, read as the tutor it would be on casting."""
    if search is None or not search.deferred or CardKind.CREATURE not in card.kinds:
        return None
    trigger = search.before.partition(",")[0]
    if trigger not in {f"when {name} dies" for name in _names_of_itself(card)}:
        return None
    return _tutor(replace(search, deferred=False), known_subtypes)


_TREASURE_SACRIFICE = re.compile(r"^sacrifice (a|one|two|three|four|five|six|seven) treasures?$")
# A loyalty cost's minus: the card data's minus sign, or a hyphen.
_LOYALTY_COST = re.compile("^[\N{MINUS SIGN}-](\\d+)$")


def _activated_tutor(
    search: _Search | None, card: GoldfishCard, known_subtypes: frozenset[str]
) -> ActivatedTutor | None:
    """A permanent's search paid for with Treasures or loyalty, read as the tutor it would be with no cost."""
    if search is None or not search.cost or not card.permanent:
        return None
    treasures = _TREASURE_SACRIFICE.match(search.cost)
    loyalty = _LOYALTY_COST.match(search.cost)
    removed = int(loyalty.group(1)) if loyalty is not None else 0
    if CardKind.PLANESWALKER not in card.kinds or removed > card.mana_value:
        removed = 0
    if treasures is None and not removed:
        return None
    tutor = _tutor(replace(search, cost=""), known_subtypes)
    if tutor is None:
        return None
    sacrificed = _NUMBER_WORDS[treasures.group(1)] if treasures is not None else 0
    return ActivatedTutor(tutor, treasures=sacrificed, loyalty=removed)


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
    key_card = card.combo or card.finisher
    reductions = _reductions(text, card)
    if reductions is None:
        return replace(card, modelled=False)
    # The Tagger calls a cost reducer ramp; its reduction is all the ramp it has.
    reducing = bool(reductions.reducers) or reductions.discount is not None
    matters = _matters(text, roles - {Role.RAMP} if reducing else roles, search)
    if _UNMODELLED.search(text):
        return replace(card, modelled=not (matters or key_card), **reductions._asdict())
    ramp = _land_ramp(search, card.kinds)
    land_drops = _EXTRA_LAND_DROPS.search(text)
    searches = ramp is None and Role.TUTOR in roles
    found = {
        "mana": _mana(text, card.kinds, context.identity, card.mana_value),
        "land_ramp": ramp,
        "tutor": _tutor(search, context.subtypes) if searches else None,
        "activated_tutor": _activated_tutor(search, card, context.subtypes) if searches else None,
        "dies_tutor": _dies_tutor(search, card, context.subtypes) if searches else None,
        "draw": _draw(text, card.kinds) if Role.DRAW in roles else None,
    }
    treasures = _treasures(text, card)
    on_damage = _damage_treasures(text, card)
    keywords = _keywords(text) if CardKind.CREATURE in card.kinds else frozenset[str]()
    tokens = _token_increase(text)
    drawn = found["draw"]
    arrives = card.permanent and (
        treasures[0] > 0
        or (isinstance(drawn, Draw) and not drawn.engine)
        or ((found["land_ramp"] is not None or found["tutor"] is not None) and _search_is_trigger(search))
    )
    extra = _NUMBER_WORDS[land_drops.group(1)] if land_drops else 0
    placed = (
        extra > 0
        or treasures != (0, 0, frozenset())
        or on_damage is not None
        or any(value is not None for value in found.values())
    )
    # A key card cast for a cost the Tagger calls alternative may be cast sooner than its mana value says.
    alternative = bool(context.tags.get(info.oracle_id, frozenset()) & ALTERNATIVE_COST_TAGS)
    return replace(
        card,
        modelled=(placed or not matters) and not (alternative and key_card),
        extra_land_drops=extra,
        needs_sacrifice=_SACRIFICE_COST.search(text) is not None,
        entry_cost=next((cost for pattern, cost in _ENTRY_COSTS if pattern.search(text)), None),
        tap_ability=CardKind.CREATURE in card.kinds and _TAP_ABILITY.search(text) is not None,
        treasures_on_entry=treasures[0],
        treasures_on_attack=treasures[1],
        treasure_on_tap=treasures[2],
        on_damage=on_damage,
        strike=_strike(keywords),
        haste="haste" in keywords,
        **reductions._asdict(),
        entry_trigger=arrives,
        treasure_extra=tokens[0],
        treasure_factor=tokens[1],
        trigger_doubler=_trigger_doubler(text),
        copy=_copy_effect(text),
        ability_copy=_ability_copy(text),
        **found,
    )


# Treasures a permanent makes as it comes, the mode of a choice included (Plundering Barbarian); a tapped
# Treasure and one made only if an opponent is ahead (Beza) are left out.
_ENTRY_TREASURES = re.compile(
    r"^when (?P<who>[^,\n]+) enters, (?:choose one —(?:\n• [^\n]*)*?\n• (?:[^—\n]+ — )?)?"
    r"create (?P<n>a|an|one|two|three|four|five) treasure tokens?\b(?![^.\n]* if (?:an?|each|any) opponent)",
    re.MULTILINE,
)
_ATTACK_TREASURES = re.compile(
    r"^whenever (?P<who>[^,\n]+) attacks, create (?P<n>a|an|one|two|three|four|five) treasure tokens?\b",
    re.MULTILINE,
)
_TAP_TREASURE = re.compile(
    r"^whenever an? (?P<type>[a-z]+) you control becomes tapped, create (?:a|one) treasure token\b",
    re.MULTILINE,
)


def _treasures(text: str, card: GoldfishCard) -> tuple[int, int, frozenset[str]]:
    """The Treasures a permanent makes as it comes and as it attacks, and the subtypes whose tapping makes
    one; those of a spell that `_mana` does not read as a ritual, made as it resolves."""
    if not card.permanent:
        made = _spell_treasures(text, card.kinds)
        return (made if made <= card.mana_value else 0), 0, frozenset()
    itself = _names_of_itself(card)
    entry, attack = (
        next((found for found in pattern.finditer(text) if found.group("who") in itself), None)
        for pattern in (_ENTRY_TREASURES, _ATTACK_TREASURES)
    )
    tapped = _TAP_TREASURE.search(text)
    return (
        _NUMBER_WORDS[entry.group("n")] if entry is not None else 0,
        _NUMBER_WORDS[attack.group("n")] if attack is not None else 0,
        frozenset({tapped.group("type")}) if tapped is not None else frozenset(),
    )


_DAMAGE_TREASURES = re.compile(
    r"^whenever (?P<who>[^,\n]+?) deals? combat damage to (?:a player|an opponent), "
    r"create (?P<n>a|an|one|two|three|four|five) treasure tokens?\b(?! for each)",
    re.MULTILINE,
)
_DAMAGING_ANY = re.compile(r"one or more (?:(?P<types>[a-z]+(?: or [a-z]+)*) )?creatures you control")
_DAMAGING_EACH = re.compile(r"an? (?P<types>[a-z]+(?: or [a-z]+)*)(?: creature)? you control")
# Words before "creature" that restrict it without naming a subtype.
_NOT_SUBTYPES_OF_CREATURES = frozenset(
    {"creature", "legendary", "nontoken", "token", "artifact", "enchanted", "equipped", "modified", "other"}
)
_KEYWORD_LINE = re.compile(r"[a-z]+(?: [a-z]+)?(?:, [a-z]+(?: [a-z]+)?)*")
_REMINDER = re.compile(r" \([^)]*\)")


def _damage_treasures(text: str, card: GoldfishCard) -> DamageTreasures | None:
    """The Treasures a creature makes when it, or one or more creatures, or each creature, deals combat damage
    to a player, in a fixed count; the creature's own damage, or that of the creatures of the types named."""
    if not card.permanent:
        return None
    itself = _names_of_itself(card)
    for found in _DAMAGE_TREASURES.finditer(text):
        who, count = found.group("who"), _NUMBER_WORDS[found.group("n")]
        if who in itself:
            return DamageTreasures(DamageScope.SELF, count)
        for pattern, scope in ((_DAMAGING_ANY, DamageScope.ANY), (_DAMAGING_EACH, DamageScope.EACH)):
            named = pattern.fullmatch(who)
            if named is None:
                continue
            words = [w for w in re.findall(r"[a-z]+", named.group("types") or "") if w != "or"]
            if _NOT_SUBTYPES_OF_CREATURES.isdisjoint(words) or words == ["creature"]:
                kinds = frozenset(words) - {"creature"}
                return DamageTreasures(scope, count, kinds)
    return None


def _keywords(text: str) -> frozenset[str]:
    """The keywords on the lines of a card that hold nothing else; a keyword another creature is given is no
    keyword of its own."""
    found: set[str] = set()
    for line in text.split("\n"):
        stripped = _REMINDER.sub("", line)
        if _KEYWORD_LINE.fullmatch(stripped):
            found.update(stripped.split(", "))
    return frozenset(found)


def _strike(keywords: frozenset[str]) -> Strike:
    """The combat damage step a creature's keywords put its damage in."""
    if "double strike" in keywords:
        return Strike.DOUBLE
    return Strike.FIRST if "first strike" in keywords else Strike.REGULAR


_TREASURE_PLUS = re.compile(
    r"if you would create (?:one or more )?treasure tokens?, instead create those tokens plus an additional "
    r"treasure token"
)
_TOKEN_MULTIPLE = re.compile(
    r"if (?:an effect would create one or more|one or more) (?P<kind>[a-z]+ )?tokens (?:would be created )?"
    r"under your control, (?:it creates )?(?P<times>twice|three times) that many"
)
_MULTIPLES = {"twice": 2, "three times": 3}
# A replacement that leaves the Treasures as they are (Academy Manufactor: one of each, the Treasure too).
_TOKEN_NEUTRAL = re.compile(r"instead create one of each")
# The kinds of token a Treasure is counted among.
_TREASURE_KINDS = frozenset({"", "artifact", "treasure"})
_ENTERING_DOUBLER = re.compile(
    r"if (?P<cause>an? [a-z]+(?: or [a-z]+)?) (?P<event>entering|attacking) causes a triggered ability of a "
    r"permanent you control to trigger, that ability triggers an additional time"
)
_SOURCE_DOUBLER = re.compile(
    r"if a triggered ability of (?:another creature you control of (?P<chosen>the chosen type)"
    r"|a creature you control with power (?P<power>\d+) or less"
    r"|another (?P<subtype>[a-z]+) you control) triggers, (?:it|that ability) triggers an additional time"
)


def _token_increase(text: str) -> tuple[int, int]:
    """The Treasures a permanent adds to each creation, and the number of times it multiplies them by."""
    multiple = _TOKEN_MULTIPLE.search(text)
    counted = multiple is not None and (multiple.group("kind") or "").strip() in _TREASURE_KINDS
    factor = _MULTIPLES[multiple.group("times")] if multiple is not None and counted else 1
    return (1 if _TREASURE_PLUS.search(text) else 0), factor


def _trigger_doubler(text: str) -> TriggerDoubler | None:
    """The triggers a permanent makes happen an additional time, when the reading can follow which."""
    entering = _ENTERING_DOUBLER.search(text)
    if entering is not None:
        if entering.group("event") == "attacking":
            return TriggerDoubler(TriggerCause.ATTACKING) if entering.group("cause") == "a creature" else None
        words = [w for w in re.findall(r"[a-z]+", entering.group("cause")) if w not in {"a", "an", "or"}]
        kinds = frozenset(_KIND_WORDS[w] for w in words if w in _KIND_WORDS)
        known = len(kinds) == len([w for w in words if w != "permanent"])
        return TriggerDoubler(TriggerCause.ENTERING, kinds) if known else None
    source = _SOURCE_DOUBLER.search(text)
    if source is None:
        return None
    if source.group("power") is not None:
        return TriggerDoubler(TriggerCause.ANY, max_power=int(source.group("power")))
    subtype = source.group("subtype")
    return TriggerDoubler(
        TriggerCause.ANY,
        subtype=subtype if subtype not in {None, "creature"} else None,
        chosen_type=source.group("chosen") is not None,
        another=True,
    )


_COMBAT_COPY = re.compile(
    r"^at the beginning of combat on your turn, you may have [^.]*? become a copy of another target "
    r"creature you control until end of turn(?P<renamed>, except its name is)?",
    re.MULTILINE,
)
_ACTIVATED_COPY = re.compile(
    r"^\{(?P<cost>\d+)\}: this \w+ becomes a copy of target creature you control until end of turn\.",
    re.MULTILINE,
)
_ENTERS_COPY = re.compile(
    r"^as this \w+ enters, you may have it become a copy of any creature on the battlefield until end of "
    r"turn(?P<haste>, except it has haste)?",
    re.MULTILINE,
)
_LEGEND_RULE_LIFTED = re.compile(r'the "legend rule" doesn\'t apply')
_ENTERS_AS_COPY = re.compile(
    r"you may have this \w+ enter (?P<tapped>tapped )?as a copy of any "
    r"(?P<of>artifact or creature|artifact|creature|land) on the battlefield"
    r"(?P<spent> with mana value less than or equal to the amount of mana spent to cast this creature)?"
)
_COPIED = {kind.value: kind for kind in CopyOf}
_ABILITY_COPY = re.compile(
    r"^\{(?P<cost>\d+)\}, \{t\}, remove an? \w+ counter from this \w+: copy target activated or triggered "
    r"ability you control",
    re.MULTILINE,
)
_COUNTERS = re.compile(r"^this \w+ enters with (?P<n>[a-z]+) \w+ counters on it", re.MULTILINE)


def _ability_copy(text: str) -> AbilityCopy | None:
    """The copy of an ability a permanent makes for a counter on it, when it says how many it enters with."""
    copying = _ABILITY_COPY.search(text)
    counters = _COUNTERS.search(text)
    if copying is None or counters is None or counters.group("n") not in _NUMBER_WORDS:
        return None
    return AbilityCopy(int(copying.group("cost")), _NUMBER_WORDS[counters.group("n")])


def _copy_effect(text: str) -> CopyEffect | None:
    """The copy a card becomes of a creature until end of turn, when the reading can follow it."""
    combat = _COMBAT_COPY.search(text)
    if combat is not None:
        return CopyEffect(
            CopyKind.COMBAT,
            CopyOf.CREATURE,
            haste="gain haste" in text,
            legend_exempt=combat.group("renamed") is not None,
        )
    activated = _ACTIVATED_COPY.search(text)
    if activated is not None:
        return CopyEffect(
            CopyKind.ACTIVATED,
            CopyOf.CREATURE,
            cost=int(activated.group("cost")),
            legend_exempt=_LEGEND_RULE_LIFTED.search(text) is not None,
        )
    entering = _ENTERS_COPY.search(text)
    if entering is not None:
        return CopyEffect(CopyKind.TURN, CopyOf.CREATURE, haste=entering.group("haste") is not None)
    stays = _ENTERS_AS_COPY.search(text)
    if stays is not None:
        return CopyEffect(
            CopyKind.ENTERS,
            _COPIED[stays.group("of")],
            tapped=stays.group("tapped") is not None,
            mana_spent=stays.group("spent") is not None,
        )
    return None


def _search_is_trigger(search: _Search | None) -> bool:
    """Whether a search happens by itself as a permanent arrives, with no cost to pay."""
    return search is not None and not (search.cost or search.repeating or search.deferred)


def _legendary(type_line: str) -> bool:
    return "legendary" in front_type(type_line).casefold().partition("—")[0].split()


def _names_of_itself(card: GoldfishCard) -> set[str]:
    """What the card's text calls it: "this creature" ("this artifact" and so on), its name or short name."""
    name = card.name.casefold()
    return {f"this {kind.value}" for kind in card.kinds} | {name, name.partition(",")[0]}


# --- effects the reading does not follow --------------------------------------------------------------------


# The Tagger tags that name them. The reading follows only some of the cards that carry each.
_UNREAD_TAGS = {
    "token-increaser": Unread.TOKENS,
    "token-doubler": Unread.TOKENS,
    "trigger-doubler": Unread.TRIGGERS,
    "mana-increaser": Unread.MANA,
    "clone": Unread.COPY,
    "copy-creature": Unread.COPY,
    "copy-artifact": Unread.COPY,
    "copy-permanent": Unread.COPY,
    "copy-ability": Unread.COPY,
    "copy-land": Unread.COPY_LAND,
}


def _unread_effects(card: GoldfishCard, text: str, tags: Collection[str]) -> frozenset[Unread]:
    """The effects the Tagger says a card has that its record leaves out: those it carries the tag of and the
    reading did not take."""
    found = {_UNREAD_TAGS[tag] for tag in tags if tag in _UNREAD_TAGS}
    if (
        card.treasure_extra
        or card.treasure_factor > 1
        or _TOKEN_MULTIPLE.search(text)
        or _TOKEN_NEUTRAL.search(text)
    ):
        found.discard(Unread.TOKENS)
    if card.trigger_doubler is not None:
        found.discard(Unread.TRIGGERS)
    if card.mana is not None or card.land is not None:
        found.discard(Unread.MANA)
    if card.copy is not None or card.ability_copy is not None:
        found -= {Unread.COPY, Unread.COPY_LAND}
    return frozenset(found)


def _worth_copying(card: GoldfishCard) -> bool:
    """Whether a copy of the permanent would give the goldfish something to count."""
    return (
        card.permanent
        and card.land is None
        and (
            card.mana is not None
            or card.treasures_on_entry > 0
            or card.treasures_on_attack > 0
            or bool(card.treasure_on_tap)
            or card.on_damage is not None
            or card.treasure_extra > 0
            or card.treasure_factor > 1
            or card.trigger_doubler is not None
            or card.entry_trigger
            or card.extra_land_drops > 0
            or (card.draw is not None and card.draw.engine)
        )
    )


def _worked_on(card: GoldfishCard) -> frozenset[Unread]:
    """The unread effects that this card gives something to work on."""
    treasures = bool(
        card.treasures_on_entry or card.treasures_on_attack or card.treasure_on_tap or card.on_damage
    )
    worked = {
        Unread.TOKENS: treasures,
        Unread.TRIGGERS: treasures or card.entry_trigger or card.dies_tutor is not None,
        Unread.MANA: card.mana is not None or card.land is not None,
        Unread.COPY: _worth_copying(card),
        Unread.COPY_LAND: card.land is not None,
    }
    return frozenset(effect for effect, found in worked.items() if found)


def flag_unread(records: Iterable[GoldfishCard]) -> list[GoldfishCard]:
    """The cards of a deck, one record each, those with an effect that is not read marked not modelled when
    another card of the deck has what the effect works on: Treasures, a trigger the goldfish counts, mana, a
    permanent worth copying, or a land. Cards flagged for another deck are flagged again for this one."""
    cards = list(records)
    worked = Counter(effect for card in cards for effect in _worked_on(card))
    flagged: list[GoldfishCard] = []
    for card in cards:
        own = _worked_on(card)
        relevant = any(worked[effect] - (effect in own) > 0 for effect in card.unread)
        read = card.modelled or card.flagged
        flagged.append(replace(card, modelled=read and not relevant, flagged=read and relevant))
    return flagged


def _adds_mana(line: _ManaLine) -> bool:
    """Whether a mana line may make more mana than it costs: a filter that pays as much as it makes (Three
    Tree Mascot's {1}) adds none. A line whose cost or amount is not read counts."""
    paid = _paid(_FILTER_COST.sub("{1}", line.cost))
    return line.varies or paid is None or _net_mana(paid, line.what, frozenset()) is not None


# A card that searches a library or looks through the top of yours, as the Tagger's tutors do; one that only
# changes how opponents search (Opposition Agent) does neither.
_USES_LIBRARY = re.compile(
    r"\bsearch(?:es)? (?:your|their) library|look at the top \w+ cards? of your library"
)


def _matters(text: str, roles: frozenset[Role], search: _Search | None) -> bool:
    """Whether the card is about what the goldfish measures: the Tagger's ramp (for its owner), draw or tutor
    role (for a card that searches or looks through a library), mana in its text, or a search for lands."""
    ramp = Role.RAMP in roles and _FOR_OPPONENT.search(text) is None
    draws = Role.DRAW in roles and re.search(r"\bdraws?\b", text) is not None
    tutors = Role.TUTOR in roles and (search is not None or _USES_LIBRARY.search(text) is not None)
    mana = any(_adds_mana(line) for line in _abilities(text))
    return ramp or draws or tutors or mana or _land_words(search) is not None


# A sentence that reduces a cost: for other spells (Jet Medallion) or this one (Ghalta).
_COST_REDUCER = re.compile(r"costs? (?:\{[^}]+\})+ less|less to cast")
# A sentence that only makes an ability cheaper (Dwarven Mauler's equip): the goldfish pays no ability costs.
_ABILITY_REDUCER = re.compile(r"^(?!.*less to cast).*less to activate")
_SENTENCE_BREAK = re.compile(r"(?<=\.) |\n")
# "Creature spells you cast cost {2} less to cast." The kind before "spells" is optional ("Spells cost ...").
_STATIC_REDUCER = re.compile(
    r"^(?:(?P<what>[a-z -]+?) )?spells? (?:you cast )?costs? \{(?P<n>\d+)\} less to cast\.?$"
)
_THATS_REDUCER = re.compile(
    r"^each spell you cast that's (?P<what>[a-z -]+?) costs? \{(?P<n>\d+)\} less to cast\.?$"
)
_ALTERNATIVES = re.compile(r"\s+(?:and|or)\s+")
_SPELL_COLOURS = {word: colour for word, colour in _COLOUR_WORDS.items() if colour in _ALL_COLOURS}
# Words before "spells" that name no subtype and that the reading does not follow.
_NOT_SUBTYPES = frozenset(
    {"this", "historic", "legendary", "multicolored", "monocolored", "colorless", "permanent"}
)
_SELF_REDUCTION = re.compile(r"^this spell costs \{(?P<n>\d+|x)\} less to cast(?P<rest>.*?)\.?$")
_MEASURES = (
    (re.compile(r"^, where x is the greatest power among creatures you control$"), Measure.GREATEST_POWER),
    (re.compile(r"^, where x is the total power of creatures you control$"), Measure.TOTAL_POWER),
    (re.compile(r"^, where x is the number of creatures you control$"), Measure.CREATURES),
)
_GREATEST_VALUE = re.compile(r"^, where x is the greatest mana value among (?P<kind>[a-z]+?)s you control$")
_POWER_CONDITION = re.compile(r" if you control a creature with power (?P<power>\d+) or greater$")
_PER_CREATURE = re.compile(r" for each creature (?:on the battlefield|you control)$")
_SHRINK = re.compile(r"^creatures you control get -(\d+)/-\1\.?$", re.MULTILINE)


class _Reductions(NamedTuple):
    reducers: tuple[Reducer, ...] = ()
    discount: Discount | None = None
    shrink: int = 0


def _reductions(text: str, card: GoldfishCard) -> _Reductions | None:
    """The cost reductions a card's text gives, or None when a sentence reducing a cost is not followed. The
    reduction of a Class sits behind a level that nothing here pays for."""
    reducers: list[Reducer] = []
    discount: Discount | None = None
    for sentence in _SENTENCE_BREAK.split(text):
        if not _COST_REDUCER.search(sentence) or _ABILITY_REDUCER.search(sentence):
            continue
        static = _static_reducer(sentence)
        own = _self_discount(sentence)
        if "class" in card.subtypes or (static is None and own is None) or (own is not None and discount):
            return None
        if static is not None:
            reducers.append(static)
        discount = own or discount
    shrink = _SHRINK.search(text)
    return _Reductions(tuple(reducers), discount, int(shrink.group(1)) if shrink else 0)


def _static_reducer(sentence: str) -> Reducer | None:
    found = _STATIC_REDUCER.match(sentence) or _THATS_REDUCER.match(sentence)
    if found is None:
        return None
    spells = _spell_kinds(found.group("what") or "")
    return Reducer(spells, int(found.group("n"))) if spells else None


def _spell_kinds(what: str) -> tuple[SpellKind, ...] | None:
    """The kinds of spell a phrase names ("instant and sorcery", "red or green", "creature"), none for a word
    the reading does not follow; no words name every spell."""
    kinds: list[SpellKind] = []
    for words in _ALTERNATIVES.split(what) if what else [""]:
        kind = _spell_kind(words.split())
        if kind is None:
            return None
        kinds.append(kind)
    return tuple(kinds)


def _spell_kind(words: Iterable[str]) -> SpellKind | None:
    kinds: set[CardKind] = set()
    not_kinds: set[CardKind] = set()
    colours: set[str] = set()
    subtype = ""
    for word in words:
        card_kind = _KIND_WORDS.get(word)
        if card_kind is not None and card_kind is not CardKind.LAND:
            kinds.add(card_kind)
        elif word.startswith("non") and _KIND_WORDS.get(word[3:]) not in {None, CardKind.LAND}:
            not_kinds.add(_KIND_WORDS[word[3:]])
        elif word in _SPELL_COLOURS:
            colours.add(_SPELL_COLOURS[word])
        elif word.isalpha() and word not in _NOT_SUBTYPES and not word.startswith("non") and not subtype:
            subtype = word
        else:
            return None
    return SpellKind(frozenset(kinds), frozenset(not_kinds), frozenset(colours), subtype)


def _self_discount(sentence: str) -> Discount | None:
    found = _SELF_REDUCTION.match(sentence)
    if found is None:
        return None
    amount, rest = found.group("n"), found.group("rest")
    if amount == "x":
        for pattern, measure in _MEASURES:
            if pattern.match(rest):
                return Discount(measure)
        value = _GREATEST_VALUE.match(rest)
        return Discount(Measure.GREATEST_VALUE, subtype=value.group("kind")) if value else None
    step = int(amount)
    power = _POWER_CONDITION.match(rest)
    if power is not None:
        return Discount(Measure.GREATEST_POWER, step, at_least=int(power.group("power")))
    return Discount(Measure.CREATURES, step) if _PER_CREATURE.match(rest) else None
