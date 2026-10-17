"""A deck's mana base against Karsten's land count formula and colour source table."""

import pytest

from commander.carddb import CardMana
from commander.manabase import (
    BasicCount,
    ColourOutcome,
    LandOption,
    Swap,
    analyse_mana,
    cheap_draw,
    cheap_ramp,
    land_count,
    needed_sources,
    suggest_mana,
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
) -> CardMana:
    return CardMana(f"id-{name}", name, cost, mv, type_line, text, produced, "common")


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


# For the suggestion: a colourless two-drop that makes no mana keeps the average mana value at 2.
FILLER = card("Filler", "{2}", 2, "Artifact")
COUNTERSPELL = card("Counterspell", "{U}{U}", 2, text="Counter target spell.")
GUILDGATE = card(
    "Azorius Guildgate",
    type_line="Land — Gate",
    text="This land enters tapped.\n{T}: Add {W} or {U}.",
    produced="WU",
)
CHANCERY = card(
    "Azorius Chancery",
    type_line="Land",
    text="This land enters tapped.\nWhen this land enters, return a land you control to its owner's hand.\n"
    "{T}: Add {W}{U}.",
    produced="WU",
)
FOUNTAIN = card(
    "Hallowed Fountain",
    type_line="Land — Plains Island",
    text="({T}: Add {W} or {U}.)\nAs this land enters, you may pay 2 life. If you don't, it enters tapped.",
    produced="WU",
)
TUNDRA = card("Tundra", type_line="Land — Plains Island", text="({T}: Add {W} or {U}.)", produced="WU")
# Most played first: the Guildgate enters tapped, so a gap passes it by.
OPTIONS = (LandOption(GUILDGATE, 25), LandOption(FOUNTAIN, 1500), LandOption(TUNDRA, 40000))
KNOWN = (LEADER, PLAINS, ISLAND, FILLER, COUNTERSPELL, GUILDGATE, CHANCERY, FOUNTAIN, TUNDRA)


def suggestion(deck: Deck, lists: list[dict[str, int]] | None = None):  # noqa: ANN201 — the plan, inferred
    cards = by_name(*KNOWN)
    report = analyse_mana(deck, cards, comparison=("recent decks", lists) if lists else None)
    return suggest_mana(deck, cards, report, options=OPTIONS)


def test_basics_move_to_the_short_colour_and_untapped_duals_close_what_is_left() -> None:
    plan = suggestion(deck_of(("Plains", 30), ("Island", 8), ("Filler", 60), ("Counterspell", 1)))
    # 38 lands against Karsten's 31.42 + 3.13 x 2 = 37.7: close enough to leave alone.
    assert (plan.lands, plan.target, plan.land_change) == (38, 37.7, 0)
    # White needs 18 x 38/41 + 1 = 17.7 for the gold commander; blue 30 x 38/41 = 27.8 for Counterspell.
    # Plains go to Islands while white stays met (18 is the last), then two shock or dual lands each
    # replace a Plains, as they make white too.
    assert plan.swaps == (
        Swap("Island", 12, "Plains", "Blue is short of sources"),
        Swap("Hallowed Fountain", 1, "Plains", "Blue is short of sources; enters untapped", 1500),
        Swap("Tundra", 1, "Plains", "Blue is short of sources; enters untapped", 40000),
    )
    # Twelve Plains become Islands; the two lands each take a further Plains, counted apart.
    assert plan.basics == (BasicCount("W", "Plains", 30, 18, 2), BasicCount("U", "Island", 8, 20))
    # Blue: 22 sources after, still short of Counterspell's 27.8; the commander is met.
    assert plan.colours == (ColourOutcome("W", 0, 0), ColourOutcome("U", 2, 1))
    assert plan.still_short == ("Counterspell",)


def test_a_plain_tap_land_gets_an_untapped_land_of_the_same_colours() -> None:
    plan = suggestion(
        deck_of(
            ("Plains", 18), ("Island", 18), ("Azorius Guildgate", 1), ("Azorius Chancery", 1), ("Filler", 60)
        )
    )
    # Both colours are met, so only the Guildgate changes; the Chancery's bounce is worth its slow turn.
    assert plan.swaps == (
        Swap("Hallowed Fountain", 1, "Azorius Guildgate", "same colours, enters untapped", 1500),
    )
    assert not plan.unchanged


def test_the_land_count_moves_only_when_off_karsten_and_outside_the_other_lists() -> None:
    big = card("Ultimatum", "{7}", 7, "Sorcery")
    middle = card("Middle", "{4}", 4, "Sorcery")
    deck = deck_of(("Plains", 15), ("Island", 15), ("Filler", 67), ("Ultimatum", 1), ("Middle", 1))
    cards = by_name(*KNOWN, big, middle)
    report = analyse_mana(deck, cards)
    plan = suggest_mana(deck, cards, report, options=OPTIONS)
    # Average (67 x 2 + 7 + 4) / 69 = 2.10, so 31.42 + 3.13 x 2.10 = 38.0: eight more lands.
    assert (plan.target, plan.land_change) == (38.0, 8)
    # The costliest spells make room; the new basics go four to each colour, both 2.7 short at 38 lands.
    assert plan.cut_candidates == ("Ultimatum", "Middle", "Filler")
    assert plan.basics == (BasicCount("W", "Plains", 15, 19), BasicCount("U", "Island", 15, 19))
    assert plan.swaps == ()
    # Recent decks running 28 to 32 lands put 30 inside their range: the count stays.
    lists = [{"Plains": 14, "Island": 14}, {"Plains": 16, "Island": 16}]
    compared = suggest_mana(
        deck, cards, analyse_mana(deck, cards, comparison=("recent decks", lists)), options=OPTIONS
    )
    assert compared.land_change == 0


def test_a_shock_land_counts_as_untapped() -> None:
    report = analyse_mana(deck_of(("Hallowed Fountain", 1), ("Plains", 1)), by_name(LEADER, FOUNTAIN, PLAINS))
    assert report.tapped_lands == 0


def test_lands_that_fetch_tapped_or_reveal_count_as_tapped_or_sometimes_tapped() -> None:
    wilds = card(
        "Evolving Wilds",
        type_line="Land",
        text="{T}, Sacrifice this land: Search your library for a basic land card, put it onto the "
        "battlefield tapped.",
    )
    snarl = card(
        "Furycalm Snarl",
        type_line="Land",
        text="As this land enters, you may reveal a Mountain or Plains card from your hand. If you don't, "
        "this land enters tapped.\n{T}: Add {R} or {W}.",
        produced="RW",
    )
    report = analyse_mana(
        deck_of(("Evolving Wilds", 1), ("Furycalm Snarl", 4)), by_name(LEADER, wilds, snarl)
    )
    assert report.tapped_lands == 1 + 4 * 0.25


def test_a_land_whose_mana_casts_only_some_spells_is_never_offered() -> None:
    cavern = card(
        "Cavern of Souls",
        type_line="Land",
        text="{T}: Add one mana of any color. Spend this mana only to cast a creature spell of the "
        "chosen type.",
        produced="WUBRG",
    )
    deck = deck_of(("Plains", 30), ("Island", 8), ("Filler", 60), ("Counterspell", 1))
    cards = by_name(*KNOWN, cavern)
    plan = suggest_mana(deck, cards, analyse_mana(deck, cards), options=(LandOption(cavern, 2000),))
    assert [swap.add for swap in plan.swaps] == ["Island"]


def test_lands_within_the_budget_come_first_and_dearer_ones_are_marked() -> None:
    deck = deck_of(("Plains", 30), ("Island", 8), ("Filler", 60), ("Counterspell", 1))
    cards = by_name(*KNOWN)
    # Tundra is the more played but over the 20.00 budget, so the Fountain comes first; blue is still
    # short after it, so Tundra follows, marked over budget.
    options = (LandOption(TUNDRA, 40000), LandOption(FOUNTAIN, 1500))
    plan = suggest_mana(deck, cards, analyse_mana(deck, cards), options=options, budget_cents=2000)
    assert [(s.add, s.within_budget) for s in plan.swaps] == [
        ("Island", True),
        ("Hallowed Fountain", True),
        ("Tundra", False),
    ]
    assert plan.budget_cents == 2000
