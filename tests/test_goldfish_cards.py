"""The goldfish deck compiler (ADR 0054): rules texts are real, copied from the card data."""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from commander.carddb import CardInfo, CardMana, Role
from commander.goldfish import simulate
from commander.goldfish_cards import (
    ActivatedTutor,
    CardKind,
    Draw,
    EntryCost,
    GoldfishCard,
    GoldfishDeck,
    LandDestination,
    LandFace,
    LandRamp,
    ManaAbility,
    ManaNeed,
    Tutor,
    TutorDestination,
    compile_card,
    compile_deck,
)
from commander.models import Deck
from commander.spellbook import ComboDetail, ComboPiece, parse_details
from test_manabase import by_name, deck_of

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass(frozen=True, slots=True)
class Real:
    mana: CardMana
    identity: str
    roles: frozenset[Role]
    tags: frozenset[str]
    power: str | None = None


def real(
    name: str,
    cost: str,
    mana_value: float,
    type_line: str,
    text: str,
    *,
    produced: str = "",
    layout: str = "normal",
    identity: str = "",
    roles: Iterable[Role] = (),
    tags: Iterable[str] = (),
    power: str | None = None,
) -> Real:
    mana = CardMana(f"id-{name}", name, cost, mana_value, type_line, text, produced, "common", layout)
    return Real(mana, identity, frozenset(roles), frozenset(tags), power)


RAMP, DRAW, TUTOR = Role.RAMP, Role.DRAW, Role.TUTOR
ALL = "WUBRG"

LEGION = real(
    "Sliver Legion",
    "{W}{U}{B}{R}{G}",
    5,
    "Legendary Creature — Sliver",
    "All Sliver creatures get +1/+1 for each other Sliver on the battlefield.",
    identity=ALL,
)
MAGDA = real(
    "Magda, Brazen Outlaw",
    "{1}{R}",
    2,
    "Legendary Creature — Dwarf Berserker",
    "Other Dwarves you control get +1/+0.\nWhenever a Dwarf you control becomes tapped, create a Treasure "
    "token.\n"
    "Sacrifice five Treasures: Search your library for an artifact or Dragon card, put that card onto the "
    "battlefield, then shuffle.",
    identity="R",
    roles=(RAMP, TUTOR),
)

FOREST = real("Forest", "", 0, "Basic Land — Forest", "({T}: Add {G}.)", produced="G", identity="G")
TRANQUIL_COVE = real(
    "Tranquil Cove",
    "",
    0,
    "Land",
    "This land enters tapped.\nWhen this land enters, you gain 1 life.\n{T}: Add {W} or {U}.",
    produced="WU",
)
FLOODED_STRAND = real(
    "Flooded Strand",
    "",
    0,
    "Land",
    "{T}, Pay 1 life, Sacrifice this land: Search your library for a Plains or Island card, put it onto the "
    "battlefield, then shuffle.",
)
EVOLVING_WILDS = real(
    "Evolving Wilds",
    "",
    0,
    "Land",
    "{T}, Sacrifice this land: Search your library for a basic land card, put it onto the battlefield "
    "tapped, "
    "then shuffle.",
)
COMMAND_TOWER = real(
    "Command Tower",
    "",
    0,
    "Land",
    "{T}: Add one mana of any color in your commander's color identity.",
    produced="WUBRG",
)
ANCIENT_TOMB = real(
    "Ancient Tomb",
    "",
    0,
    "Land",
    "{T}: Add {C}{C}. This land deals 2 damage to you.",
    produced="C",
    roles=(RAMP,),
)
CABAL_COFFERS = real(
    "Cabal Coffers",
    "",
    0,
    "Land",
    "{2}, {T}: Add {B} for each Swamp you control.",
    produced="B",
    roles=(RAMP,),
)
KAROO = real(
    "Karoo",
    "",
    0,
    "Land",
    "This land enters tapped.\nWhen this land enters, sacrifice it unless you return an untapped Plains you "
    "control to its owner's hand.\n{T}: Add {C}{W}.",
    produced="WC",
)
PATHWAY = real(
    "Blightstep Pathway // Searstep Pathway",
    "",
    0,
    "Land // Land",
    "{T}: Add {B}.\n//\n{T}: Add {R}.",
    produced="BR",
    layout="modal_dfc",
)
SHATTERSKULL = real(
    "Shatterskull Smashing // Shatterskull, the Hammer Pass",
    "{X}{R}{R}",
    2,
    "Sorcery // Land",
    "Shatterskull Smashing deals X damage divided as you choose among up to two target creatures and/or "
    "planeswalkers. If X is 6 or more, Shatterskull Smashing deals twice X damage divided as you choose "
    "among "
    "them instead.\n//\nAs this land enters, you may pay 3 life. If you don't, it enters tapped.\n{T}: Add "
    "{R}.",
    produced="R",
    layout="modal_dfc",
    roles=(Role.REMOVAL,),
)
SEA_GATE = real(
    "Sea Gate Restoration // Sea Gate, Reborn",
    "{4}{U}{U}{U}",
    7,
    "Sorcery // Land",
    "Draw cards equal to the number of cards in your hand plus one. You have no maximum hand size for the "
    "rest of the game.\n//\nAs this land enters, you may pay 3 life. If you don't, it enters tapped.\n"
    "{T}: Add {U}.",
    produced="U",
    layout="modal_dfc",
    roles=(DRAW,),
)

SOL_RING = real("Sol Ring", "{1}", 1, "Artifact", "{T}: Add {C}{C}.", produced="C", roles=(RAMP,))
JUNGLE_SHRINE = real(
    "Jungle Shrine",
    "",
    0,
    "Land",
    "This land enters tapped.\n{T}: Add {R}, {G}, or {W}.",
    produced="RGW",
)
NOBLE_HIERARCH = real(
    "Noble Hierarch",
    "{G}",
    1,
    "Creature — Human Druid",
    "Exalted (Whenever a creature you control attacks alone, that creature gets +1/+1 until end of "
    "turn.)\n{T}: Add {G}, {W}, or {U}.",
    produced="GWU",
    roles=(RAMP,),
)
ARCANE_SIGNET = real(
    "Arcane Signet",
    "{2}",
    2,
    "Artifact",
    "{T}: Add one mana of any color in your commander's color identity.",
    produced="WUBRG",
    roles=(RAMP,),
)
LLANOWAR = real(
    "Llanowar Elves", "{G}", 1, "Creature — Elf Druid", "{T}: Add {G}.", produced="G", roles=(RAMP,)
)
DARK_RITUAL = real("Dark Ritual", "{B}", 1, "Instant", "Add {B}{B}{B}.", produced="B", roles=(RAMP,))
CABAL_RITUAL = real(
    "Cabal Ritual",
    "{1}{B}",
    2,
    "Instant",
    "Add {B}{B}{B}.\nThreshold — Add {B}{B}{B}{B}{B} instead if there are seven or more cards in your "
    "graveyard.",
    produced="B",
    roles=(RAMP,),
)
LOTUS_PETAL = real(
    "Lotus Petal",
    "{0}",
    0,
    "Artifact",
    "{T}, Sacrifice this artifact: Add one mana of any color.",
    produced="WUBRG",
    roles=(RAMP,),
)
AZORIUS_SIGNET = real(
    "Azorius Signet", "{2}", 2, "Artifact", "{1}, {T}: Add {W}{U}.", produced="WU", roles=(RAMP,)
)
TALISMAN = real(
    "Talisman of Dominance",
    "{2}",
    2,
    "Artifact",
    "{T}: Add {C}.\n{T}: Add {U} or {B}. This artifact deals 1 damage to you.",
    produced="UBC",
    roles=(RAMP,),
)
MANA_VAULT = real(
    "Mana Vault",
    "{1}",
    1,
    "Artifact",
    "This artifact doesn't untap during your untap step.\nAt the beginning of your upkeep, you may pay {4}. "
    "If you do, untap this artifact.\nAt the beginning of your draw step, if this artifact is tapped, it "
    "deals "
    "1 damage to you.\n{T}: Add {C}{C}{C}.",
    produced="C",
    roles=(RAMP,),
)
MIND_STONE = real(
    "Mind Stone",
    "{2}",
    2,
    "Artifact",
    "{T}: Add {C}.\n{1}, {T}, Sacrifice this artifact: Draw a card.",
    produced="C",
    roles=(DRAW, RAMP),
)

CULTIVATE = real(
    "Cultivate",
    "{2}{G}",
    3,
    "Sorcery",
    "Search your library for up to two basic land cards, reveal those cards, put one onto the battlefield "
    "tapped and the other into your hand, then shuffle.",
    roles=(RAMP, TUTOR),
)
RAMPANT_GROWTH = real(
    "Rampant Growth",
    "{1}{G}",
    2,
    "Sorcery",
    "Search your library for a basic land card, put that card onto the battlefield tapped, then shuffle.",
    roles=(RAMP,),
)
NATURES_LORE = real(
    "Nature's Lore",
    "{1}{G}",
    2,
    "Sorcery",
    "Search your library for a Forest card, put that card onto the battlefield, then shuffle.",
    roles=(RAMP,),
)
FARSEEK = real(
    "Farseek",
    "{1}{G}",
    2,
    "Sorcery",
    "Search your library for a Plains, Island, Swamp, or Mountain card, put it onto the battlefield tapped, "
    "then shuffle.",
    roles=(RAMP,),
)
SKYSHROUD_CLAIM = real(
    "Skyshroud Claim",
    "{3}{G}",
    4,
    "Sorcery",
    "Search your library for up to two Forest cards, put them onto the battlefield, then shuffle.",
    roles=(RAMP,),
)
WOOD_ELVES = real(
    "Wood Elves",
    "{2}{G}",
    3,
    "Creature — Elf Scout",
    "When this creature enters, search your library for a Forest card, put that card onto the battlefield, "
    "then shuffle.",
    roles=(RAMP,),
)
SAKURA_TRIBE_ELDER = real(
    "Sakura-Tribe Elder",
    "{1}{G}",
    2,
    "Creature — Snake Shaman",
    "Sacrifice this creature: Search your library for a basic land card, put that card onto the battlefield "
    "tapped, then shuffle.",
    roles=(RAMP,),
)
WAYFARERS_BAUBLE = real(
    "Wayfarer's Bauble",
    "{1}",
    1,
    "Artifact",
    "{2}, {T}, Sacrifice this artifact: Search your library for a basic land card, put that card onto the "
    "battlefield tapped, then shuffle.",
    roles=(RAMP,),
)
BURNISHED_HART = real(
    "Burnished Hart",
    "{3}",
    3,
    "Artifact Creature — Elk",
    "{3}, Sacrifice this creature: Search your library for up to two basic land cards, put them onto the "
    "battlefield tapped, then shuffle.",
    roles=(RAMP,),
)

DEMONIC_TUTOR = real(
    "Demonic Tutor",
    "{1}{B}",
    2,
    "Sorcery",
    "Search your library for a card, put that card into your hand, then shuffle.",
    roles=(TUTOR,),
)
VAMPIRIC_TUTOR = real(
    "Vampiric Tutor",
    "{B}",
    1,
    "Instant",
    "Search your library for a card, then shuffle and put that card on top. You lose 2 life.",
    roles=(TUTOR,),
)
WORLDLY_TUTOR = real(
    "Worldly Tutor",
    "{G}",
    1,
    "Instant",
    "Search your library for a creature card, reveal it, then shuffle and put the card on top.",
    roles=(TUTOR,),
)
ENLIGHTENED_TUTOR = real(
    "Enlightened Tutor",
    "{W}",
    1,
    "Instant",
    "Search your library for an artifact or enchantment card, reveal it, then shuffle and put that card on "
    "top.",
    roles=(TUTOR,),
)
GREEN_SUNS_ZENITH = real(
    "Green Sun's Zenith",
    "{X}{G}",
    1,
    "Sorcery",
    "Search your library for a green creature card with mana value X or less, put it onto the battlefield, "
    "then shuffle. Shuffle Green Sun's Zenith into its owner's library.",
    roles=(Role.RECURSION, TUTOR),
)
RECRUITER_OF_THE_GUARD = real(
    "Recruiter of the Guard",
    "{2}{W}",
    3,
    "Creature — Human Soldier",
    "When this creature enters, you may search your library for a creature card with toughness 2 or less, "
    "reveal it, put it into your hand, then shuffle.",
    roles=(TUTOR,),
)
IMPERIAL_RECRUITER = real(
    "Imperial Recruiter",
    "{2}{R}",
    3,
    "Creature — Human Advisor",
    "When this creature enters, search your library for a creature card with power 2 or less, reveal it, "
    "put it into your hand, then shuffle.",
    roles=(TUTOR,),
    power="1",
)
TROPHY_MAGE = real(
    "Trophy Mage",
    "{2}{U}",
    3,
    "Creature — Human Wizard",
    "When this creature enters, you may search your library for an artifact card with mana value 3, "
    "reveal it, put it into your hand, then shuffle.",
    roles=(TUTOR,),
)
FIERCE_EMPATH = real(
    "Fierce Empath",
    "{2}{G}",
    3,
    "Creature — Elf Druid",
    "When this creature enters, you may search your library for a creature card with mana value 6 or "
    "greater, reveal it, put it into your hand, then shuffle.",
    roles=(TUTOR,),
)
TIME_OF_NEED = real(
    "Time of Need",
    "{1}{G}",
    2,
    "Sorcery",
    "Search your library for a legendary creature card, reveal it, put it into your hand, then shuffle.",
    roles=(TUTOR,),
)
FINALE = real(
    "Finale of Devastation",
    "{X}{G}{G}",
    2,
    "Sorcery",
    "Search your library and/or graveyard for a creature card with mana value X or less and put it onto the "
    "battlefield. If you search your library this way, shuffle. If X is 10 or more, creatures you control "
    "get "
    "+X/+X and gain haste until end of turn.",
    roles=(Role.RECURSION, TUTOR),
)
SURVIVAL = real(
    "Survival of the Fittest",
    "{1}{G}",
    2,
    "Enchantment",
    "{G}, Discard a creature card: Search your library for a creature card, reveal that card, put it into "
    "your hand, then shuffle.",
    roles=(TUTOR,),
)
ENTOMB = real(
    "Entomb",
    "{B}",
    1,
    "Instant",
    "Search your library for a card, put that card into your graveyard, then shuffle.",
    roles=(TUTOR,),
)
NATURAL_ORDER = real(
    "Natural Order",
    "{2}{G}{G}",
    4,
    "Sorcery",
    "As an additional cost to cast this spell, sacrifice a green creature.\nSearch your library for a green "
    "creature card, put it onto the battlefield, then shuffle.",
    roles=(TUTOR,),
)

HARMONIZE = real("Harmonize", "{2}{G}{G}", 4, "Sorcery", "Draw three cards.", roles=(DRAW,))
NIGHTS_WHISPER = real(
    "Night's Whisper", "{1}{B}", 2, "Sorcery", "You draw two cards and lose 2 life.", roles=(DRAW,)
)
RHYSTIC_STUDY = real(
    "Rhystic Study",
    "{2}{U}",
    3,
    "Enchantment",
    "Whenever an opponent casts a spell, you may draw a card unless that player pays {1}.",
    roles=(DRAW,),
)
PHYREXIAN_ARENA = real(
    "Phyrexian Arena",
    "{1}{B}{B}",
    3,
    "Enchantment",
    "At the beginning of your upkeep, you draw a card and lose 1 life.",
    roles=(DRAW,),
)
JAYEMDAE_TOME = real("Jayemdae Tome", "{4}", 4, "Artifact — Book", "{4}, {T}: Draw a card.", roles=(DRAW,))
MERFOLK_LOOTER = real(
    "Merfolk Looter",
    "{1}{U}",
    2,
    "Creature — Merfolk Rogue",
    "{T}: Draw a card, then discard a card.",
    roles=(DRAW,),
)
MULLDRIFTER = real(
    "Mulldrifter",
    "{4}{U}",
    5,
    "Creature — Elemental",
    "Flying\nWhen this creature enters, draw two cards.\nEvoke {2}{U} (You may cast this spell for its evoke "
    "cost. If you do, it's sacrificed when it enters.)",
    roles=(DRAW,),
)
KENRITH = real(
    "Kenrith, the Returned King",
    "{4}{W}",
    5,
    "Legendary Creature — Human Noble",
    "{R}: All creatures gain trample and haste until end of turn.\n{1}{G}: Put a +1/+1 counter on target "
    "creature.\n{2}{W}: Target player gains 5 life.\n{3}{U}: Target player draws a card.\n{4}{B}: Put target "
    "creature card from a graveyard onto the battlefield under its owner's control.",
    roles=(DRAW, Role.RECURSION),
)
FIRE_ICE = real(
    "Fire // Ice",
    "{1}{R} // {1}{U}",
    4,
    "Instant // Instant",
    "Fire deals 2 damage divided as you choose among one or two targets.\n//\nTap target permanent.\n"
    "Draw a card.",
    layout="split",
    roles=(DRAW, Role.REMOVAL),
)
BONECRUSHER = real(
    "Bonecrusher Giant // Stomp",
    "{2}{R} // {1}{R}",
    3,
    "Creature — Giant // Instant — Adventure",
    "Whenever this creature becomes the target of a spell, this creature deals 2 damage to that spell's "
    "controller.\n//\nDamage can't be prevented this turn. Stomp deals 2 damage to any target.",
    layout="adventure",
    roles=(Role.REMOVAL,),
)

CRATERHOOF = real(
    "Craterhoof Behemoth",
    "{5}{G}{G}{G}",
    8,
    "Creature — Beast",
    "Haste\nWhen this creature enters, creatures you control gain trample and get +X/+X until end of turn, "
    "where X is the number of creatures you control.",
    tags=("overrun",),
)
THASSAS_ORACLE = real(
    "Thassa's Oracle",
    "{U}{U}",
    2,
    "Creature — Merfolk Wizard",
    "When this creature enters, look at the top X cards of your library, where X is your devotion to blue. "
    "Put up to one of them on top of your library and the rest on the bottom of your library in a random "
    "order. "
    "If X is greater than or equal to the number of cards in your library, you win the game.",
    tags=("alternate-win-condition",),
)
BATTERED_GOLEM = real(
    "Battered Golem",
    "{3}",
    3,
    "Artifact Creature — Golem",
    "This creature doesn't untap during your untap step.\nWhenever an artifact enters, you may untap this "
    "creature.",
)
MASKWOOD_NEXUS = real(
    "Maskwood Nexus",
    "{4}",
    4,
    "Artifact",
    "Creatures you control are every creature type. The same is true for creature spells you control and "
    "creature cards you own that aren't on the battlefield.\n{3}, {T}: Create a 2/2 blue Shapeshifter "
    "creature "
    "token with changeling.",
)
ANIMATE_DEAD = real(
    "Animate Dead",
    "{1}{B}",
    2,
    "Enchantment — Aura",
    "Enchant creature card in a graveyard\nWhen this Aura enters, if it's on the battlefield, it loses "
    '"enchant creature card in a graveyard" and gains "enchant creature put onto the battlefield with this '
    'Aura." Return enchanted creature card to the battlefield under your control and attach this Aura to it. '
    "When this Aura leaves the battlefield, that creature's controller sacrifices it.\nEnchanted creature "
    "gets "
    "-1/-0.",
    roles=(Role.RECURSION,),
)
WORLDGORGER = real(
    "Worldgorger Dragon",
    "{3}{R}{R}{R}",
    6,
    "Creature — Nightmare Dragon",
    "Flying, trample\nWhen this creature enters, exile all other permanents you control.\nWhen this creature "
    "leaves the battlefield, return the exiled cards to the battlefield under their owners' control.",
)

SCULPTOR = real(
    "Etherium Sculptor",
    "{1}{U}",
    2,
    "Artifact Creature — Vedalken Artificer",
    "Artifact spells you cast cost {1} less to cast.",
)
MEDALLION = real("Sapphire Medallion", "{2}", 2, "Artifact", "Blue spells you cast cost {1} less to cast.")
FIREBLAST = real(
    "Fireblast",
    "{4}{R}{R}",
    6,
    "Instant",
    "You may sacrifice two Mountains rather than pay this spell's mana cost.\nFireblast deals 4 damage to "
    "any "
    "target.",
    roles=(Role.REMOVAL,),
    tags=("alternative-cost",),
)
GRIZZLY_BEARS = real("Grizzly Bears", "{1}{G}", 2, "Creature — Bear", "")

SMOTHERING_TITHE = real(
    "Smothering Tithe",
    "{3}{W}",
    4,
    "Enchantment",
    "Whenever an opponent draws a card, that player may pay {2}. If the player doesn't, you create a "
    "Treasure token.",
    produced="WUBRG",
    roles=(RAMP,),
)
PRIEST_OF_TITANIA = real(
    "Priest of Titania",
    "{1}{G}",
    2,
    "Creature — Elf Druid",
    "{T}: Add {G} for each Elf on the battlefield.",
    produced="G",
    roles=(RAMP,),
)
DOCKSIDE = real(
    "Dockside Extortionist",
    "{1}{R}",
    2,
    "Creature — Goblin Pirate",
    "When this creature enters, create X Treasure tokens, where X is the number of artifacts and "
    "enchantments "
    "your opponents control.",
    produced="WUBRG",
    roles=(RAMP,),
)
JEWELED_LOTUS = real(
    "Jeweled Lotus",
    "{0}",
    0,
    "Artifact",
    "{T}, Sacrifice this artifact: Add three mana of any one color. Spend this mana only to cast your "
    "commander.",
    produced="WUBRG",
    roles=(RAMP,),
)
CHROME_MOX = real(
    "Chrome Mox",
    "{0}",
    0,
    "Artifact",
    "Imprint — When this artifact enters, you may exile a nonartifact, nonland card from your hand.\n{T}: "
    "Add "
    "one mana of any of the exiled card's colors.",
    produced="WUBRG",
    roles=(RAMP,),
)
MOX_DIAMOND = real(
    "Mox Diamond",
    "{0}",
    0,
    "Artifact",
    "If this artifact would enter, you may discard a land card instead. If you do, put this artifact onto "
    "the "
    "battlefield. If you don't, put it into its owner's graveyard.\n{T}: Add one mana of any color.",
    produced="WUBRG",
    roles=(RAMP,),
)
ORACLE_OF_MUL_DAYA = real(
    "Oracle of Mul Daya",
    "{3}{G}",
    4,
    "Creature — Elf Shaman",
    "You may play an additional land on each of your turns.\nPlay with the top card of your library "
    "revealed.\nYou may play lands from the top of your library.",
    roles=(RAMP,),
)


WILD_GROWTH = real(
    "Wild Growth",
    "{G}",
    1,
    "Enchantment — Aura",
    "Enchant land\nWhenever enchanted land is tapped for mana, its controller adds an additional {G}.",
    produced="G",
    roles=(RAMP,),
)
UTOPIA_SPRAWL = real(
    "Utopia Sprawl",
    "{G}",
    1,
    "Enchantment — Aura",
    "Enchant Forest\nAs this Aura enters, choose a color.\nWhenever enchanted Forest is tapped for mana, its "
    "controller adds an additional one mana of the chosen color.",
    produced="WUBRG",
    roles=(RAMP,),
)
FERTILE_GROUND = real(
    "Fertile Ground",
    "{1}{G}",
    2,
    "Enchantment — Aura",
    "Enchant land\nWhenever enchanted land is tapped for mana, its controller adds an additional one mana of "
    "any color.",
    produced="WUBRG",
    roles=(RAMP,),
)
ELVISH_SPIRIT_GUIDE = real(
    "Elvish Spirit Guide",
    "{2}{G}",
    3,
    "Creature — Elf Spirit",
    "Exile this card from your hand: Add {G}.",
    produced="G",
    roles=(RAMP,),
)
EXPLORATION = real(
    "Exploration",
    "{G}",
    1,
    "Enchantment",
    "You may play an additional land on each of your turns.",
    roles=(RAMP,),
)
AZUSA = real(
    "Azusa, Lost but Seeking",
    "{2}{G}",
    3,
    "Legendary Creature — Human Monk",
    "You may play two additional lands on each of your turns.",
    roles=(RAMP,),
)
SUMMONERS_PACT = real(
    "Summoner's Pact",
    "{0}",
    0,
    "Instant",
    "Search your library for a green creature card, reveal it, put it into your hand, then shuffle.\n"
    "At the beginning of your next upkeep, pay {2}{G}{G}. If you don't, you lose the game.",
    roles=(TUTOR,),
    tags=("manaless-value",),
)
SLAUGHTER_PACT = real(
    "Slaughter Pact",
    "{0}",
    0,
    "Instant",
    "Destroy target nonblack creature.\nAt the beginning of your next upkeep, pay {2}{B}. If you don't, you "
    "lose the game.",
    roles=(Role.REMOVAL,),
    tags=("manaless-value",),
)
DIABOLIC_INTENT = real(
    "Diabolic Intent",
    "{1}{B}",
    2,
    "Sorcery",
    "As an additional cost to cast this spell, sacrifice a creature.\nSearch your library for a card, put "
    "that "
    "card into your hand, then shuffle.",
    roles=(TUTOR,),
)
ELDRITCH_EVOLUTION = real(
    "Eldritch Evolution",
    "{1}{G}{G}",
    3,
    "Sorcery",
    "As an additional cost to cast this spell, sacrifice a creature.\nSearch your library for a creature "
    "card "
    "with mana value X or less, where X is 2 plus the sacrificed creature's mana value. Put that card onto "
    "the "
    "battlefield, then shuffle. Exile Eldritch Evolution.",
    roles=(TUTOR,),
)
CULLING_THE_WEAK = real(
    "Culling the Weak",
    "{B}",
    1,
    "Instant",
    "As an additional cost to cast this spell, sacrifice a creature.\nAdd {B}{B}{B}{B}.",
    produced="B",
    roles=(RAMP,),
)
CULLING_RITUAL = real(
    "Culling Ritual",
    "{2}{B}{G}",
    4,
    "Sorcery",
    "Destroy each nonland permanent with mana value 2 or less. Add {B} or {G} for each permanent destroyed "
    "this way.",
    produced="BG",
    roles=(Role.BOARD_WIPE, RAMP, Role.REMOVAL),
)
CROP_ROTATION = real(
    "Crop Rotation",
    "{G}",
    1,
    "Instant",
    "As an additional cost to cast this spell, sacrifice a land.\nSearch your library for a land card, put "
    "that "
    "card onto the battlefield, then shuffle.",
)
TINKER = real(
    "Tinker",
    "{2}{U}",
    3,
    "Sorcery",
    "As an additional cost to cast this spell, sacrifice an artifact.\nSearch your library for an artifact "
    "card, put it onto the battlefield, then shuffle.",
    roles=(TUTOR,),
)
PATH_TO_EXILE = real(
    "Path to Exile",
    "{W}",
    1,
    "Instant",
    "Exile target creature. Its controller may search their library for a basic land card, put that card "
    "onto "
    "the battlefield tapped, then shuffle.",
    roles=(RAMP, Role.REMOVAL),
)
FORCE_OF_WILL = real(
    "Force of Will",
    "{3}{U}{U}",
    5,
    "Instant",
    "You may pay 1 life and exile a blue card from your hand rather than pay this spell's mana cost.\n"
    "Counter target spell.",
    roles=(Role.COUNTERSPELL,),
)
JET_MEDALLION = real("Jet Medallion", "{2}", 2, "Artifact", "Black spells you cast cost {1} less to cast.")
DELIGHTED_HALFLING = real(
    "Delighted Halfling",
    "{G}",
    1,
    "Creature — Halfling Citizen",
    "{T}: Add {C}.\n{T}: Add one mana of any color. Spend this mana only to cast a legendary spell, and that "
    "spell can't be countered.",
    produced="WUBRGC",
    roles=(RAMP,),
)
CAVERN_OF_SOULS = real(
    "Cavern of Souls",
    "",
    0,
    "Land",
    "As this land enters, choose a creature type.\n{T}: Add {C}.\n{T}: Add one mana of any color. Spend this "
    "mana only to cast a creature spell of the chosen type, and that spell can't be countered.",
    produced="WUBRGC",
)
BOSEIJU = real(
    "Boseiju, Who Endures",
    "",
    0,
    "Legendary Land",
    "{T}: Add {G}.\nChannel — {1}{G}, Discard this card: Destroy target artifact, enchantment, or nonbasic "
    "land an opponent controls. That player may search their library for a land card with a basic land type, "
    "put it onto the battlefield, then shuffle. This ability costs {1} less to activate for each legendary "
    "creature you control.",
    produced="G",
    roles=(Role.REMOVAL,),
)


def info_of(card: Real) -> CardInfo:
    mana = card.mana
    return CardInfo(
        oracle_id=mana.oracle_id,
        name=mana.name,
        mana_cost=mana.mana_cost,
        cmc=mana.cmc,
        type_line=mana.type_line,
        color_identity=card.identity,
        game_changer=False,
        commander_legal=True,
        cheapest_cents=None,
        edhrec_rank=None,
        roles=card.roles,
    )


def compile_for(
    deck: Deck,
    cards: Iterable[Real],
    *,
    combos: tuple[ComboDetail, ...] = (),
    synergy: Iterable[str] = (),
    added: Iterable[str] = (),
    removed: Iterable[str] = (),
) -> GoldfishDeck:
    known = list(cards)
    return compile_deck(
        deck,
        by_name(*(c.mana for c in known)),
        {c.mana.oracle_id: info_of(c) for c in known},
        {c.mana.oracle_id: c.tags for c in known},
        combos=combos,
        synergy=list(synergy),
        finishers_added=added,
        finishers_removed=removed,
        powers={c.mana.oracle_id: c.power for c in known},
    )


def build(
    *cards: Real,
    commander: Real = LEGION,
    copies: Mapping[str, int] | None = None,
    missing: tuple[str, ...] = (),
    combos: tuple[ComboDetail, ...] = (),
    synergy: Iterable[str] = (),
    added: Iterable[str] = (),
    removed: Iterable[str] = (),
) -> GoldfishDeck:
    """The deck of the commander and these cards, one copy each unless copies says; `missing` names cards the
    deck lists that the card data lacks."""
    rows = [(c.mana.name, (copies or {}).get(c.mana.name, 1)) for c in cards]
    deck = deck_of(*rows, *((name, 1) for name in missing), commander=commander.mana.name)
    return compile_for(
        deck, [commander, *cards], combos=combos, synergy=synergy, added=added, removed=removed
    )


def one(card: Real, *, commander: Real = LEGION) -> GoldfishCard:
    """The record a card compiles to in a deck of just the commander and it."""
    return card_named(build(card, commander=commander), card.mana.name)


def card_named(deck: GoldfishDeck, name: str) -> GoldfishCard:
    return next(c for c in (*deck.cards, *deck.commanders) if c.name == name)


def incoming(
    card: Real,
    *deck_cards: Real,
    commander: Real = LEGION,
    copies: int = 1,
    combos: tuple[ComboDetail, ...] = (),
    synergy: Iterable[str] = (),
    added: Iterable[str] = (),
    removed: Iterable[str] = (),
) -> GoldfishCard:
    """The card compiled for a swap into the deck of the commander and `deck_cards`, which it is not in."""
    deck = deck_of(*((c.mana.name, 1) for c in deck_cards), commander=commander.mana.name)
    known = [commander, *deck_cards, card]
    return compile_card(
        card.mana.name,
        deck,
        by_name(*(c.mana for c in known)),
        {c.mana.oracle_id: info_of(c) for c in known},
        {c.mana.oracle_id: c.tags for c in known},
        copies=copies,
        combos=combos,
        synergy=list(synergy),
        finishers_added=added,
        finishers_removed=removed,
        powers={c.mana.oracle_id: c.power for c in known},
    )


def test_a_card_compiled_for_a_swap_is_the_record_it_has_once_in_the_deck() -> None:
    others = (FOREST, SOL_RING)
    for card in (ARCANE_SIGNET, CULTIVATE, HARMONIZE, DEMONIC_TUTOR):
        assert incoming(card, *others) == card_named(build(*others, card), card.mana.name)


def test_a_card_compiled_for_a_swap_takes_the_copies_it_is_given() -> None:
    assert incoming(HARMONIZE, FOREST, copies=3).copies == 3


def test_a_fetch_land_compiled_for_a_swap_finds_the_typed_lands_of_the_deck() -> None:
    card = incoming(FLOODED_STRAND, WATERY_GRAVE, BREEDING_POOL, commander=GLARB)
    # Plains and Island by type; Watery Grave and Breeding Pool are Islands, so B and G come too, but no W.
    assert card.land == LandFace(frozenset("UBG"), tapped=False)
    assert incoming(FLOODED_STRAND, commander=GLARB).land == LandFace(frozenset("U"), tapped=False)


def test_a_card_compiled_for_a_swap_is_read_in_the_decks_colour_identity() -> None:
    assert incoming(COMMAND_TOWER, commander=MAGDA).land == LandFace(frozenset("R"), tapped=False)


def test_synergy_and_finishers_apply_to_a_card_compiled_for_a_swap() -> None:
    assert incoming(HARMONIZE, synergy=["id-Harmonize"]).synergy
    assert not incoming(HARMONIZE, synergy=["id-Other"]).synergy
    assert incoming(SOL_RING, added=["Sol Ring"]).finisher
    assert not incoming(CRATERHOOF, removed=["Craterhoof Behemoth"]).finisher


def test_a_card_compiled_for_a_swap_is_no_combo_piece_of_a_combo_the_deck_lacks() -> None:
    # The deck's combos are those of its own cards; a combo with the incoming card in it is left out.
    assert not incoming(BATTERED_GOLEM, commander=MAGDA, combos=combo_details()).combo


def test_a_card_the_card_data_lacks_is_a_plain_spell_when_compiled_for_a_swap() -> None:
    deck = deck_of(("Forest", 1), commander=LEGION.mana.name)
    known = [LEGION, FOREST]
    card = compile_card(
        "Unknown Card",
        deck,
        by_name(*(c.mana for c in known)),
        {c.mana.oracle_id: info_of(c) for c in known},
        {},
    )
    assert not card.modelled
    assert card.mana is None
    assert card.name == "Unknown Card"


# --- lands -------------------------------------------------------------------------------------------------


def test_a_basic_land_is_a_free_untapped_land_of_its_colour() -> None:
    deck = build(FOREST, copies={"Forest": 36})
    forest = card_named(deck, "Forest")
    assert forest.copies == 36
    assert forest.mana_value == 0
    assert forest.pips == {}
    assert forest.kinds == {CardKind.LAND}
    assert forest.land == LandFace(frozenset("G"), tapped=False, amount=1)
    assert forest.mana is None
    assert forest.modelled


def test_a_tapped_dual_taps_for_either_colour_and_enters_tapped() -> None:
    assert one(TRANQUIL_COVE).land == LandFace(frozenset("WU"), tapped=True)


def test_a_land_whose_only_condition_is_on_another_line_still_enters_tapped() -> None:
    plaza = real(
        "Gateway Plaza",
        "",
        0,
        "Land — Gate",
        "This land enters tapped.\nWhen this land enters, sacrifice it unless you pay {1}.\n"
        "{T}: Add one mana of any color.",
        produced="WUBRG",
    )
    field = real(
        "Field of the Dead",
        "",
        0,
        "Land",
        "This land enters tapped.\n{T}: Add {C}.\nWhenever this land or another land you control enters, "
        "if you control seven or more lands with different names, create a 2/2 black Zombie creature token.",
        produced="C",
    )
    assert one(plaza).land == LandFace(frozenset("WUBRG"), tapped=True)
    assert one(field).land == LandFace(frozenset("C"), tapped=True)


def test_a_fetch_land_has_the_colours_it_can_find() -> None:
    assert one(FLOODED_STRAND).land == LandFace(frozenset("WU"), tapped=False)
    assert one(EVOLVING_WILDS).land == LandFace(frozenset("WUBRG"), tapped=True)


GLARB = real(
    "Glarb, Calamity's Augur",
    "{2}{B}{G}{U}",
    5,
    "Legendary Creature — Frog Horror",
    "Deathtouch\nYou may look at the top card of your library any time.",
    identity="BGU",
)
WATERY_GRAVE = real(
    "Watery Grave",
    "",
    0,
    "Land — Island Swamp",
    "({T}: Add {U} or {B}.)\nAs this land enters, you may pay 2 life. If you don't, it enters tapped.",
    produced="UB",
    identity="UB",
)
BREEDING_POOL = real(
    "Breeding Pool",
    "",
    0,
    "Land — Forest Island",
    "({T}: Add {G} or {U}.)\nAs this land enters, you may pay 2 life. If you don't, it enters tapped.",
    produced="GU",
    identity="GU",
)
BANT_PANORAMA = real(
    "Bant Panorama",
    "",
    0,
    "Land",
    "{T}: Add {C}.\n{1}, {T}, Sacrifice this land: Search your library for a basic Forest, Plains, or "
    "Island card, put it onto the battlefield tapped, then shuffle.",
    produced="C",
)
WINDSWEPT_HEATH = real(
    "Windswept Heath",
    "",
    0,
    "Land",
    "{T}, Pay 1 life, Sacrifice this land: Search your library for a Forest or Plains card, put it onto the "
    "battlefield, then shuffle.",
)


def test_a_fetch_land_finds_the_typed_lands_of_the_deck_within_the_identity() -> None:
    deck = build(FLOODED_STRAND, WATERY_GRAVE, BREEDING_POOL, commander=GLARB)
    # Plains and Island by type; Watery Grave and Breeding Pool are Islands, so B and G come too, but no W.
    assert card_named(deck, "Flooded Strand").land == LandFace(frozenset("UBG"), tapped=False)
    assert card_named(build(FLOODED_STRAND, commander=GLARB), "Flooded Strand").land == LandFace(
        frozenset("U"), tapped=False
    )


def test_a_fetch_land_finds_only_lands_of_the_types_it_names() -> None:
    deck = build(WINDSWEPT_HEATH, WATERY_GRAVE, BREEDING_POOL, FOREST, commander=GLARB)
    # Forest or Plains: the Forest and Breeding Pool, not the Island-Swamp.
    assert card_named(deck, "Windswept Heath").land == LandFace(frozenset("GU"), tapped=False)


def test_a_panorama_finds_the_basics_of_its_types_and_no_typed_nonbasic_land() -> None:
    deck = build(BANT_PANORAMA, WATERY_GRAVE)
    # Basic Forest, Plains or Island: G, W and U, plus its own colourless; Watery Grave's Swamp adds no B.
    assert card_named(deck, "Bant Panorama").land == LandFace(frozenset("CGWU"), tapped=True)


def test_a_fetch_land_for_any_basic_keeps_its_reading_beside_typed_lands() -> None:
    deck = build(EVOLVING_WILDS, WATERY_GRAVE, commander=GLARB)
    assert card_named(deck, "Evolving Wilds").land == LandFace(frozenset("UBG"), tapped=True)


def test_lands_for_any_colour_are_limited_to_the_commanders_identity() -> None:
    assert one(COMMAND_TOWER).land == LandFace(frozenset("WUBRG"), tapped=False)
    assert one(COMMAND_TOWER, commander=MAGDA).land == LandFace(frozenset("R"), tapped=False)
    # A fetch land finds only what the deck can use.
    assert one(EVOLVING_WILDS, commander=MAGDA).land == LandFace(frozenset("R"), tapped=True)


def test_a_land_making_two_colourless_mana_says_so() -> None:
    tomb = one(ANCIENT_TOMB)
    assert tomb.land == LandFace(frozenset("C"), tapped=False, amount=2)
    assert tomb.modelled


def test_lands_with_variable_or_bounced_mana_are_not_modelled_but_stay_lands() -> None:
    coffers = one(CABAL_COFFERS)
    assert coffers.land == LandFace(frozenset("B"), tapped=False, amount=1)
    assert not coffers.modelled
    karoo = one(KAROO)
    assert karoo.land is not None
    assert (karoo.land.colours, karoo.land.amount) == (frozenset("WC"), 2)
    assert not karoo.modelled


def test_a_land_with_restricted_mana_counts_only_its_unrestricted_mana() -> None:
    cavern = one(CAVERN_OF_SOULS)
    assert cavern.land == LandFace(frozenset("C"), tapped=False)
    assert cavern.modelled


def test_a_land_with_an_unrelated_cost_reduction_is_still_a_modelled_land() -> None:
    boseiju = one(BOSEIJU)
    assert boseiju.land == LandFace(frozenset("G"), tapped=False)
    assert boseiju.modelled


def test_a_pathway_is_a_land_that_can_be_either_face() -> None:
    pathway = one(PATHWAY)
    assert pathway.land == LandFace(frozenset("BR"), tapped=False)
    assert pathway.mana_value == 0
    assert pathway.kinds == {CardKind.LAND}


def test_a_modal_card_with_a_land_back_is_its_spell_cost_and_its_land_face() -> None:
    smashing = one(SHATTERSKULL)
    assert smashing.mana_value == 2
    assert smashing.pips == {"R": 2}
    assert smashing.x_cost
    assert smashing.kinds == {CardKind.SORCERY}
    assert smashing.land == LandFace(frozenset("R"), tapped=False)
    assert smashing.modelled


def land(name: str, type_line: str, text: str, produced: str, *, roles: Iterable[Role] = ()) -> Real:
    return real(name, "", 0, type_line, text, produced=produced, roles=roles)


# Lands as the card data has them, produced mana included.
CRYSTAL_VEIN = land("Crystal Vein", "Land", "{T}: Add {C}.\n{T}, Sacrifice this land: Add {C}{C}.", "C")
CITY_OF_TRAITORS = land(
    "City of Traitors", "Land", "When you play another land, sacrifice this land.\n{T}: Add {C}{C}.", "C"
)
GEMSTONE_CAVERNS = land(
    "Gemstone Caverns",
    "Legendary Land",
    "If this card is in your opening hand and you're not the starting player, you may begin the game with "
    "Gemstone Caverns on the battlefield with a luck counter on it. If you do, exile a card from your hand.\n"
    "{T}: Add {C}. If Gemstone Caverns has a luck counter on it, instead add one mana of any color.",
    "WUBRGC",
)
NYKTHOS = land(
    "Nykthos, Shrine to Nyx",
    "Legendary Land",
    "{T}: Add {C}.\n{2}, {T}: Choose a color. Add an amount of mana of that color equal to your devotion to "
    "that color. (Your devotion to a color is the number of mana symbols of that color in the mana costs of "
    "permanents you control.)",
    "WUBRGC",
)
THREE_TREE_CITY = land(
    "Three Tree City",
    "Legendary Land",
    "As Three Tree City enters, choose a creature type.\n{T}: Add {C}.\n{2}, {T}: Choose a color. Add an "
    "amount of mana of that color equal to the number of creatures you control of the chosen type.",
    "WUBRGC",
)
TALON_GATES = land(
    "Talon Gates of Madara",
    "Land — Gate",
    "When this land enters, up to one target creature phases out.\n{T}: Add {C}.\n{1}, {T}: Add one mana of "
    "any color.\n{4}: Put this card from your hand onto the battlefield.",
    "WUBRGC",
)
ANCIENT_ZIGGURAT = land(
    "Ancient Ziggurat",
    "Land",
    "{T}: Add one mana of any color. Spend this mana only to cast a creature spell.",
    "WUBRG",
)
MAZE_OF_ITH = land(
    "Maze of Ith",
    "Land",
    "{T}: Untap target attacking creature. Prevent all combat damage that would be dealt to and dealt by "
    "that creature this turn.",
    "",
)
GLACIAL_CHASM = land(
    "Glacial Chasm",
    "Land",
    "Cumulative upkeep—Pay 2 life. (At the beginning of your upkeep, put an age counter on this permanent, "
    "then sacrifice it unless you pay its upkeep cost for each age counter on it.)\n"
    "When this land enters, sacrifice a land.\nCreatures you control can't attack.\n"
    "Prevent all damage that would be dealt to you.",
    "",
)
LOTUS_FIELD = land(
    "Lotus Field",
    "Land",
    "Hexproof\nThis land enters tapped.\nWhen this land enters, sacrifice two lands.\n"
    "{T}: Add three mana of any one color.",
    "WUBRG",
)
MYRIAD_LANDSCAPE = land(
    "Myriad Landscape",
    "Land",
    "This land enters tapped.\n{T}: Add {C}.\n{2}, {T}, Sacrifice this land: Search your library for up to "
    "two basic land cards that share a land type, put them onto the battlefield tapped, then shuffle.",
    "C",
    roles=(RAMP,),
)
KROSAN_VERGE = land(
    "Krosan Verge",
    "Land",
    "This land enters tapped.\n{T}: Add {C}.\n{2}, {T}, Sacrifice this land: Search your library for a "
    "Forest card and a Plains card, put them onto the battlefield tapped, then shuffle.",
    "C",
    roles=(RAMP,),
)
EXOTIC_ORCHARD = land(
    "Exotic Orchard",
    "Land",
    "{T}: Add one mana of any color that a land an opponent controls could produce.",
    "WUBRG",
)


def test_a_land_that_makes_its_double_mana_only_by_sacrificing_itself_makes_one() -> None:
    vein = one(CRYSTAL_VEIN)
    assert vein.land == LandFace(frozenset("C"), tapped=False, amount=1)
    assert vein.modelled


def test_a_land_that_is_sacrificed_when_another_land_is_played_says_so() -> None:
    city = one(CITY_OF_TRAITORS)
    assert city.land == LandFace(frozenset("C"), tapped=False, amount=2, lost_to_land_drop=True)
    assert city.modelled


def test_a_land_makes_the_colours_of_its_mana_abilities_not_those_it_could_make_later() -> None:
    # Gemstone Caverns makes {C} from the hand; its any colour needs the luck counter of an opening hand.
    assert one(GEMSTONE_CAVERNS).land == LandFace(frozenset("C"), tapped=False, amount=1)
    # The coloured ability of Nykthos and Three Tree City is a devotion or creature count, not read as mana.
    assert one(NYKTHOS).land == LandFace(frozenset("C"), tapped=False, amount=1)
    assert one(THREE_TREE_CITY).land == LandFace(frozenset("C"), tapped=False, amount=1)
    # A filter that pays one mana for one mana nets nothing.
    assert one(TALON_GATES).land == LandFace(frozenset("C"), tapped=False, amount=1)


def test_a_land_whose_mana_is_all_restricted_makes_none() -> None:
    ziggurat = one(ANCIENT_ZIGGURAT)
    assert ziggurat.land == LandFace(frozenset(), tapped=False, amount=0)
    assert ziggurat.modelled


def test_a_land_with_no_mana_ability_makes_no_mana() -> None:
    maze = one(MAZE_OF_ITH)
    assert maze.land == LandFace(frozenset(), tapped=False, amount=0)
    assert maze.modelled
    chasm = one(GLACIAL_CHASM)
    assert chasm.land == LandFace(frozenset(), tapped=False, amount=0)


def test_a_land_that_costs_other_lands_as_it_enters_is_not_modelled() -> None:
    field = one(LOTUS_FIELD)
    assert field.land == LandFace(frozenset("WUBRG"), tapped=True, amount=3)
    assert not field.modelled
    assert not one(GLACIAL_CHASM).modelled


@pytest.mark.parametrize("sacrificer", [MYRIAD_LANDSCAPE, KROSAN_VERGE], ids=lambda c: c.mana.name)
def test_a_land_that_also_searches_by_sacrificing_itself_stays_a_colourless_land_not_modelled(
    sacrificer: Real,
) -> None:
    card = one(sacrificer, commander=LEGION)
    assert card.land == LandFace(frozenset("C"), tapped=True, amount=1)
    assert not card.modelled


def test_a_land_whose_second_colour_needs_a_land_type_keeps_both_colours() -> None:
    verge = land(
        "Hushwood Verge",
        "Land",
        "{T}: Add {G}.\n{T}: Add {W}. Activate only if you control a Forest or a Plains.",
        "WG",
    )
    card = one(verge)
    assert card.land == LandFace(frozenset("GW"), tapped=False, amount=1)
    assert card.modelled


def test_a_land_that_makes_no_mana_is_played_without_adding_any() -> None:
    giant = real("Giant", "{9}", 9, "Creature — Giant", "")
    deck = build(FOREST, MAZE_OF_ITH, giant, copies={"Giant": 5})
    slots = [card_named(deck, "Forest"), card_named(deck, "Maze of Ith"), *[card_named(deck, "Giant")] * 5]
    # Seven cards are dealt whole: the Forest on turn 1, the Maze on turn 2 adds nothing.
    report = simulate(slots, (), (), bracket=5, average_mana_value=2.0, games=1, seed=7)
    assert report.outcomes[0].mana[:4] == (1, 1, 1, 1)


def test_a_filter_land_keeps_the_colours_it_filters_to() -> None:
    heath = land("Fetid Heath", "Land", "{T}: Add {C}.\n{W/B}, {T}: Add {W}{W}, {W}{B}, or {B}{B}.", "WBC")
    assert one(heath).land == LandFace(frozenset("CWB"), tapped=False, amount=1)


def test_a_land_with_another_land_sacrificed_by_an_unrelated_ability_is_still_modelled() -> None:
    kavaron = land(
        "Kavaron, Memorial World",
        "Land — Planet",
        "This land enters tapped.\n{T}: Add {R}.\nStation (Tap another creature you control: Put charge "
        "counters equal to its power on this Planet. Station only as a sorcery.)\n12+ | {1}{R}, {T}, "
        "Sacrifice a land: Create a 2/2 colorless Robot artifact creature token, then creatures you control "
        "get +1/+0 and gain haste until end of turn.",
        "R",
    )
    assert one(kavaron).modelled


def test_a_land_that_copies_another_land_still_makes_one_mana() -> None:
    vesuva = land(
        "Vesuva", "Land", "You may have this land enter tapped as a copy of any land on the battlefield.", ""
    )
    assert one(vesuva).land == LandFace(frozenset(), tapped=False, amount=1)


def test_command_tower_and_exotic_orchard_make_the_commanders_colours() -> None:
    tower = real(
        "Command Tower",
        "",
        0,
        "Land",
        "{T}: Add one mana of any color in your commander's color identity.",
        produced="WUBRG",
    )
    assert one(tower, commander=MAGDA).land == LandFace(frozenset("R"), tapped=False)
    assert one(EXOTIC_ORCHARD, commander=MAGDA).land == LandFace(frozenset("R"), tapped=False)
    assert one(EXOTIC_ORCHARD).land == LandFace(frozenset("WUBRG"), tapped=False)


# --- mana sources -------------------------------------------------------------------------------------------


def test_sol_ring_makes_two_colourless_at_once() -> None:
    ring = one(SOL_RING)
    assert ring.mana == ManaAbility(2, frozenset("C"), delayed=False, ritual=False)
    assert ring.mana_value == 1
    assert ring.kinds == {CardKind.ARTIFACT}
    assert ring.permanent
    assert ring.modelled


def test_arcane_signet_makes_any_colour_of_the_commanders_identity() -> None:
    assert one(ARCANE_SIGNET).mana == ManaAbility(1, frozenset("WUBRG"), delayed=False, ritual=False)
    assert one(ARCANE_SIGNET, commander=MAGDA).mana == ManaAbility(
        1, frozenset("R"), delayed=False, ritual=False
    )


def test_a_choice_of_coloured_mana_is_one_mana_not_one_per_option() -> None:
    shrine = one(JUNGLE_SHRINE)
    assert shrine.land == LandFace(frozenset("RGW"), tapped=True, amount=1)
    hierarch = one(NOBLE_HIERARCH)
    assert hierarch.mana == ManaAbility(1, frozenset("GWU"), delayed=True, ritual=False)
    assert one(SOL_RING).mana == ManaAbility(2, frozenset("C"), delayed=False, ritual=False)


def test_a_mana_creature_is_delayed_by_summoning_sickness() -> None:
    elves = one(LLANOWAR)
    assert elves.mana == ManaAbility(1, frozenset("G"), delayed=True, ritual=False)
    assert elves.pips == {"G": 1}
    assert elves.kinds == {CardKind.CREATURE}


def test_a_ritual_is_one_shot_mana_gross_of_its_own_cost() -> None:
    ritual = one(DARK_RITUAL)
    assert ritual.mana == ManaAbility(3, frozenset("B"), delayed=False, ritual=True)
    assert not ritual.permanent
    # Cabal Ritual's threshold line is a second mode, not more mana.
    assert one(CABAL_RITUAL).mana == ManaAbility(3, frozenset("B"), delayed=False, ritual=True)


def test_a_permanent_that_sacrifices_or_never_untaps_is_one_shot_too() -> None:
    petal = one(LOTUS_PETAL)
    assert petal.mana == ManaAbility(1, frozenset("WUBRG"), delayed=False, ritual=True)
    assert petal.mana_value == 0
    assert one(MANA_VAULT).mana == ManaAbility(3, frozenset("C"), delayed=False, ritual=True)


def test_a_signet_nets_one_mana_of_either_colour() -> None:
    assert one(AZORIUS_SIGNET).mana == ManaAbility(1, frozenset("WU"), delayed=False, ritual=False)


def test_a_talisman_unions_its_abilities_colours() -> None:
    assert one(TALISMAN).mana == ManaAbility(1, frozenset("CUB"), delayed=False, ritual=False)


MOX_AMBER = real(
    "Mox Amber",
    "{0}",
    0,
    "Legendary Artifact",
    "{T}: Add one mana of any color among legendary creatures and planeswalkers you control.",
    produced="WUBRG",
    roles=(RAMP,),
)
FLYWHEEL_RACER = real(
    "Flywheel Racer",
    "{2}",
    2,
    "Artifact — Vehicle",
    "Vigilance\n{T}: Add one mana of any color. Activate only if this permanent is a creature.\n"
    "Crew 1 (Tap any number of creatures you control with total power 1 or more: This Vehicle "
    "becomes an artifact creature until end of turn.)",
    produced="WUBRG",
    roles=(RAMP,),
)
SPRINGLEAF_DRUM = real(
    "Springleaf Drum",
    "{1}",
    1,
    "Artifact",
    "{T}, Tap an untapped creature you control: Add one mana of any color.",
    produced="WUBRG",
    roles=(RAMP,),
)
MOX_OPAL = real(
    "Mox Opal",
    "{0}",
    0,
    "Legendary Artifact",
    "Metalcraft — {T}: Add one mana of any color. Activate only if you control three or more artifacts.",
    produced="WUBRG",
    roles=(RAMP,),
)
TREASONOUS_OGRE = real(
    "Treasonous Ogre",
    "{3}{R}",
    4,
    "Creature — Ogre Shaman",
    "Dethrone (Whenever this creature attacks the player with the most life or tied for most life, put a "
    "+1/+1 counter on it.)\nPay 3 life: Add {R}.",
    produced="R",
    roles=(RAMP,),
)
ORCISH_LUMBERJACK = real(
    "Orcish Lumberjack",
    "{R}",
    1,
    "Creature — Orc",
    "{T}, Sacrifice a Forest: Add three mana in any combination of {R} and/or {G}.",
    produced="RG",
    roles=(RAMP,),
)


def test_a_rock_that_needs_a_legendary_creature_is_not_modelled() -> None:
    deck = build(MOX_AMBER)
    card = card_named(deck, MOX_AMBER.mana.name)
    assert card.mana is None
    assert not card.modelled
    assert deck.not_modelled == (MOX_AMBER.mana.name,)


def test_a_rock_that_taps_a_creature_needs_one_and_a_crewed_vehicle_needs_a_turn_too() -> None:
    drum = one(SPRINGLEAF_DRUM)
    assert drum.mana == ManaAbility(1, frozenset(ALL), delayed=False, ritual=False, needs=ManaNeed.CREATURE)
    assert drum.modelled
    racer = one(FLYWHEEL_RACER)
    assert racer.mana == ManaAbility(1, frozenset(ALL), delayed=True, ritual=False, needs=ManaNeed.CREATURE)
    assert racer.modelled


def test_mox_opal_needs_three_artifacts() -> None:
    opal = one(MOX_OPAL)
    assert opal.mana == ManaAbility(1, frozenset(ALL), delayed=False, ritual=False, needs=ManaNeed.ARTIFACTS)
    assert opal.modelled


def test_mana_paid_for_with_life_is_mana_for_each_payment() -> None:
    ogre = one(TREASONOUS_OGRE)
    assert ogre.mana == ManaAbility(1, frozenset("R"), delayed=False, ritual=False, life=3)
    assert ogre.modelled


def test_mana_paid_for_with_a_land_is_a_burst_from_a_creature_in_play() -> None:
    jack = one(ORCISH_LUMBERJACK)
    assert jack.mana == ManaAbility(
        3, frozenset("RG"), delayed=True, ritual=False, land_sacrificed=frozenset({"forest"})
    )
    assert jack.modelled


def test_a_sacrifice_to_draw_is_neither_a_draw_nor_a_hindrance_to_the_mana() -> None:
    stone = one(MIND_STONE)
    assert stone.mana == ManaAbility(1, frozenset("C"), delayed=False, ritual=False)
    assert stone.draw is None
    assert stone.modelled


TINDER_WALL = real(
    "Tinder Wall",
    "{G}",
    1,
    "Creature — Plant Wall",
    "Defender (This creature can't attack.)\nSacrifice this creature: Add {R}{R}.\n{R}, Sacrifice this "
    "creature: It deals 2 damage to target creature it's blocking.",
    produced="R",
    roles=(RAMP, Role.REMOVAL),
)
WILD_CANTOR = real(
    "Wild Cantor",
    "{R/G}",
    1,
    "Creature — Human Druid",
    "({R/G} can be paid with either {R} or {G}.)\nSacrifice this creature: Add one mana of any color.",
    produced="WUBRG",
    roles=(RAMP,),
)
RITE_OF_FLAME = real(
    "Rite of Flame",
    "{R}",
    1,
    "Sorcery",
    "Add {R}{R}, then add {R} for each card named Rite of Flame in each graveyard.",
    produced="R",
    roles=(RAMP,),
)
JESKAS_WILL = real(
    "Jeska's Will",
    "{2}{R}",
    3,
    "Sorcery",
    "Choose one. If you control a commander as you cast this spell, you may choose both instead.\n• Add {R} "
    "for each card in target opponent's hand.\n• Exile the top three cards of your library. You may play "
    "them this turn.",
    produced="R",
    roles=(RAMP,),
)
STRIKE_IT_RICH = real(
    "Strike It Rich",
    "{R}",
    1,
    "Sorcery",
    "Create a Treasure token. (It's an artifact with \"{T}, Sacrifice this token: Add one mana of any "
    'color.")\nFlashback {2}{R} (You may cast this card from your graveyard for its flashback cost. Then '
    "exile it.)",
    produced="WUBRG",
    roles=(RAMP,),
)


def test_a_creature_that_sacrifices_itself_for_mana_is_one_shot_mana_at_once() -> None:
    wall = one(TINDER_WALL)
    assert wall.mana == ManaAbility(2, frozenset("R"), delayed=False, ritual=True)
    assert wall.modelled
    assert one(WILD_CANTOR).mana == ManaAbility(1, frozenset(ALL), delayed=False, ritual=True)


def test_a_ritual_keeps_the_fixed_part_of_mana_that_grows_with_a_count() -> None:
    rite = one(RITE_OF_FLAME)
    assert rite.mana == ManaAbility(2, frozenset("R"), delayed=False, ritual=True)
    assert rite.modelled


def test_jeskas_will_counts_an_opponents_hand_of_seven() -> None:
    will = one(JESKAS_WILL)
    assert will.mana == ManaAbility(7, frozenset("R"), delayed=False, ritual=True)
    assert will.modelled


def test_a_spell_that_makes_a_treasure_is_a_ritual_of_one_mana_of_any_colour() -> None:
    strike = one(STRIKE_IT_RICH, commander=MAGDA)
    assert strike.mana == ManaAbility(1, frozenset("R"), delayed=False, ritual=True)
    assert strike.modelled


GEOSURGE = real(
    "Geosurge",
    "{R}{R}{R}{R}",
    4,
    "Sorcery",
    "Add {R}{R}{R}{R}{R}{R}{R}. Spend this mana only to cast artifact or creature spells.",
    produced="R",
    roles=(RAMP,),
)
OPEN_THE_OMENPATHS = real(
    "Open the Omenpaths",
    "{2}{R}",
    3,
    "Instant",
    "Choose one —\n• Add two mana of any one color and two mana of any other color. Spend this mana only to "
    "cast creature or enchantment spells.\n• Creatures you control get +1/+0 until end of turn.",
    produced="WUBRG",
    roles=(RAMP,),
)
SHANG_CHI = real(
    "Shang-Chi, Master of Kung Fu",
    "{1}{G}",
    2,
    "Legendary Creature — Human Warrior Hero",
    "You may activate abilities of creatures you control as though those creatures had haste.\n{T}: Add two "
    "mana of any one color. Spend this mana only to activate abilities of creature sources.",
    produced="WUBRG",
    roles=(RAMP,),
)


def test_mana_spent_only_on_some_kinds_of_spell_is_counted_for_those_kinds() -> None:
    assert one(GEOSURGE).mana == ManaAbility(
        7,
        frozenset("R"),
        delayed=False,
        ritual=True,
        spend_on=frozenset({CardKind.ARTIFACT, CardKind.CREATURE}),
    )
    omenpaths = one(OPEN_THE_OMENPATHS)
    assert omenpaths.mana == ManaAbility(
        4,
        frozenset(ALL),
        delayed=False,
        ritual=True,
        spend_on=frozenset({CardKind.CREATURE, CardKind.ENCHANTMENT}),
    )
    assert omenpaths.modelled


LIONS_EYE_DIAMOND = real(
    "Lion's Eye Diamond",
    "{0}",
    0,
    "Artifact",
    "Discard your hand, Sacrifice this artifact: Add three mana of any one color. Activate only as an "
    "instant.",
    produced="WUBRG",
    roles=(RAMP,),
)
METAMORPHOSIS = real(
    "Metamorphosis",
    "{G}",
    1,
    "Sorcery",
    "As an additional cost to cast this spell, sacrifice a creature.\nAdd X mana of any one color, where X "
    "is 1 plus the sacrificed creature's mana value. Spend this mana only to cast creature spells.",
    produced="WUBRG",
    roles=(RAMP,),
)
INFERNAL_PLUNGE = real(
    "Infernal Plunge",
    "{R}",
    1,
    "Sorcery",
    "As an additional cost to cast this spell, sacrifice a creature.\nAdd {R}{R}{R}.",
    produced="R",
    roles=(RAMP,),
)


def test_lions_eye_diamond_is_one_shot_mana_that_discards_the_hand() -> None:
    diamond = one(LIONS_EYE_DIAMOND)
    assert diamond.mana == ManaAbility(3, frozenset(ALL), delayed=False, ritual=True, discards_hand=True)
    assert diamond.modelled


def test_a_ritual_that_sacrifices_a_creature_needs_one_and_may_add_its_mana_value() -> None:
    plunge = one(INFERNAL_PLUNGE)
    assert plunge.mana == ManaAbility(3, frozenset("R"), delayed=False, ritual=True)
    assert plunge.needs_sacrifice
    metamorphosis = one(METAMORPHOSIS)
    assert metamorphosis.mana == ManaAbility(
        1,
        frozenset(ALL),
        delayed=False,
        ritual=True,
        spend_on=frozenset({CardKind.CREATURE}),
        plus_sacrificed=True,
    )
    assert metamorphosis.needs_sacrifice
    assert metamorphosis.modelled


def test_mana_spent_only_on_abilities_is_left_out() -> None:
    card = one(SHANG_CHI)
    assert card.mana is None
    assert not card.modelled


# --- land ramp ------------------------------------------------------------------------------------


def test_cultivate_puts_one_land_tapped_and_one_in_hand_and_is_no_tutor() -> None:
    cultivate = one(CULTIVATE)
    assert cultivate.land_ramp == LandRamp(2, basic_only=True, destination=LandDestination.TAPPED, to_hand=1)
    assert cultivate.tutor is None
    assert cultivate.modelled


def test_land_ramp_reads_basics_typed_lands_tapped_and_untapped() -> None:
    assert one(RAMPANT_GROWTH).land_ramp == LandRamp(1, basic_only=True, destination=LandDestination.TAPPED)
    forest = frozenset({"forest"})
    assert one(NATURES_LORE).land_ramp == LandRamp(
        1, basic_only=False, destination=LandDestination.UNTAPPED, land_types=forest
    )
    assert one(FARSEEK).land_ramp == LandRamp(
        1,
        basic_only=False,
        destination=LandDestination.TAPPED,
        land_types=frozenset({"plains", "island", "swamp", "mountain"}),
    )
    assert one(SKYSHROUD_CLAIM).land_ramp == LandRamp(
        2, basic_only=False, destination=LandDestination.UNTAPPED, land_types=forest
    )
    assert one(WOOD_ELVES).land_ramp == LandRamp(
        1, basic_only=False, destination=LandDestination.UNTAPPED, land_types=forest
    )


def test_a_land_record_carries_its_basic_land_types() -> None:
    assert one(FOREST).land_types == {"forest"}
    assert one(BREEDING_POOL).land_types == {"forest", "island"}
    assert one(COMMAND_TOWER).land_types == frozenset()
    assert one(SOL_RING).land_types == frozenset()


def test_a_permanent_that_sacrifices_itself_for_lands_carries_its_activation_cost() -> None:
    assert one(SAKURA_TRIBE_ELDER).land_ramp == LandRamp(
        1, basic_only=True, destination=LandDestination.TAPPED, activation=0
    )
    assert one(WAYFARERS_BAUBLE).land_ramp == LandRamp(
        1, basic_only=True, destination=LandDestination.TAPPED, activation=2
    )
    assert one(BURNISHED_HART).land_ramp == LandRamp(
        2, basic_only=True, destination=LandDestination.TAPPED, activation=3
    )


# --- tutors -------------------------------------------------------------------------------------------------


def test_a_tutor_for_any_card_goes_to_hand_or_to_the_top() -> None:
    assert one(DEMONIC_TUTOR).tutor == Tutor(frozenset(), TutorDestination.HAND)
    assert one(VAMPIRIC_TUTOR).tutor == Tutor(frozenset(), TutorDestination.TOP)


def test_a_tutor_names_the_card_types_it_finds() -> None:
    assert one(WORLDLY_TUTOR).tutor == Tutor(frozenset({CardKind.CREATURE}), TutorDestination.TOP)
    assert one(ENLIGHTENED_TUTOR).tutor == Tutor(
        frozenset({CardKind.ARTIFACT, CardKind.ENCHANTMENT}), TutorDestination.TOP
    )


def test_an_x_tutor_to_the_battlefield_is_limited_by_x() -> None:
    zenith = one(GREEN_SUNS_ZENITH)
    assert zenith.tutor == Tutor(
        frozenset({CardKind.CREATURE}), TutorDestination.BATTLEFIELD, x_limited=True, colours=frozenset("G")
    )
    assert zenith.x_cost
    assert zenith.mana_value == 1
    finale = one(FINALE)
    assert finale.tutor == Tutor(frozenset({CardKind.CREATURE}), TutorDestination.BATTLEFIELD, x_limited=True)
    assert finale.x_cost
    assert finale.pips == {"G": 2}


def test_a_search_is_a_tutor_only_when_the_tagger_says_so() -> None:
    untagged = real(
        "Demonic Tutor", DEMONIC_TUTOR.mana.mana_cost, 2, "Sorcery", DEMONIC_TUTOR.mana.oracle_text
    )
    card = one(untagged)
    assert card.tutor is None
    assert card.modelled


@pytest.mark.parametrize(
    "tutor",
    [RECRUITER_OF_THE_GUARD, TROPHY_MAGE, FIERCE_EMPATH, TIME_OF_NEED],
    ids=lambda t: t.mana.name,
)
def test_a_search_restricted_beyond_type_colour_power_and_a_mana_value_ceiling_is_not_modelled(
    tutor: Real,
) -> None:
    card = one(tutor)
    assert card.tutor is None
    assert not card.modelled


def test_a_tutor_for_a_creature_with_power_up_to_a_ceiling_says_so_and_cards_carry_their_power() -> None:
    recruiter = one(IMPERIAL_RECRUITER)
    assert recruiter.tutor == Tutor(frozenset({CardKind.CREATURE}), TutorDestination.HAND, max_power=2)
    assert recruiter.modelled
    assert recruiter.power == 1
    assert one(SOL_RING).power is None


GOBLIN_MATRON = real(
    "Goblin Matron",
    "{2}{R}",
    3,
    "Creature — Goblin",
    "When this creature enters, you may search your library for a Goblin card, reveal that card, put it into "
    "your hand, then shuffle.",
    roles=(TUTOR,),
)
STONEFORGE = real(
    "Stoneforge Mystic",
    "{1}{W}",
    2,
    "Creature — Kor Artificer",
    "When this creature enters, you may search your library for an Equipment card, reveal it, put it into "
    "your hand, then shuffle.\n{1}{W}, {T}: You may put an Equipment card from your hand onto the "
    "battlefield.",
    roles=(TUTOR,),
)
ACADEMY_RECTOR = real(
    "Academy Rector",
    "{3}{W}",
    4,
    "Creature — Human Cleric",
    "When this creature dies, you may exile it. If you do, search your library for an enchantment card, put "
    "that card onto the battlefield, then shuffle.",
    roles=(TUTOR,),
)
HERMIT_DRUID = real(
    "Hermit Druid",
    "{1}{G}",
    2,
    "Creature — Human Druid",
    "{G}, {T}: Reveal cards from the top of your library until you reveal a basic land card. Put that card "
    "into your hand and all other cards revealed this way into your graveyard.",
)


def test_a_tutor_for_a_subtype_of_the_decks_cards_finds_it_and_cards_carry_their_subtypes() -> None:
    deck = build(GOBLIN_MATRON, ELECTROMANCER)
    matron = card_named(deck, "Goblin Matron")
    assert matron.tutor == Tutor(frozenset(), TutorDestination.HAND, subtypes=frozenset({"goblin"}))
    assert matron.modelled
    assert card_named(deck, "Goblin Electromancer").subtypes == {"goblin", "wizard"}
    # No card of the deck is an Equipment, so the word is not known for a type.
    mystic = one(STONEFORGE)
    assert mystic.tutor is None
    assert not mystic.modelled


SARKHANS_TRIUMPH = real(
    "Sarkhan's Triumph",
    "{2}{R}",
    3,
    "Instant",
    "Search your library for a Dragon creature card, reveal it, put it into your hand, then shuffle.",
    roles=(TUTOR,),
)
SHIVAN_DRAGON = real(
    "Shivan Dragon",
    "{4}{R}{R}",
    6,
    "Creature — Dragon",
    "Flying\n{R}: This creature gets +1/+0 until end of turn.",
)


def test_a_tutor_for_a_subtype_of_a_card_type_needs_both() -> None:
    triumph = card_named(build(SARKHANS_TRIUMPH, SHIVAN_DRAGON), "Sarkhan's Triumph")
    assert triumph.tutor == Tutor(
        frozenset({CardKind.CREATURE}), TutorDestination.HAND, subtypes=frozenset({"dragon"})
    )


PROTEAN_HULK = real(
    "Protean Hulk",
    "{5}{G}{G}",
    7,
    "Creature — Beast",
    "When this creature dies, search your library for any number of creature cards with total mana value 6 "
    "or less, put them onto the battlefield, then shuffle.",
    roles=(TUTOR,),
)
CARRION_FEEDER = real(
    "Carrion Feeder",
    "{B}",
    1,
    "Creature — Zombie",
    "This creature can't block.\nSacrifice a creature: Put a +1/+1 counter on this creature.",
)
PHYREXIAN_TOWER = real(
    "Phyrexian Tower",
    "",
    0,
    "Legendary Land",
    "{T}: Add {C}.\n{T}, Sacrifice a creature: Add {B}{B}.",
    produced="BC",
)


def test_a_search_as_the_creature_dies_is_its_death_tutor_and_not_one_on_casting() -> None:
    rector = one(ACADEMY_RECTOR)
    assert rector.tutor is None
    assert rector.dies_tutor == Tutor(frozenset({CardKind.ENCHANTMENT}), TutorDestination.BATTLEFIELD)
    assert rector.modelled
    hulk = one(PROTEAN_HULK)
    creatures = frozenset({CardKind.CREATURE})
    assert hulk.dies_tutor == Tutor(creatures, TutorDestination.BATTLEFIELD, total_mana_value=6)
    assert hulk.modelled


def test_a_sacrifice_for_no_mana_is_a_free_outlet_and_one_that_costs_mana_is_not() -> None:
    assert one(CARRION_FEEDER).free_outlet
    assert one(PHYREXIAN_TOWER).free_outlet
    assert not one(DIABOLIC_INTENT).free_outlet
    assert not one(PROTEAN_HULK).free_outlet


def test_a_creature_with_a_tap_ability_other_than_mana_says_so() -> None:
    assert one(HERMIT_DRUID).tap_ability
    assert not one(LLANOWAR).tap_ability
    assert not one(THASSAS_ORACLE).tap_ability


@pytest.mark.parametrize("tutor", [SURVIVAL, ENTOMB], ids=lambda t: t.mana.name)
def test_tutors_that_cost_a_card_repeat_or_leave_the_library_are_not_modelled(tutor: Real) -> None:
    card = one(tutor)
    assert card.tutor is None
    assert not card.modelled


# --- draw ---------------------------------------------------------------------------------------------------


def test_a_draw_spell_draws_its_count_at_once() -> None:
    assert one(HARMONIZE).draw == Draw(3, engine=False)
    assert one(NIGHTS_WHISPER).draw == Draw(2, engine=False)


def test_a_permanent_that_keeps_drawing_is_an_engine() -> None:
    assert one(RHYSTIC_STUDY).draw == Draw(1, engine=True)
    assert one(PHYREXIAN_ARENA).draw == Draw(1, engine=True)


def test_a_creature_that_draws_on_arrival_draws_once_and_a_looter_is_an_engine() -> None:
    assert one(MULLDRIFTER).draw == Draw(2, engine=False)
    assert one(MERFOLK_LOOTER).draw == Draw(1, engine=True)


EIDOLON_OF_BLOSSOMS = real(
    "Eidolon of Blossoms",
    "{2}{G}{G}",
    4,
    "Enchantment Creature — Spirit",
    "Constellation — Whenever this creature or another enchantment you control enters, draw a card.",
    roles=(DRAW,),
)
SETESSAN_CHAMPION = real(
    "Setessan Champion",
    "{2}{G}",
    3,
    "Creature — Human Warrior",
    "Constellation — Whenever an enchantment you control enters, put a +1/+1 counter on this creature and "
    "draw a card.",
    roles=(DRAW,),
)
RANGING_RAPTORS = real(
    "Ranging Raptors",
    "{2}{G}",
    3,
    "Creature — Dinosaur",
    "Enrage — Whenever this creature is dealt damage, you may search your library for a basic land card, "
    "put it onto the battlefield tapped, then shuffle.",
    roles=(RAMP,),
)


@pytest.mark.parametrize("drawer", [EIDOLON_OF_BLOSSOMS, SETESSAN_CHAMPION], ids=lambda c: c.mana.name)
def test_a_trigger_behind_an_ability_word_is_a_repeating_draw(drawer: Real) -> None:
    assert one(drawer).draw == Draw(1, engine=True)


def test_a_search_trigger_behind_an_ability_word_repeats_and_is_not_modelled() -> None:
    raptors = one(RANGING_RAPTORS)
    assert raptors.land_ramp is None
    assert not raptors.modelled


def test_a_draw_that_costs_mana_each_time_or_varies_is_not_modelled() -> None:
    for card in (one(KENRITH), one(SEA_GATE), one(JAYEMDAE_TOME)):
        assert card.draw is None
        assert not card.modelled


def test_a_split_card_is_read_by_the_half_it_is_cast_as() -> None:
    fire = one(FIRE_ICE)
    assert fire.mana_value == 2
    assert fire.pips == {"R": 1}
    assert fire.kinds == {CardKind.INSTANT}
    # The draw is on Ice, the half that is not cast.
    assert fire.draw is None
    assert fire.modelled


def test_an_adventure_card_is_cast_as_its_creature() -> None:
    giant = one(BONECRUSHER)
    assert giant.mana_value == 3
    assert giant.pips == {"R": 1}
    assert giant.kinds == {CardKind.CREATURE}


# --- finishers, combos and tags -------------------------------------------------------------------


def test_cards_with_a_finisher_tag_are_finishers() -> None:
    hoof = one(CRATERHOOF)
    assert hoof.finisher
    assert hoof.mana_value == 8
    assert hoof.pips == {"G": 3}
    assert one(THASSAS_ORACLE).finisher
    assert not one(SOL_RING).finisher


def test_finishers_can_be_added_by_name_and_removed() -> None:
    deck = build(SOL_RING, CRATERHOOF, added=["sol ring"], removed=["Craterhoof Behemoth"])
    assert card_named(deck, "Sol Ring").finisher
    assert not card_named(deck, "Craterhoof Behemoth").finisher


def combo_details() -> tuple[ComboDetail, ...]:
    raw = json.loads((FIXTURES / "spellbook_combos.json").read_text(encoding="utf-8"))
    return parse_details(raw)


def test_a_combo_with_a_described_piece_is_kept_on_its_named_pieces() -> None:
    deck = build(BATTERED_GOLEM, MASKWOOD_NEXUS, commander=MAGDA, combos=combo_details())
    assert [(c.id, c.pieces, c.requirements) for c in deck.combos] == [
        (
            "643-1486-3459--67",
            ("Magda, Brazen Outlaw", "Battered Golem", "Maskwood Nexus"),
            ("Vehicle with Crew 4 or less",),
        )
    ]
    assert deck.combos[0].results[0] == "Infinite artifact ETB"
    assert deck.left_out_combos == ()
    assert card_named(deck, "Battered Golem").combo
    assert card_named(deck, "Maskwood Nexus").combo
    assert card_named(deck, "Magda, Brazen Outlaw").combo


def test_a_combo_needing_a_piece_in_the_graveyard_is_left_out() -> None:
    reanimation = ComboDetail(
        "1-2",
        (
            ComboPiece("Animate Dead", "Enchantment — Aura", ("B",)),
            ComboPiece("Worldgorger Dragon", "Creature — Nightmare Dragon", ("G",)),
        ),
        (),
        ("Infinite mana",),
        "",
    )
    deck = build(ANIMATE_DEAD, WORLDGORGER, SOL_RING, combos=(reanimation,))
    assert deck.combos == ()
    assert [c.pieces for c in deck.left_out_combos] == [("Animate Dead", "Worldgorger Dragon")]
    assert not card_named(deck, "Animate Dead").combo
    assert not card_named(deck, "Worldgorger Dragon").combo


def test_a_piece_may_start_in_any_one_of_its_zones() -> None:
    either = ComboDetail(
        "3-4",
        (
            ComboPiece("Animate Dead", "Enchantment — Aura", ("H", "B")),
            ComboPiece("Worldgorger Dragon", "Creature — Nightmare Dragon", ("G", "H")),
        ),
        (),
        (),
        "",
    )
    assert len(build(ANIMATE_DEAD, WORLDGORGER, combos=(either,)).combos) == 1


def test_a_piece_that_needs_only_the_command_zone_is_marked_so() -> None:
    combo = ComboDetail(
        "5-6",
        (
            ComboPiece("Magda, Brazen Outlaw", "Legendary Creature — Dwarf", ("C",)),
            ComboPiece("Battered Golem", "Artifact Creature — Golem", ("B", "C")),
        ),
        (),
        (),
        "",
    )
    deck = build(BATTERED_GOLEM, commander=MAGDA, combos=(combo,))
    # Battered Golem may also start on the battlefield, so it is not a command zone piece.
    assert [c.command_zone for c in deck.combos] == [("Magda, Brazen Outlaw",)]


def test_a_combo_with_a_piece_outside_the_deck_is_left_out() -> None:
    deck = build(BATTERED_GOLEM, commander=MAGDA, combos=combo_details())
    assert deck.combos == ()
    assert len(deck.left_out_combos) == 1
    assert not card_named(deck, "Battered Golem").combo


def test_synergy_marks_the_cards_matching_the_dominant_tags() -> None:
    deck = build(SOL_RING, HARMONIZE, synergy=["id-Harmonize"])
    assert card_named(deck, "Harmonize").synergy
    assert not card_named(deck, "Sol Ring").synergy


# --- mana auras, free mana, land drops and sacrifices -------------------------------------------------------


def test_a_mana_aura_adds_mana_with_its_land_and_comes_the_turn_it_lands() -> None:
    growth = one(WILD_GROWTH)
    assert growth.mana == ManaAbility(1, frozenset("G"), delayed=False, ritual=False, on_land=True)
    assert growth.modelled
    assert one(UTOPIA_SPRAWL).mana == ManaAbility(
        1, frozenset("WUBRG"), delayed=False, ritual=False, on_land=True
    )
    assert one(FERTILE_GROUND, commander=MAGDA).mana == ManaAbility(
        1, frozenset("R"), delayed=False, ritual=False, on_land=True
    )


def test_a_spirit_guide_is_one_shot_mana_from_the_hand() -> None:
    guide = one(ELVISH_SPIRIT_GUIDE)
    assert guide.mana == ManaAbility(1, frozenset("G"), delayed=False, ritual=True, from_hand=True)
    assert guide.mana_value == 3
    assert guide.modelled


def test_chrome_mox_and_mox_diamond_are_free_rocks_costing_a_card() -> None:
    mox = one(CHROME_MOX)
    assert mox.mana == ManaAbility(1, frozenset("WUBRG"), delayed=False, ritual=False)
    assert mox.entry_cost is EntryCost.NONLAND_CARD
    assert mox.mana_value == 0
    assert mox.modelled
    diamond = one(MOX_DIAMOND)
    assert diamond.mana == ManaAbility(1, frozenset("WUBRG"), delayed=False, ritual=False)
    assert diamond.entry_cost is EntryCost.LAND_CARD
    assert diamond.modelled
    assert one(SOL_RING).entry_cost is None


def test_lotus_petal_is_read_whatever_the_taggers_free_cost_tag_says() -> None:
    assert one(LOTUS_PETAL).modelled


def test_a_mana_creature_with_one_restricted_ability_keeps_the_free_one() -> None:
    halfling = one(DELIGHTED_HALFLING)
    assert halfling.mana == ManaAbility(1, frozenset("C"), delayed=True, ritual=False)
    assert halfling.modelled


def test_extra_land_drops_are_counted_per_turn() -> None:
    assert one(EXPLORATION).extra_land_drops == 1
    assert one(AZUSA).extra_land_drops == 2
    assert one(ORACLE_OF_MUL_DAYA).extra_land_drops == 1
    assert one(ORACLE_OF_MUL_DAYA).modelled
    assert one(SOL_RING).extra_land_drops == 0


def test_a_pact_is_a_free_spell() -> None:
    pact = one(SUMMONERS_PACT)
    assert pact.mana_value == 0
    assert pact.tutor == Tutor(frozenset({CardKind.CREATURE}), TutorDestination.HAND, colours=frozenset("G"))
    assert pact.modelled
    assert one(SLAUGHTER_PACT).modelled


def test_tutors_and_rituals_with_a_sacrifice_cost_say_so() -> None:
    intent = one(DIABOLIC_INTENT)
    assert intent.tutor == Tutor(frozenset(), TutorDestination.HAND)
    assert intent.needs_sacrifice
    order = one(NATURAL_ORDER)
    assert order.tutor == Tutor(
        frozenset({CardKind.CREATURE}), TutorDestination.BATTLEFIELD, colours=frozenset("G")
    )
    assert order.needs_sacrifice
    culling = one(CULLING_THE_WEAK)
    assert culling.mana == ManaAbility(4, frozenset("B"), delayed=False, ritual=True)
    assert culling.needs_sacrifice
    assert not one(DEMONIC_TUTOR).needs_sacrifice


def test_a_tutor_limited_by_the_sacrificed_creature_carries_the_bonus() -> None:
    evolution = one(ELDRITCH_EVOLUTION)
    assert evolution.tutor == Tutor(
        frozenset({CardKind.CREATURE}), TutorDestination.BATTLEFIELD, value_over_sacrifice=2
    )
    assert evolution.needs_sacrifice


def test_a_search_that_sacrifices_a_land_or_an_artifact_is_not_modelled() -> None:
    rotation = one(CROP_ROTATION)
    assert not rotation.modelled
    assert not rotation.needs_sacrifice
    assert rotation.land_ramp is None
    tinker = one(TINKER)
    assert not tinker.modelled
    assert not tinker.needs_sacrifice
    assert tinker.tutor is None
    assert build(CROP_ROTATION, TINKER, DIABOLIC_INTENT).not_modelled == ("Crop Rotation", "Tinker")


ENTISH_RESTORATION = real(
    "Entish Restoration",
    "{2}{G}",
    3,
    "Instant",
    "Sacrifice a land. Search your library for up to two basic land cards, put them onto the battlefield "
    "tapped, then shuffle. If you control a creature with power 4 or greater, instead search your library "
    "for up to three basic land cards, put them onto the battlefield tapped, then shuffle.",
    roles=(RAMP,),
)
YASHARN = real(
    "Yasharn, Implacable Earth",
    "{2}{G}{W}",
    4,
    "Legendary Creature — Elemental Boar",
    "When Yasharn enters, search your library for a basic Forest card and a basic Plains card, reveal those "
    "cards, put them into your hand, then shuffle.\nPlayers can't pay life or sacrifice nonland permanents "
    "to cast spells or activate abilities.",
    roles=(TUTOR,),
)


def test_a_spell_that_sacrifices_a_land_in_its_effect_before_the_search_is_not_modelled() -> None:
    restoration = one(ENTISH_RESTORATION)
    assert restoration.land_ramp is None
    assert not restoration.modelled


def test_a_search_for_two_different_land_cards_is_not_modelled() -> None:
    yasharn = one(YASHARN)
    assert yasharn.land_ramp is None
    assert yasharn.tutor is None
    assert not yasharn.modelled


# --- cards the reading cannot place ---------------------------------------------------------------


def test_alternative_costs_are_plain_modelled_spells_when_they_do_not_matter() -> None:
    deck = build(FIREBLAST, FORCE_OF_WILL)
    assert deck.not_modelled == ()
    assert card_named(deck, "Force of Will").mana_value == 5


HERALDS_HORN = real(
    "Herald's Horn",
    "{3}",
    3,
    "Artifact",
    "As this artifact enters, choose a creature type.\nCreature spells you cast of the chosen type cost {1} "
    "less to cast.\nAt the beginning of your upkeep, look at the top card of your library. If it's a "
    "creature card of the chosen type, you may reveal it and put it into your hand.",
)
URZAS_INCUBATOR = real(
    "Urza's Incubator",
    "{3}",
    3,
    "Artifact",
    "As this artifact enters, choose a creature type.\nCreature spells of the chosen type cost {2} less to "
    "cast.",
)
ELECTROMANCER = real(
    "Goblin Electromancer",
    "{U}{R}",
    2,
    "Creature — Goblin Wizard",
    "Instant and sorcery spells you cast cost {1} less to cast.",
)
ANIMAR = real(
    "Animar, Soul of Elements",
    "{G}{U}{R}",
    3,
    "Legendary Creature — Elemental",
    "Protection from white and from black\nWhenever you cast a creature spell, put a +1/+1 counter on "
    "Animar.\nCreature spells you cast cost {1} less to cast for each +1/+1 counter on Animar.",
)
JHOIRAS_FAMILIAR = real(
    "Jhoira's Familiar",
    "{4}",
    4,
    "Artifact Creature — Bird",
    "Flying\nHistoric spells you cast cost {1} less to cast. (Artifacts, legendaries, and Sagas are "
    "historic.)",
)
GHALTA = real(
    "Ghalta, Primal Hunger",
    "{10}{G}{G}",
    12,
    "Legendary Creature — Elder Dinosaur",
    "This spell costs {X} less to cast, where X is the total power of creatures you control.\nTrample (This "
    "creature can deal excess combat damage to the player or planeswalker it's attacking.)",
)


COST_REDUCERS = [
    SCULPTOR,
    MEDALLION,
    JET_MEDALLION,
    HERALDS_HORN,
    URZAS_INCUBATOR,
    ELECTROMANCER,
    ANIMAR,
    JHOIRAS_FAMILIAR,
    GHALTA,
]


@pytest.mark.parametrize("reducer", COST_REDUCERS, ids=lambda c: c.mana.name)
def test_a_cost_reducer_is_a_spell_with_its_printed_cost_listed_as_not_modelled(reducer: Real) -> None:
    deck = build(reducer)
    card = card_named(deck, reducer.mana.name)
    assert not card.modelled
    assert card.mana_value == int(reducer.mana.cmc)
    assert (card.mana, card.land, card.land_ramp, card.tutor, card.draw) == (None,) * 5
    assert deck.not_modelled == (reducer.mana.name,)


@pytest.mark.parametrize("reducer", [SCULPTOR, MEDALLION, FIREBLAST], ids=lambda c: c.mana.name)
def test_a_cost_reducer_or_alternative_cost_that_is_a_finisher_is_not_modelled(reducer: Real) -> None:
    deck = build(reducer, added=[reducer.mana.name])
    card = card_named(deck, reducer.mana.name)
    assert not card.modelled
    assert card.finisher
    assert card.mana_value == int(reducer.mana.cmc)
    assert (card.mana, card.land, card.land_ramp, card.tutor, card.draw) == (None,) * 5
    assert deck.not_modelled == (reducer.mana.name,)


def test_a_cost_reducer_that_is_a_combo_piece_is_not_modelled() -> None:
    combo = ComboDetail(
        "5-6",
        (
            ComboPiece("Etherium Sculptor", "Artifact Creature — Vedalken Artificer", ("B",)),
            ComboPiece("Sol Ring", "Artifact", ("B",)),
        ),
        (),
        (),
        "",
    )
    deck = build(SCULPTOR, SOL_RING, combos=(combo,))
    assert deck.not_modelled == ("Etherium Sculptor",)


def test_a_cost_reducer_the_tagger_calls_ramp_is_not_modelled() -> None:
    tagged = real("Jet Medallion", "{2}", 2, "Artifact", JET_MEDALLION.mana.oracle_text, roles=(RAMP,))
    assert not one(tagged).modelled


def test_a_free_spell_tag_does_not_keep_a_readable_card_out() -> None:
    # Lotus Petal and the pacts carry the Tagger's manaless-value tag; they are read all the same.
    petal = real(
        "Lotus Petal",
        LOTUS_PETAL.mana.mana_cost,
        0,
        "Artifact",
        LOTUS_PETAL.mana.oracle_text,
        roles=(RAMP,),
        tags=("manaless-value",),
    )
    assert one(petal).modelled
    assert one(petal).mana is not None


def test_a_key_card_with_an_alternative_cost_tag_is_not_modelled() -> None:
    tagged = real(
        "Rampant Growth",
        RAMPANT_GROWTH.mana.mana_cost,
        2,
        "Sorcery",
        RAMPANT_GROWTH.mana.oracle_text,
        roles=(RAMP,),
        tags=("alternative-cost", "overrun"),
    )
    assert not one(tagged).modelled


def test_interaction_and_ramp_for_the_opponent_are_plain_modelled_spells() -> None:
    path = one(PATH_TO_EXILE)
    assert path.modelled
    assert (path.mana, path.land_ramp, path.tutor, path.draw) == (None,) * 4


@pytest.mark.parametrize(
    "unplaced",
    [
        SMOTHERING_TITHE,
        PRIEST_OF_TITANIA,
        DOCKSIDE,
        JEWELED_LOTUS,
        CULLING_RITUAL,
    ],
    ids=lambda c: c.mana.name,
)
def test_ramp_the_reading_cannot_place_is_not_modelled(unplaced: Real) -> None:
    card = one(unplaced)
    assert card.mana is None
    assert card.land_ramp is None
    assert not card.modelled


HOARDMASTER = real(
    "Magda, the Hoardmaster",
    "{1}{R}",
    2,
    "Legendary Creature — Dwarf Berserker",
    "Whenever you commit a crime, create a tapped Treasure token. This ability triggers only once each turn. "
    "(Targeting opponents, anything they control, and/or cards in their graveyards is a crime.)\n"
    "Sacrifice three Treasures: Create a 4/4 red Scorpion Dragon creature token with flying and haste. "
    "Activate only as a sorcery.",
    identity="R",
    roles=(RAMP,),
)


def test_a_commander_the_reading_cannot_place_is_named_in_not_modelled() -> None:
    deck = build(BATTERED_GOLEM, commander=HOARDMASTER)
    assert [c.name for c in deck.commanders] == ["Magda, the Hoardmaster"]
    assert deck.not_modelled == ("Magda, the Hoardmaster",)


def test_a_treasure_for_each_dwarf_tapped_and_a_search_for_five_treasures_are_read() -> None:
    magda = card_named(build(BATTERED_GOLEM, commander=MAGDA), "Magda, Brazen Outlaw")
    assert magda.treasure_on_tap == {"dwarf"}
    # No card of the deck is a Dragon, but the word is the other choice beside a card type.
    dragon = frozenset({"dragon"})
    tutor = Tutor(frozenset({CardKind.ARTIFACT}), TutorDestination.BATTLEFIELD, subtypes=dragon, either=True)
    assert magda.activated_tutor == ActivatedTutor(tutor, treasures=5)
    assert magda.tutor is None
    assert magda.modelled


TEZZERET = real(
    "Tezzeret, Cruel Captain",
    "{3}",
    3,
    "Legendary Planeswalker — Tezzeret",
    "Whenever an artifact you control enters, put a loyalty counter on Tezzeret.\n"
    "0: Untap target artifact or creature. If it's an artifact creature, put a +1/+1 counter on it.\n"
    "\N{MINUS SIGN}3: Search your library for an artifact card with mana value 1 or less, reveal it, put it "
    "into your hand, then shuffle.",
    roles=(TUTOR,),
)


def test_a_loyalty_search_no_dearer_than_the_card_is_read_as_payable_once() -> None:
    tezzeret = one(TEZZERET)
    tutor = Tutor(frozenset({CardKind.ARTIFACT}), TutorDestination.HAND, 1)
    assert tezzeret.activated_tutor == ActivatedTutor(tutor, loyalty=3)
    assert tezzeret.modelled
    dearer = replace(TEZZERET, mana=replace(TEZZERET.mana, mana_cost="{2}", cmc=2))
    assert one(dearer).activated_tutor is None
    assert not one(dearer).modelled


PLUNDERING_BARBARIAN = real(
    "Plundering Barbarian",
    "{2}{R}",
    3,
    "Creature — Dwarf Barbarian",
    "When this creature enters, choose one —\n• Smash the Chest — Destroy target artifact.\n"
    "• Pry It Open — Create a Treasure token. (It's an artifact with \"{T}, Sacrifice this token: Add one "
    'mana of any color.")',
    roles=(RAMP,),
)
LANNERY_STORM = real(
    "Captain Lannery Storm",
    "{2}{R}",
    3,
    "Legendary Creature — Human Pirate",
    "Haste\nWhenever Captain Lannery Storm attacks, create a Treasure token. (It's an artifact with \"{T}, "
    'Sacrifice this token: Add one mana of any color.")\nWhenever you sacrifice a Treasure, Captain Lannery '
    "Storm gets +1/+0 until end of turn.",
    roles=(RAMP,),
)
FACE_BREAKER = real(
    "Professional Face-Breaker",
    "{2}{R}",
    3,
    "Creature — Human Warrior",
    "Menace\nWhenever one or more creatures you control deal combat damage to a player, create a Treasure "
    "token.\nSacrifice a Treasure: Exile the top card of your library. You may play that card this turn.",
    roles=(RAMP,),
)


def test_treasures_made_on_entering_or_attacking_are_read_and_combat_damage_is_not() -> None:
    barbarian = one(PLUNDERING_BARBARIAN)
    assert (barbarian.treasures_on_entry, barbarian.treasures_on_attack) == (1, 0)
    assert barbarian.modelled
    storm = one(LANNERY_STORM)
    assert (storm.treasures_on_entry, storm.treasures_on_attack) == (0, 1)
    assert storm.modelled
    breaker = one(FACE_BREAKER)
    assert (breaker.treasures_on_entry, breaker.treasures_on_attack, breaker.treasure_on_tap) == (0, 0, set())
    assert not breaker.modelled


def test_a_card_missing_from_the_card_data_is_a_plain_spell_of_average_value() -> None:
    deck = build(SOL_RING, CULTIVATE, missing=("Rhystic Study",))
    study = card_named(deck, "Rhystic Study")
    assert study.oracle_id == ""
    assert study.mana_value == 2
    assert study.pips == {}
    assert not study.modelled
    assert (study.mana, study.land, study.land_ramp, study.tutor, study.draw) == (None,) * 5
    assert deck.not_modelled == ("Rhystic Study",)


def test_a_card_with_no_text_is_a_plain_modelled_spell() -> None:
    bears = one(GRIZZLY_BEARS)
    assert bears.modelled
    assert (bears.mana, bears.land, bears.land_ramp, bears.tutor, bears.draw) == (None,) * 5
    assert (bears.mana_value, bears.pips) == (2, {"G": 1})


# --- the deck -------------------------------------------------------------------------------------


def test_the_commander_is_apart_from_the_99_and_the_average_counts_only_the_99s_spells() -> None:
    deck = build(SOL_RING, CULTIVATE, FOREST, copies={"Forest": 36})
    assert [c.name for c in deck.commanders] == ["Sliver Legion"]
    assert deck.commanders[0].commander
    assert [c.name for c in deck.cards] == ["Sol Ring", "Cultivate", "Forest"]
    assert not any(c.commander for c in deck.cards)
    assert deck.average_mana_value == 2.0
    assert deck.not_modelled == ()


def test_a_card_in_two_categories_is_one_record_with_the_copies_added() -> None:
    deck = compile_for(deck_of(("Forest", 20), ("Forest", 16), ("Sol Ring", 1)), [LEGION, FOREST, SOL_RING])
    assert [(c.name, c.copies) for c in deck.cards] == [("Forest", 36), ("Sol Ring", 1)]
