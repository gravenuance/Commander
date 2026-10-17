"""The suggested mana plan: land count, basics, named lands and tap-land swaps (ADR 0024)."""

from dataclasses import replace

import pytest

from commander.manabase import analyse_mana
from commander.manaplan import BasicCount, ColourOutcome, LandOption, Swap, suggest_mana
from commander.models import Deck
from test_manabase import FOUNTAIN, ISLAND, LEADER, PLAINS, by_name, card, deck_of

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
TUNDRA = card("Tundra", type_line="Land — Plains Island", text="({T}: Add {W} or {U}.)", produced="WU")
FORTRESS = card(
    "Glacial Fortress",
    type_line="Land",
    text="This land enters tapped unless you control a Plains or an Island.\n{T}: Add {W} or {U}.",
    produced="WU",
)
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


def test_a_colourless_deck_keeps_its_land_count_as_no_basic_can_be_added() -> None:
    leader = card("Leader", "{3}", 3, "Legendary Artifact Creature — Golem")
    waste = card("Ruins", type_line="Land", produced="C")
    deck = deck_of(("Ruins", 20), ("Filler", 79))
    cards = by_name(leader, waste, FILLER)
    plan = suggest_mana(deck, cards, analyse_mana(deck, cards), options=())
    # Karsten wants 31.42 + 3.13 x 2 = 37.7 lands, but no basic land type can be added for a deck with no
    # coloured pips, and the spells the plan would cut have no land to take their place.
    assert (plan.lands, plan.target) == (20, 37.7)
    assert plan.land_change == 0
    assert plan.cut_candidates == ()
    assert plan.unchanged


def test_lands_are_cut_only_as_far_as_the_basics_go() -> None:
    deck = deck_of(("Tundra", 43), ("Plains", 2), ("Filler", 54))
    cards = by_name(*KNOWN)
    plan = suggest_mana(deck, cards, analyse_mana(deck, cards), options=())
    # 45 lands against 37.7 is seven too many, but only the two Plains can go.
    assert (plan.lands, plan.target, plan.land_change) == (45, 37.7, -2)
    assert plan.basics[0] == BasicCount("W", "Plains", 2, 0)


@pytest.mark.parametrize(
    ("lands", "spells", "target", "change"),
    [
        # Fifteen spells of total mana value 29 make 31.42 + 3.13 x 29/15 = 37.5.
        (35, (14, 1), 37.5, 3),  # 2.5 short: halves round away from zero
        (40, (14, 1), 37.5, -3),  # 2.5 over
        (36, (14, 1), 37.5, 2),  # 1.5 short
        # Ten spells of total mana value 21 make 38.0.
        (37, (9, 3), 38.0, 0),  # exactly one short is within the tolerance
        (36, (9, 3), 38.0, 2),  # two short
    ],
)
def test_the_land_change_rounds_halves_away_from_zero(
    lands: int, spells: tuple[int, int], target: float, change: int
) -> None:
    # Nine or fourteen two-drops and one more spell, so the average mana value is what the target needs.
    twos, other = spells
    one_more = card("Odd", "{" + str(other) + "}", other, "Artifact")
    deck = deck_of(("Plains", lands // 2 + lands % 2), ("Island", lands // 2), ("Filler", twos), ("Odd", 1))
    cards = by_name(*KNOWN, one_more)
    plan = suggest_mana(deck, cards, analyse_mana(deck, cards), options=())
    assert (plan.lands, plan.target, plan.land_change) == (lands, target, change)


@pytest.mark.parametrize(
    ("lands", "spells", "suggested", "change"),
    [
        # Just under 2.5 short and just over one short: float noise from the formula, not a real gap.
        (35, (14, 1), 37.5 - 1e-13, 3),
        (37, (9, 3), 38.0 + 1e-13, 0),
    ],
)
def test_float_noise_in_the_suggested_count_does_not_move_a_land_change_edge(
    lands: int, spells: tuple[int, int], suggested: float, change: int
) -> None:
    twos, other = spells
    one_more = card("Odd", "{" + str(other) + "}", other, "Artifact")
    deck = deck_of(("Plains", lands // 2 + lands % 2), ("Island", lands // 2), ("Filler", twos), ("Odd", 1))
    cards = by_name(*KNOWN, one_more)
    report = analyse_mana(deck, cards)
    noisy = replace(report, lands=replace(report.lands, suggested=suggested))
    assert suggest_mana(deck, cards, noisy, options=()).land_change == change


def test_a_tap_land_making_none_of_the_decks_colours_gets_no_twin() -> None:
    ruins = card(
        "Tapped Ruins", type_line="Land", text="This land enters tapped.\n{T}: Add {C}.", produced="C"
    )
    deck = deck_of(("Plains", 18), ("Island", 18), ("Tapped Ruins", 1), ("Filler", 60))
    cards = by_name(*KNOWN, ruins)
    plan = suggest_mana(deck, cards, analyse_mana(deck, cards), options=OPTIONS)
    # An untapped Tundra is no twin of a land that makes only colourless mana, which the reason would claim.
    assert plan.swaps == ()


def test_a_check_land_is_taken_or_refused_alike_to_close_a_gap_and_to_replace_a_tap_land() -> None:
    cards = by_name(*KNOWN, FORTRESS)
    options = (LandOption(FORTRESS, 1500),)
    gap = deck_of(("Plains", 30), ("Island", 8), ("Filler", 60), ("Counterspell", 1))
    twin = deck_of(("Plains", 18), ("Island", 18), ("Azorius Guildgate", 1), ("Filler", 60))
    # 36 or more Plains and Islands among the 99: the Fortress is untapped about every game.
    named = suggest_mana(gap, cards, analyse_mana(gap, cards), options=options)
    assert [swap.add for swap in named.swaps] == ["Island", "Glacial Fortress"]
    swapped = suggest_mana(twin, cards, analyse_mana(twin, cards), options=options)
    assert swapped.swaps == (
        Swap("Glacial Fortress", 1, "Azorius Guildgate", "same colours, enters untapped", 1500),
    )
    # Five Plains and Islands among 86 cards: it enters tapped more often than not, so neither step offers it.
    sparse = deck_of(
        ("Plains", 4), ("Island", 1), ("Azorius Guildgate", 1), ("Filler", 80), ("Counterspell", 1)
    )
    lists = [{"Plains": 4, "Island": 1}]
    report = analyse_mana(sparse, cards, comparison=("recent decks", lists))
    refused = suggest_mana(sparse, cards, report, options=options)
    assert all(swap.add != "Glacial Fortress" for swap in refused.swaps)
