"""A deck's mana base against Karsten's land count formula and colour source table."""

import pytest

from commander.carddb import CardMana
from commander.manabase import (
    Casting,
    Need,
    analyse_mana,
    castings,
    cheap_draw,
    cheap_ramp,
    land_count,
    mana_only_land,
    needed_sources,
)
from commander.models import Deck, DeckCard


def card(
    name: str,
    cost: str = "",
    mv: float = 0,
    type_line: str = "Instant",
    *,
    text: str = "",
    produced: str = "",
    layout: str = "normal",
) -> CardMana:
    return CardMana(f"id-{name}", name, cost, mv, type_line, text, produced, "common", layout)


def deck_of(*cards: tuple[str, int], commander: str = "Leader") -> Deck:
    rows = [DeckCard(commander, 1, ("Commander",), True, "sid", 1, 1, "tst", "1", "Normal")]  # noqa: FBT003 — positional like the model
    rows += [DeckCard(name, n, ("Main",), True, "sid", 1, 1, "tst", "1", "Normal") for name, n in cards]  # noqa: FBT003 — as above
    return Deck(1, "test", "me", 3, "2026-10-16", tuple(rows))


def by_name(*cards: CardMana) -> dict[str, CardMana]:
    return {c.name.casefold(): c for c in cards}


LEADER = card("Leader", "{1}{W}{U}", 3, "Legendary Creature — Human")
PLAINS = card("Plains", type_line="Basic Land — Plains", produced="W")
ISLAND = card("Island", type_line="Basic Land — Island", produced="U")
BRAINSTORM = card("Brainstorm", "{U}", 1, text="Draw three cards, then put two cards from your hand on top.")
SIGNET = card("Azorius Signet", "{2}", 2, "Artifact", text="{1}, {T}: Add {W}{U}.", produced="WU")
WRATH = card("Wrath of God", "{2}{W}{W}", 4, "Sorcery", text="Destroy all creatures.")


def test_karstens_cheap_draw_and_ramp_follow_his_text_rules() -> None:
    assert cheap_draw(BRAINSTORM)
    assert cheap_draw(
        card("Elvish Visionary", "{1}{G}", 2, "Creature — Elf", text="When this enters, draw a card.")
    )
    # A creature that draws without an enters trigger is not one, nor is anything costing three.
    assert not cheap_draw(
        card("Looter", "{1}{U}", 2, "Creature — Merfolk", text="{T}: Draw a card, then discard.")
    )
    assert not cheap_draw(card("Divination", "{2}{U}", 3, "Sorcery", text="Draw two cards."))
    assert cheap_draw(card("Cycler", "{3}{B}", 4, "Creature", text="Cycling {B}"))
    assert cheap_ramp(SIGNET)
    assert cheap_ramp(
        card("Rampant Growth", "{1}{G}", 2, "Sorcery", text="Search your library for a basic land card")
    )
    assert not cheap_ramp(card("Elf", "{G}", 1, "Creature — Elf", text="When this dies, add {G}."))
    assert not cheap_ramp(
        card("Cultivate", "{2}{G}", 3, "Sorcery", text="Search your library for basic land cards")
    )


def test_the_land_count_formula_counts_land_spells_as_part_of_a_land() -> None:
    mdfc = CardMana("id", "Smash // Pass", "{X}{R}{R}", 2, "Sorcery // Land", "", "R", "mythic")
    count = land_count([(30, PLAINS), (2, mdfc), (1, BRAINSTORM), (1, SIGNET), (1, WRATH)])
    assert count.lands == pytest.approx(30 + 2 * 0.74)
    assert (count.land_spells, count.cheap_draw, count.cheap_ramp) == (2, 1, 1)
    assert count.average_mana_value == pytest.approx((2 + 2 + 1 + 2 + 4) / 5)
    assert count.suggested == pytest.approx(round(31.42 + 3.13 * (11 / 5) - 0.28 * 2, 1))


def test_sources_needed_come_from_the_table_and_its_nearest_entries() -> None:
    assert (needed_sources(4, 2), needed_sources(1, 1), needed_sources(3, 3)) == (26, 19, 36)
    # Past the table: an eight-drop with one pip needs what a six-drop does, five pips what four do.
    assert (needed_sources(8, 1), needed_sources(6, 5)) == (14, 36)


def test_each_colour_lists_the_cards_short_of_sources_scaled_to_the_land_count() -> None:
    deck = deck_of(
        ("Plains", 21), ("Island", 20), ("Wrath of God", 1), ("Brainstorm", 1), ("Azorius Signet", 1)
    )
    report = analyse_mana(deck, by_name(LEADER, PLAINS, ISLAND, WRATH, BRAINSTORM, SIGNET))
    white = report.colours[0]
    assert (white.colour, white.lands) == ("W", 21)
    wrath = next(need for need in white.needs if need.card == "Wrath of God")
    # 2CC needs 26 of 41 lands; the Signet adds 3/4 for a four-drop.
    assert (wrath.pattern, wrath.needed, wrath.have, wrath.short) == ("2CC", 26, 21.8, True)
    # The commander is gold: 18 + 1 white sources for a 3-drop with one white pip.
    leader = next(need for need in white.needs if need.card == "Leader")
    assert (leader.needed, leader.short) == (19, False)
    assert [need.card for need in white.shortfalls] == ["Wrath of God"]


def test_turn_one_counts_only_untapped_lands() -> None:
    tapped = card("Tapped Island", type_line="Land", text="This land enters tapped.", produced="U")
    deck = deck_of(("Island", 15), ("Tapped Island", 10), ("Plains", 16), ("Brainstorm", 1))
    report = analyse_mana(deck, by_name(LEADER, PLAINS, ISLAND, tapped, BRAINSTORM))
    blue = report.colours[1]
    assert blue.lands == 25
    # Brainstorm needs 19 blue sources on turn one; only the 15 Islands enter untapped.
    assert [(n.card, n.have, n.needed) for n in report.turn_one] == [("Brainstorm", 15, 19)]
    assert report.tapped_lands == 10


def test_a_split_card_is_judged_by_its_halves_not_by_its_combined_mana_value() -> None:
    # Printed mana value 4 (two plus two), but a half is cast for two; equal halves both count.
    split = card("Wear // Tear", "{1}{W} // {1}{U}", 4, "Instant // Instant", layout="split")
    report = analyse_mana(
        deck_of(("Plains", 19), ("Island", 18), ("Wear // Tear", 1)), by_name(LEADER, PLAINS, ISLAND, split)
    )
    needs = {c.colour: [n for n in c.needs if n.card == "Wear // Tear"] for c in report.colours}
    # A two-drop with one pip asks 19 of 41 lands, 17.1 of 37: white from the front half, blue from the back.
    assert needs == {
        "W": [Need("Wear // Tear", "1C", 17.1, 19.0)],
        "U": [Need("Wear // Tear", "1C", 17.1, 18.0)],
    }


def test_a_split_card_is_judged_by_its_most_expensive_half_alone() -> None:
    split = card("Gather // Scatter", "{W} // {2}{W}{W}", 5, "Instant // Instant", layout="split")
    tapped = card("Tapped Plains", type_line="Land", text="This land enters tapped.", produced="W")
    deck = deck_of(("Plains", 9), ("Tapped Plains", 10), ("Island", 18), ("Gather // Scatter", 1))
    report = analyse_mana(deck, by_name(LEADER, PLAINS, ISLAND, tapped, split))
    white = report.colours[0]
    # {2}{W}{W} asks 26 of 41 lands, 23.5 of 37; the {W} half is not asked about, so no turn-one check.
    assert [n for n in white.needs if n.card == "Gather // Scatter"] == [
        Need("Gather // Scatter", "2CC", 23.5, 19.0)
    ]
    assert report.turn_one == ()


def test_a_split_card_is_not_asked_for_the_colour_only_its_cheaper_half_uses() -> None:
    split = card("Dispel // Prayer", "{W} // {U}{U}", 3, "Instant // Instant", layout="split")
    report = analyse_mana(
        deck_of(("Plains", 19), ("Island", 18), ("Dispel // Prayer", 1)),
        by_name(LEADER, PLAINS, ISLAND, split),
    )
    needs = {c.colour: [n for n in c.needs if n.card == "Dispel // Prayer"] for c in report.colours}
    # {U}{U} asks 30 of 41 lands, 27.1 of 37.
    assert needs == {"W": [], "U": [Need("Dispel // Prayer", "CC", 27.1, 18.0)]}


def test_an_adventure_is_judged_by_its_creature_at_the_cards_mana_value() -> None:
    # Bonecrusher Giant's shape: the creature at three, the one-mana-cheaper adventure beside it.
    giant = card("Sunlit Giant // Flare", "{2}{W} // {1}{W}", 3, "Creature // Instant", layout="adventure")
    report = analyse_mana(
        deck_of(("Plains", 19), ("Island", 18), ("Sunlit Giant // Flare", 1)),
        by_name(LEADER, PLAINS, ISLAND, giant),
    )
    needs = [n for c in report.colours for n in c.needs if n.card == "Sunlit Giant // Flare"]
    # A three-drop with one pip asks 18 of 41 lands, 16.2 of 37, less than the adventure's 19 would.
    assert needs == [Need("Sunlit Giant // Flare", "2C", 16.2, 19.0)]


def test_an_adventures_one_mana_half_is_no_turn_one_play() -> None:
    tapped = card("Tapped Plains", type_line="Land", text="This land enters tapped.", produced="W")
    lands = (("Plains", 9), ("Tapped Plains", 10), ("Island", 18))
    beast = card("Lovestruck Giant // Pet", "{2}{W} // {W}", 3, "Creature // Sorcery", layout="adventure")
    deck = deck_of(*lands, ("Lovestruck Giant // Pet", 1))
    assert analyse_mana(deck, by_name(LEADER, PLAINS, ISLAND, tapped, beast)).turn_one == ()
    # Cast the creature for one and it is a one-drop: ten of the nineteen white sources enter tapped.
    drop = card("Giant Killer // Chop Down", "{W} // {2}{W}", 1, "Creature // Instant", layout="adventure")
    deck = deck_of(*lands, ("Giant Killer // Chop Down", 1))
    report = analyse_mana(deck, by_name(LEADER, PLAINS, ISLAND, tapped, drop))
    assert report.turn_one == (Need("Giant Killer // Chop Down", "C", 17.1, 9.0),)


@pytest.mark.parametrize(
    ("cost", "mana_value", "layout", "expected"),
    [
        ("{R} // {2}{R}", 3, "split", [Casting(3, {"R": 1})]),
        ("{2}{R} // {R}", 3, "split", [Casting(3, {"R": 1})]),
        ("{1}{R} // {1}{U}", 4, "split", [Casting(2, {"R": 1}), Casting(2, {"U": 1})]),
        ("{2}{R} // {1}{R}", 3, "adventure", [Casting(3, {"R": 1})]),
        ("{2}{R}", 3, "adventure", [Casting(3, {"R": 1})]),
        ("{2}{R}", 3, "modal_dfc", [Casting(3, {"R": 1})]),
        ("{2}{R} // {1}{U}", 3, "transform", [Casting(3, {"R": 1})]),
        ("{3}{R}", 4, "flip", [Casting(4, {"R": 1})]),
    ],
)
def test_how_a_card_is_judged_follows_its_layout(
    cost: str, mana_value: int, layout: str, expected: list[Casting]
) -> None:
    assert castings(card("Two Parts", cost, mana_value, layout=layout)) == expected


CUT_RIBBONS_TEXT = (
    "Cut deals 4 damage to target creature.\n//\n"
    "Aftermath (Cast this spell only from your graveyard. Then exile it.)\nEach opponent loses X life."
)
DEAD_GONE_TEXT = (
    "Dead deals 2 damage to target creature.\n//\nReturn target creature you control to its owner's hand."
)


def test_an_aftermath_card_is_judged_by_its_first_half_alone() -> None:
    # The graveyard half costs {X}{B}{B}, which would tie Cut's mana value 2 and ask for black sources.
    cost, kinds = "{1}{R} // {X}{B}{B}", "Sorcery // Sorcery"
    ribbons = card("Cut // Ribbons", cost, 4, kinds, text=CUT_RIBBONS_TEXT, layout="split")
    assert castings(ribbons) == [Casting(2, {"R": 1})]


def test_a_split_card_without_aftermath_keeps_its_most_expensive_half() -> None:
    cost, kinds = "{R} // {2}{R}", "Instant // Instant"
    dead = card("Dead // Gone", cost, 4, kinds, text=DEAD_GONE_TEXT, layout="split")
    assert castings(dead) == [Casting(3, {"R": 1})]


def test_fetches_choosing_lands_and_opponent_lands_count_as_karsten_says() -> None:
    fetch = card("Flooded Strand", type_line="Land", text="Search your library for a Plains or Island card")
    passage = card("Fabled Passage", type_line="Land", text="Search your library for a basic land card")
    orchard = card(
        "Exotic Orchard",
        type_line="Land",
        text="Add one mana of any color that a land an opponent controls could produce.",
        produced="WUBRG",
    )
    three = card("Leader", "{W}{U}{B}", 3, "Legendary Creature")
    deck = deck_of(("Flooded Strand", 1), ("Fabled Passage", 3), ("Exotic Orchard", 4))
    report = analyse_mana(deck, by_name(three, fetch, passage, orchard))
    lands = {c.colour: c.lands for c in report.colours}
    # Strand: 1 W and 1 U; Passage: 2/3 each in a three-colour deck; Orchard: 3/4 each.
    assert lands == {"W": 1 + 2 + 3, "U": 1 + 2 + 3, "B": 2 + 3}


def test_a_rock_that_depends_on_opponents_counts_half_a_source() -> None:
    stone = card(
        "Fellwar Stone",
        "{2}",
        2,
        "Artifact",
        text="{T}: Add one mana of any color that a land an opponent controls could produce.",
        produced="WUBRG",
    )
    deck = deck_of(("Plains", 20), ("Fellwar Stone", 1), ("Wrath of God", 1))
    report = analyse_mana(deck, by_name(LEADER, PLAINS, WRATH, stone))
    wrath = next(need for need in report.colours[0].needs if need.card == "Wrath of God")
    # Fellwar Stone is half a source, where a plain rock is three-quarters.
    assert wrath.have == 20.5


def test_pitch_spells_are_left_out_and_lists_compared_by_median() -> None:
    force = card(
        "Force of Will", "{3}{U}{U}", 5, text="You may pay 1 life and exile a blue card rather than pay."
    )
    deck = deck_of(("Island", 30), ("Force of Will", 1), ("Azorius Signet", 1))
    lists = [{"Island": 28, "Azorius Signet": 1}, {"Island": 30}, {"Island": 26, "Azorius Signet": 1}]
    report = analyse_mana(
        deck,
        by_name(LEADER, ISLAND, force, SIGNET),
        alternative_cost=["Force of Will"],
        comparison=("top-cut lists", lists),
    )
    assert report.skipped == ("Force of Will",)
    assert all(need.card != "Force of Will" for colour in report.colours for need in colour.needs)
    compared = report.comparison
    assert compared is not None
    assert (compared.lists, compared.lands, compared.lands_low, compared.lands_high) == (3, 28, 26, 30)
    assert (compared.mana_cards, compared.deck_mana_cards) == (1, 1)


FOUNTAIN = card(
    "Hallowed Fountain",
    type_line="Land — Plains Island",
    text="({T}: Add {W} or {U}.)\nAs this land enters, you may pay 2 life. If you don't, it enters tapped.",
    produced="WU",
)


def test_a_shock_land_counts_as_untapped() -> None:
    report = analyse_mana(deck_of(("Hallowed Fountain", 1), ("Plains", 1)), by_name(LEADER, FOUNTAIN, PLAINS))
    assert report.tapped_lands == 0


def test_lands_that_fetch_tapped_or_enter_tapped_on_a_count_are_tapped_or_sometimes_tapped() -> None:
    wilds = card(
        "Evolving Wilds",
        type_line="Land",
        text="{T}, Sacrifice this land: Search your library for a basic land card, put it onto the "
        "battlefield tapped.",
    )
    cliffs = card(
        "Blackcleave Cliffs",
        type_line="Land",
        text="This land enters tapped unless you control two or fewer other lands.\n{T}: Add {B} or {R}.",
        produced="BR",
    )
    report = analyse_mana(
        deck_of(("Evolving Wilds", 1), ("Blackcleave Cliffs", 4)), by_name(LEADER, wilds, cliffs)
    )
    assert report.tapped_lands == 1 + 4 * 0.25


FILLER = card("Filler", "{2}", 2, "Artifact")
CLIFFTOP = card(
    "Clifftop Retreat",
    type_line="Land",
    text="This land enters tapped unless you control a Mountain or a Plains.\n{T}: Add {R} or {W}.",
    produced="RW",
)
SNARL = card(
    "Furycalm Snarl",
    type_line="Land",
    text="As this land enters, you may reveal a Mountain or Plains card from your hand. If you don't, "
    "this land enters tapped.\n{T}: Add {R} or {W}.",
    produced="RW",
)


def test_a_check_land_is_mostly_untapped_in_a_deck_with_many_lands_of_its_types() -> None:
    deck = deck_of(("Clifftop Retreat", 1), ("Plains", 3), ("Filler", 16))
    report = analyse_mana(deck, by_name(LEADER, CLIFFTOP, PLAINS, FILLER))
    # 3 Plains among the other 19 cards, 9 seen by turn three: none drawn is C(16,9) / C(19,9) = 720 / 5814.
    assert report.tapped_lands == 0.12


def test_a_check_land_is_mostly_tapped_in_a_deck_with_almost_none() -> None:
    deck = deck_of(("Clifftop Retreat", 1), ("Plains", 1), ("Filler", 18))
    report = analyse_mana(deck, by_name(LEADER, CLIFFTOP, PLAINS, FILLER))
    # One Plains among the other 19 cards: it is missed with probability 10 / 19.
    assert report.tapped_lands == 0.53


def test_a_reveal_land_counts_typed_duals_and_leaves_out_other_basic_types() -> None:
    forest = card("Forest", type_line="Basic Land — Forest", produced="G")
    tundra = card("Tundra", type_line="Land — Plains Island", text="({T}: Add {W} or {U}.)", produced="WU")
    deck = deck_of(("Furycalm Snarl", 1), ("Tundra", 2), ("Forest", 5), ("Filler", 12))
    report = analyse_mana(deck, by_name(LEADER, SNARL, tundra, forest, FILLER))
    # Two Plains-typed cards among the other 19 (the Forests do not count): missed with C(17,9) / C(19,9).
    assert report.tapped_lands == 0.26


def test_cards_with_only_hybrid_or_phyrexian_pips_are_listed_as_not_counted() -> None:
    hybrid = card("Boros Charm Twin", "{2}{W/U}{W/U}", 4, "Sorcery")
    phyrexian = card("Gut Shot Twin", "{R/P}", 1, "Instant")
    # One fixed pip beside a hybrid one is still counted, at the fixed one.
    mixed = card("Mixed Bag", "{W}{W/U}", 2, "Instant")
    deck = deck_of(("Plains", 20), ("Boros Charm Twin", 1), ("Gut Shot Twin", 1), ("Mixed Bag", 1))
    report = analyse_mana(deck, by_name(LEADER, PLAINS, hybrid, phyrexian, mixed))
    assert report.skipped == ("Boros Charm Twin", "Gut Shot Twin")
    assert any(need.card == "Mixed Bag" for colour in report.colours for need in colour.needs)


def oracle(*lines: str) -> str:
    return "\n".join(lines)


# Oracle texts as Scryfall's bulk data has them (this land for the card's name).
MANA_ONLY = {
    "Overgrown Tomb": oracle(
        "({T}: Add {B} or {G}.)", "As this land enters, you may pay 2 life. If you don't, it enters tapped."
    ),
    "Woodland Cemetery": oracle(
        "This land enters tapped unless you control a Swamp or a Forest.", "{T}: Add {B} or {G}."
    ),
    "Llanowar Wastes": oracle("{T}: Add {C}.", "{T}: Add {B} or {G}. This land deals 1 damage to you."),
    "Command Tower": oracle("{T}: Add one mana of any color in your commander's color identity."),
    "Exotic Orchard": oracle(
        "{T}: Add one mana of any color that a land an opponent controls could produce."
    ),
    "Branchloft Pathway // Boulderloft Pathway": oracle("{T}: Add {G}.", "//", "{T}: Add {W}."),
    "Port Town": oracle(
        "As this land enters, you may reveal a Plains or Island card from your hand. If you don't, this land enters tapped.",  # noqa: E501 — an oracle text
        "{T}: Add {W} or {U}.",
    ),
    "Underground Sea": oracle("({T}: Add {U} or {B}.)"),
    "Azorius Guildgate": oracle("This land enters tapped.", "{T}: Add {W} or {U}."),
    # A filter land turns one mana into two, so it nets one.
    "Graven Cairns": oracle("{T}: Add {C}.", "{B/R}, {T}: Add {B}{B}, {B}{R}, or {R}{R}."),
    "Sungrass Prairie": oracle("{1}, {T}: Add {G}{W}."),
}
UTILITY = {
    "Vesuva": oracle("You may have this land enter tapped as a copy of any land on the battlefield."),
    "Field of the Dead": oracle(
        "This land enters tapped.",
        "{T}: Add {C}.",
        "Whenever this land or another land you control enters, if you control seven or more lands with different names, create a 2/2 black Zombie creature token.",  # noqa: E501 — an oracle text
    ),
    "Mirrorpool": oracle(
        "This land enters tapped.",
        "{T}: Add {C}.",
        "{2}{C}, {T}, Sacrifice this land: Copy target instant or sorcery spell you control. You may choose new targets for the copy.",  # noqa: E501 — an oracle text
        "{4}{C}, {T}, Sacrifice this land: Create a token that's a copy of target creature you control.",
    ),
    "Verdant Catacombs": oracle(
        "{T}, Pay 1 life, Sacrifice this land: Search your library for a Swamp or Forest card, put it onto the battlefield, then shuffle."  # noqa: E501 — an oracle text
    ),
    "Mishra's Factory": oracle(
        "{T}: Add {C}.",
        "{1}: This land becomes a 2/2 Assembly-Worker artifact creature until end of turn. It's still a land.",  # noqa: E501 — an oracle text
        "{T}: Target Assembly-Worker creature gets +1/+1 until end of turn.",
    ),
    "Tranquil Thicket": oracle(
        "This land enters tapped.", "{T}: Add {G}.", "Cycling {G} ({G}, Discard this card: Draw a card.)"
    ),
    "Gruul Turf": oracle(
        "This land enters tapped.",
        "When this land enters, return a land you control to its owner's hand.",
        "{T}: Add {R}{G}.",
    ),
    "Evolving Wilds": oracle(
        "{T}, Sacrifice this land: Search your library for a basic land card, put it onto the battlefield tapped, then shuffle."  # noqa: E501 — an oracle text
    ),
    "Cavern of Souls": oracle(
        "As this land enters, choose a creature type.",
        "{T}: Add {C}.",
        "{T}: Add one mana of any color. Spend this mana only to cast a creature spell of the chosen type, and that spell can't be countered.",  # noqa: E501 — an oracle text
    ),
    # Lands whose mana ability can make more than one mana, from the card database.
    "Gaea's Cradle": oracle("{T}: Add {G} for each creature you control."),
    "Ancient Tomb": oracle("{T}: Add {C}{C}. This land deals 2 damage to you."),
    "Cabal Coffers": oracle("{2}, {T}: Add {B} for each Swamp you control."),
    "Nykthos, Shrine to Nyx": oracle(
        "{T}: Add {C}.",
        "{2}, {T}: Choose a color. Add an amount of mana of that color equal to your devotion to that color. (Your devotion to a color is the number of mana symbols of that color in the mana costs of permanents you control.)",  # noqa: E501 — an oracle text
    ),
    "Urza's Tower": oracle(
        "{T}: Add {C}. If you control an Urza's Mine and an Urza's Power-Plant, add {C}{C}{C} instead."
    ),
    "Baldur's Gate": oracle(
        "{T}: Add {C}.",
        "{2}, {T}: Add X mana of any one color, where X is the number of other Gates you control.",
    ),
    "Unknown": "",
}


@pytest.mark.parametrize("name", list(MANA_ONLY))
def test_a_land_that_only_makes_mana_or_enters_tapped_is_mana_only(name: str) -> None:
    assert mana_only_land(card(name, type_line="Land", text=MANA_ONLY[name]))


@pytest.mark.parametrize("name", list(UTILITY))
def test_a_land_with_any_other_ability_or_no_known_text_is_not_mana_only(name: str) -> None:
    assert not mana_only_land(card(name, type_line="Land", text=UTILITY[name]))


def test_a_land_with_a_spell_half_is_a_utility_land_but_a_land_on_both_halves_is_not() -> None:
    midgar = oracle(
        "This land enters tapped.",
        "{T}: Add {B}.",
        "//",
        "You may sacrifice an artifact or creature. If you do, draw two cards. (Then exile this card. You may play the land later from exile.)",  # noqa: E501 — an oracle text
    )
    adventure = card(
        "Midgar, City of Mako // Reactor Raid", type_line="Land — Town // Sorcery — Adventure", text=midgar
    )
    assert not mana_only_land(adventure)
    pathway = oracle("{T}: Add {G}.", "//", "{T}: Add {W}.")
    both = card("Branchloft Pathway // Boulderloft Pathway", type_line="Land // Land", text=pathway)
    assert mana_only_land(both)
