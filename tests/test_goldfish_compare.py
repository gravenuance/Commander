"""The paired comparison of a swap (ADR 0054 "Swaps"): only the games a swap can change are played again, and
the result is the same as playing the swapped deck whole on the same seed."""

import dataclasses
from collections.abc import Sequence

import pytest

from commander import goldfish
from commander.goldfish import (
    FigureKind,
    GoldfishComparison,
    GoldfishReport,
    PartialSwap,
    Swap,
    compare,
    compare_deck,
    deck_slots,
    is_real_shift,
    simulate,
    swapped_average,
)
from commander.goldfish import (
    goldfish as play_deck,
)
from commander.goldfish_cards import (
    CardKind,
    Draw,
    GoldfishCard,
    GoldfishCombo,
    GoldfishDeck,
    LandDestination,
    LandRamp,
    ManaAbility,
    Tutor,
    TutorDestination,
)
from test_goldfish import CREATURE, FOREST, SOL_RING, VANILLA, land, make, repeat, run

SEED = 21
GAMES = 400
SWAMP = land("Swamp", "B")
GOLGARI = land("Overgrown Tomb", "GB", tapped=True)
WILDS = land("Evolving Wilds", "GB", tapped=True)
ARTIFACT = frozenset({CardKind.ARTIFACT})
SIGNET = make(
    "Arcane Signet", mv=2, kinds=ARTIFACT, mana=ManaAbility(1, frozenset("GB"), delayed=False, ritual=False)
)
TALISMAN = make(
    "Talisman of Growth",
    mv=2,
    kinds=ARTIFACT,
    mana=ManaAbility(1, frozenset("GB"), delayed=False, ritual=False),
)
LLANOWAR = make(
    "Llanowar Elves",
    mv=1,
    pips={"G": 1},
    kinds=CREATURE,
    mana=ManaAbility(1, frozenset("G"), delayed=True, ritual=False),
)
CULTIVATE = dataclasses.replace(
    make("Cultivate", mv=3, pips={"G": 1}),
    land_ramp=LandRamp(2, basic_only=True, destination=LandDestination.TAPPED, to_hand=1),
)
TUTOR = make("Worldly Tutor", mv=2, pips={"B": 1}, tutor=Tutor(frozenset(), TutorDestination.HAND))
HARMONIZE = make("Harmonize", mv=4, pips={"G": 2}, draw=Draw(3, engine=False))
RHYSTIC = make("Rhystic Study", mv=3, pips={"B": 1}, draw=Draw(1, engine=True))
PIECE_A = make("Piece A", mv=3, pips={"G": 1}, kinds=CREATURE, combo=True)
PIECE_B = make("Piece B", mv=2, pips={"B": 1}, kinds=CREATURE, combo=True)
OVERRUN = make("Overrun", mv=5, pips={"G": 1}, finisher=True)
HOOF = make("Craterhoof Behemoth", mv=8, pips={"G": 1}, kinds=CREATURE, finisher=True)
BOSS = make("Boss", mv=4, pips={"G": 1, "B": 1}, kinds=CREATURE)
FILLER = make("Filler", mv=3, pips={"G": 1})
SOLO = make("Solo Spell", mv=3, pips={"B": 1}, kinds=CREATURE)
PLAIN = [
    make(f"Spell {i}", mv=1 + i % 7, pips={"GB"[i % 2]: 1}, copies=5 if i < 10 else 4) for i in range(11)
]
PIECE_C = make("Piece C", mv=2, pips={"B": 1}, kinds=CREATURE, combo=True)
COMBO = GoldfishCombo("1-2", (PIECE_A.name, PIECE_B.name), (), ())
# A combo the deck cannot assemble: its second piece is not among the cards.
COMBO_MISSING_C = GoldfishCombo("1-3", (PIECE_A.name, PIECE_C.name), (), ())
DARK_RITUAL = make(
    "Dark Ritual",
    mv=1,
    pips={"B": 1},
    kinds=frozenset({CardKind.INSTANT}),
    mana=ManaAbility(3, frozenset("B"), delayed=False, ritual=True),
)
GREEN_SUN = make(
    "Green Sun's Zenith",
    mv=1,
    pips={"G": 1},
    tutor=Tutor(frozenset({CardKind.CREATURE}), TutorDestination.BATTLEFIELD, x_limited=True),
)


def real_shaped() -> GoldfishDeck:
    """99 cards shaped like a commander deck: 34 lands, ramp, a tutor, draw, a combo, a finisher, spells."""
    cards = (
        dataclasses.replace(FOREST, copies=14),
        dataclasses.replace(SWAMP, copies=14),
        dataclasses.replace(GOLGARI, copies=4),
        dataclasses.replace(WILDS, copies=2),
        SOL_RING,
        SIGNET,
        LLANOWAR,
        CULTIVATE,
        TUTOR,
        HARMONIZE,
        RHYSTIC,
        PIECE_A,
        PIECE_B,
        OVERRUN,
        SOLO,
        *PLAIN,
    )
    assert sum(card.copies for card in cards) == 99
    return GoldfishDeck(
        cards=cards,
        commanders=(BOSS,),
        combos=(COMBO, COMBO_MISSING_C),
        left_out_combos=(),
        not_modelled=(),
        average_mana_value=3.0,
    )


type Card = GoldfishCard

DECK = real_shaped()
PLAIN_SPELL = PLAIN[3]


def base_of(bracket: int | None = 4, deck: GoldfishDeck = DECK) -> GoldfishReport:
    return play_deck(deck, bracket=bracket, seed=SEED, games=GAMES)


def judged(
    swaps: Sequence[Swap], *, bracket: int | None = 4, deck: GoldfishDeck = DECK, **options: bool
) -> GoldfishComparison:
    return compare_deck(deck, base_of(bracket, deck), swaps, seed=SEED, **options)


def swapped_slots(deck: GoldfishDeck, swaps: Sequence[Swap]) -> list[Card]:
    """The deck's slots with each swap's incoming card in the slots of the cut card, or in the last ones
    of a partial swap."""
    slots = list(deck_slots(deck))
    for swap in swaps:
        at = [i for i, card in enumerate(slots) if card.name == swap[0].name]
        for i in at[-swap.copies :] if isinstance(swap, PartialSwap) else at:
            slots[i] = swap[1]
    return slots


def played_whole(
    swaps: Sequence[Swap], *, bracket: int | None = 4, deck: GoldfishDeck = DECK
) -> GoldfishReport:
    """The deck with the swaps made, played from scratch on the same seed."""
    return simulate(
        swapped_slots(deck, swaps),
        deck.commanders,
        deck.combos,
        bracket=bracket,
        average_mana_value=swapped_average(deck_slots(deck), swaps, deck.average_mana_value),
        games=GAMES,
        seed=SEED,
        not_modelled=base_of(bracket, deck).not_modelled,
    )


# --- the same figures as playing the swapped deck whole -----------------------------------------------------

EQUAL_CASES = {
    "a ramp swap": [(PLAIN_SPELL, TALISMAN)],
    "a swap of two plain spells, played anyway": [(PLAIN_SPELL, make("Other Spell", mv=6, pips={"B": 2}))],
    "a tutor cut": [(TUTOR, make("Filler", mv=2, pips={"B": 1}))],
    "a tutor in": [
        (
            PLAIN_SPELL,
            make("Vampiric Tutor", mv=1, pips={"B": 1}, tutor=Tutor(frozenset(), TutorDestination.TOP)),
        )
    ],
    "a combo piece cut": [(PIECE_A, make("Filler", mv=3, pips={"G": 1}))],
    "a combo piece in for a combo that was missing it": [(PLAIN_SPELL, PIECE_C)],
    "a ritual in": [(PLAIN_SPELL, DARK_RITUAL)],
    "an X-limited tutor in": [(PLAIN_SPELL, GREEN_SUN)],
    "a finisher cut": [(OVERRUN, make("Filler", mv=5, pips={"G": 1}))],
    "a finisher in": [(PLAIN_SPELL, HOOF)],
    "a finisher for a finisher": [(OVERRUN, HOOF)],
    "a land for a land": [(WILDS, land("Command Tower", "GB"))],
    "a land cut for a spell": [(FOREST, make("Filler", mv=2, pips={"G": 1}))],
    "a land-ramp spell cut": [(CULTIVATE, make("Filler", mv=3, pips={"G": 1}))],
    "a draw engine cut": [(RHYSTIC, make("Filler", mv=3, pips={"B": 1}))],
    "a plan of two swaps": [(PLAIN_SPELL, TALISMAN), (SOLO, make("Other Spell", mv=6, pips={"B": 2}))],
    "a card already in the deck": [(PLAIN_SPELL, SOL_RING)],
}


@pytest.mark.parametrize("swaps", EQUAL_CASES.values(), ids=EQUAL_CASES.keys())
def test_a_swap_replayed_gives_the_report_of_the_swapped_deck_played_whole(swaps: list[Swap]) -> None:
    result = judged(swaps, skip_irrelevant=False)
    assert result.swapped == played_whole(swaps)
    assert result.base == base_of()


DECK_FOREST, DECK_SWAMP, DECK_GOLGARI = DECK.cards[0], DECK.cards[1], DECK.cards[2]
KODAMAS_REACH = dataclasses.replace(CULTIVATE, name="Kodama's Reach")
MIND_STONE = make(
    "Mind Stone", mv=2, kinds=ARTIFACT, mana=ManaAbility(1, frozenset("C"), delayed=False, ritual=False)
)
VAMPIRIC = make("Vampiric Tutor", mv=1, pips={"B": 1}, tutor=Tutor(frozenset(), TutorDestination.TOP))

PARTIAL_CASES = {
    "one forest for a ramp rock": [PartialSwap(DECK_FOREST, TALISMAN, 1)],
    "three swamps for land ramp": [PartialSwap(DECK_SWAMP, KODAMAS_REACH, 3)],
    "two of a plain spell for a ritual": [PartialSwap(PLAIN_SPELL, DARK_RITUAL, 2)],
    "two of a plain spell for a finisher": [PartialSwap(PLAIN_SPELL, HOOF, 2)],
    "one of a plain spell for a tutor": [PartialSwap(PLAIN_SPELL, VAMPIRIC, 1)],
    "one of a plain spell for a combo piece": [PartialSwap(PLAIN_SPELL, PIECE_C, 1)],
    "one tapped dual for an untapped one": [PartialSwap(DECK_GOLGARI, land("Command Tower", "GB"), 1)],
    "all but one of a plain spell for a rock": [PartialSwap(PLAIN_SPELL, TALISMAN, 4)],
    "a partial swap and a whole one": [PartialSwap(DECK_FOREST, TALISMAN, 2), (SOLO, MIND_STONE)],
    "a partial swap and a whole one that cuts a finisher": [
        PartialSwap(DECK_SWAMP, TALISMAN, 2),
        (OVERRUN, HOOF),
    ],
    "two partial swaps": [PartialSwap(DECK_FOREST, TALISMAN, 2), PartialSwap(DECK_SWAMP, MIND_STONE, 1)],
    "two partial swaps of plain spells": [
        PartialSwap(PLAIN_SPELL, DARK_RITUAL, 1),
        PartialSwap(PLAIN[4], HOOF, 2),
    ],
    "a partial swap for a card the deck holds": [PartialSwap(PLAIN_SPELL, SOL_RING, 2)],
}


def without_numbers(report: GoldfishReport) -> GoldfishReport:
    """The report with the cards each game reached left out: they are counted by number, and a card put in
    some slots of another is numbered after all the others, where a deck played whole numbers it by place."""
    outcomes = tuple(outcome._replace(reached=frozenset()) for outcome in report.outcomes)
    return dataclasses.replace(report, outcomes=outcomes)


@pytest.mark.parametrize("swaps", PARTIAL_CASES.values(), ids=PARTIAL_CASES.keys())
def test_a_swap_of_some_copies_gives_the_report_of_the_deck_with_those_copies_swapped_played_whole(
    swaps: list[Swap],
) -> None:
    result = judged(swaps, skip_irrelevant=False)
    assert without_numbers(result.swapped) == without_numbers(played_whole(swaps))
    assert result.base == base_of()


def test_the_same_holds_in_bracket_three_where_the_keep_rule_asks_for_lands_and_draw() -> None:
    swaps = [(HARMONIZE, make("Filler", mv=4, pips={"G": 2})), (PLAIN_SPELL, TALISMAN)]
    assert judged(swaps, bracket=3).swapped == played_whole(swaps, bracket=3)


# --- only the affected games are played again ---------------------------------------------------------------


def test_a_swap_replays_only_the_games_that_can_change_and_every_changed_game_is_among_them() -> None:
    swaps = [(SOLO, TALISMAN)]
    result = judged(swaps)
    whole = played_whole(swaps)
    changed = {i for i, (a, b) in enumerate(zip(result.base.outcomes, whole.outcomes, strict=True)) if a != b}
    assert changed
    assert changed <= set(result.replayed)
    assert len(result.replayed) < GAMES // 2
    assert result.swapped == whole


def expected_replays(
    result: GoldfishComparison, cut: Card, *, lands: bool, wanted: bool, copies: int | None = None
) -> list[int]:
    """The games that drew the cut card's slots (the last `copies` of them, if given) in some shuffle, or
    whose searches looked at lands (when the swap has one) or at what a tutor wants (when it has one)."""
    slots = deck_slots(DECK)
    named = [i for i, card in enumerate(slots) if card.name == cut.name]
    cut_slots = set(named if copies is None else named[-copies:])
    found: list[int] = []
    for index, outcome in enumerate(result.base.outcomes):
        reached = (lands and outcome.land_search) or (wanted and outcome.tutor_look)
        rng = goldfish.game_rng(SEED, index)
        for depth in outcome.shuffle_depths:
            order = list(range(len(slots)))
            # A shuffle takes 8 bytes a card from the stream: a Fisher-Yates from the front, step i swapping
            # slot i with one of the slots from i on, chosen by the i-th number modulo the slots left.
            numbers = memoryview(rng.randbytes(8 * len(order))).cast("Q")
            for i in range(len(order)):
                j = i + numbers[i] % (len(order) - i)
                order[i], order[j] = order[j], order[i]
            reached = reached or any(slot in cut_slots for slot in order[:depth])
        if reached:
            found.append(index)
    return found


def test_the_games_replayed_are_those_that_drew_the_cut_slot_when_no_search_looks_at_the_cards() -> None:
    result = judged([(SOLO, TALISMAN)])
    assert list(result.replayed) == expected_replays(result, SOLO, lands=False, wanted=False)
    # Games that searched are not replayed for that: the cards a search takes out are lands and combo cards.
    searching = [i for i, o in enumerate(result.base.outcomes) if o.searched]
    assert set(searching) - set(result.replayed)


def test_a_swap_of_some_copies_replays_the_games_that_drew_one_of_the_slots_it_gives_up() -> None:
    swaps = [PartialSwap(PLAIN_SPELL, TALISMAN, 2)]
    result = judged(swaps)
    expected = expected_replays(result, PLAIN_SPELL, lands=False, wanted=False, copies=2)
    assert list(result.replayed) == expected
    # The three copies it keeps are drawn in games that need no replay, which a swap of all five would play.
    assert expected
    assert set(expected) < set(expected_replays(result, PLAIN_SPELL, lands=False, wanted=False))
    whole = without_numbers(played_whole(swaps))
    before = without_numbers(result.base)
    changed = {i for i, (a, b) in enumerate(zip(before.outcomes, whole.outcomes, strict=True)) if a != b}
    assert changed
    assert changed <= set(result.replayed)


def test_a_swap_of_one_land_replays_the_games_that_drew_its_slot_and_those_that_searched_for_a_land() -> None:
    result = judged([PartialSwap(DECK_FOREST, TALISMAN, 1)])
    assert list(result.replayed) == expected_replays(result, DECK_FOREST, lands=True, wanted=False, copies=1)
    assert {i for i, o in enumerate(result.base.outcomes) if o.land_search} <= set(result.replayed)
    assert len(result.replayed) < GAMES


def test_a_swap_of_some_copies_of_a_combo_piece_replays_the_games_whose_tutor_looked() -> None:
    pieces = dataclasses.replace(PIECE_A, copies=2)
    forests = dataclasses.replace(DECK_FOREST, copies=13)
    deck = dataclasses.replace(DECK, cards=(forests, *DECK.cards[1:11], pieces, *DECK.cards[12:]))
    assert sum(card.copies for card in deck.cards) == 99
    swaps = [PartialSwap(pieces, FILLER, 1)]
    result = judged(swaps, deck=deck)
    assert {i for i, o in enumerate(result.base.outcomes) if o.tutor_look} <= set(result.replayed)
    assert without_numbers(result.swapped) == without_numbers(played_whole(swaps, deck=deck))
    assert any(o.combo_turns[0] for o in result.base.outcomes)


def test_a_swap_of_all_the_copies_given_as_some_is_the_swap_of_the_card() -> None:
    some = judged([PartialSwap(PLAIN_SPELL, TALISMAN, PLAIN_SPELL.copies)])
    whole = judged([(PLAIN_SPELL, TALISMAN)])
    assert some == whole


def test_a_swap_of_some_copies_of_plain_spells_that_play_alike_changes_nothing_and_plays_nothing() -> None:
    swaps = [PartialSwap(PLAIN_SPELL, make("Other Spell", mv=4, pips={"B": 1}), 2)]
    result = judged(swaps)
    assert result.replayed == ()
    assert result.swapped == result.base


def test_a_swap_of_some_copies_that_moves_the_average_across_the_land_count_line_plays_every_game() -> None:
    deck = dataclasses.replace(DECK, average_mana_value=3.49)
    swaps = [PartialSwap(PLAIN_SPELL, make("Big Spell", mv=12, pips={"B": 1}), 1)]
    base = play_deck(deck, bracket=3, seed=SEED, games=GAMES)
    result = compare_deck(deck, base, swaps, seed=SEED)
    assert len(result.replayed) == GAMES
    assert without_numbers(result.swapped) == without_numbers(played_whole(swaps, bracket=3, deck=deck))


def test_a_card_cut_in_part_stays_among_the_cards_not_modelled_and_one_cut_whole_does_not() -> None:
    deck = dataclasses.replace(DECK, not_modelled=(PLAIN_SPELL.name,))
    some = judged([PartialSwap(PLAIN_SPELL, TALISMAN, 2)], deck=deck)
    whole = judged([(PLAIN_SPELL, TALISMAN)], deck=deck)
    assert some.swapped.not_modelled == (PLAIN_SPELL.name,)
    assert whole.swapped.not_modelled == ()


def test_a_swap_of_a_land_replays_the_games_whose_land_search_looked_at_the_lands() -> None:
    swaps = [(WILDS, land("Command Tower", "GB"))]
    result = judged(swaps)
    assert list(result.replayed) == expected_replays(result, WILDS, lands=True, wanted=False)
    assert any(o.land_search for o in result.base.outcomes)
    assert {i for i, o in enumerate(result.base.outcomes) if o.land_search} <= set(result.replayed)


@pytest.mark.parametrize(
    ("swap", "cut"), [((PIECE_A, FILLER), PIECE_A), ((OVERRUN, HOOF), OVERRUN), ((SOLO, HOOF), SOLO)]
)
def test_a_swap_of_a_combo_piece_or_finisher_replays_the_games_whose_tutor_looked(
    swap: Swap, cut: Card
) -> None:
    result = judged([swap])
    assert list(result.replayed) == expected_replays(result, cut, lands=False, wanted=True)
    assert any(o.tutor_look for o in result.base.outcomes)
    assert {i for i, o in enumerate(result.base.outcomes) if o.tutor_look} <= set(result.replayed)


def test_a_library_that_loses_cards_to_searches_reaches_that_much_further() -> None:
    outcomes = judged([]).base.outcomes
    searched = [o for o in outcomes if o.searched]
    assert searched
    # The last shuffle's depth is the cards drawn and the cards taken out. Bracket 4 plays six turns: seven
    # cards and one a turn make 13, which no game is short of, and each card a search takes out adds one.
    assert all(o.shuffle_depths[-1] >= 13 for o in outcomes)
    assert all(o.shuffle_depths[-1] > 13 for o in searched)
    assert any(o.shuffle_depths[-1] == 13 for o in outcomes if not o.searched)


def test_a_game_whose_hand_holds_the_cut_card_is_replayed() -> None:
    # Dealt whole, the one deck's seven cards are all in the opening hand, so every game is replayed.
    slots = [*repeat(FOREST, 6), VANILLA]
    base = run(slots, games=20)
    result = compare(
        base, slots, [(VANILLA, SOL_RING)], (), (), average_mana_value=2.0, seed=7, skip_irrelevant=False
    )
    assert len(result.replayed) == 20


def test_a_swap_that_changes_the_keep_rule_replays_every_game() -> None:
    # The deck's only combo piece and finisher go, so the top brackets stop asking for a way to win.
    swaps = [(PIECE_A, SOLO), (OVERRUN, make("Filler", mv=5, pips={"G": 1}))]
    result = judged(swaps)
    assert len(result.replayed) == GAMES
    assert result.swapped == played_whole(swaps)


def test_a_card_already_in_the_deck_replays_every_game_as_the_numbering_of_cards_changes() -> None:
    assert len(judged([(PLAIN_SPELL, SOL_RING)]).replayed) == GAMES


# --- plain spells -------------------------------------------------------------------------------------------


def test_a_swap_of_two_plain_spells_that_cost_the_same_changes_nothing_and_plays_nothing() -> None:
    swaps = [(PLAIN_SPELL, make("Other Spell", mv=4, pips={"B": 1}))]
    result = judged(swaps)
    assert result.replayed == ()
    assert result.moved == ()
    assert not result.any_moved
    assert result.swapped == result.base
    assert result.swapped == played_whole(swaps)


PLAIN_SWAPS_THAT_PLAY = {
    "a dearer spell": make("Dearer Spell", mv=6, pips={"B": 2}),
    "a spell of another colour": make("Red Spell", mv=4, pips={"R": 1}),
    "a creature for a non-creature": make("Creature Spell", mv=4, pips={"B": 1}, kinds=CREATURE),
}


@pytest.mark.parametrize("incoming", PLAIN_SWAPS_THAT_PLAY.values(), ids=PLAIN_SWAPS_THAT_PLAY.keys())
def test_a_plain_swap_that_changes_the_cost_colours_or_creatures_is_played_like_any_other(
    incoming: Card,
) -> None:
    swaps = [(PLAIN_SPELL, incoming)]
    result = judged(swaps)
    assert result.replayed
    assert result.swapped == played_whole(swaps)


def test_a_plain_swap_that_moves_the_average_across_the_land_count_line_is_played() -> None:
    deck = dataclasses.replace(DECK, average_mana_value=3.49)
    swaps = [(PLAIN_SPELL, make("Big Spell", mv=12, pips={"B": 1}))]
    base = play_deck(deck, bracket=3, seed=SEED, games=GAMES)
    result = compare_deck(deck, base, swaps, seed=SEED)
    # The average passes 3.5, so bracket 3 keeps hands of three lands where it kept two: every game is played.
    assert len(result.replayed) == GAMES


def test_a_plain_spell_for_a_ramp_card_is_not_short_circuited() -> None:
    assert judged([(PLAIN_SPELL, TALISMAN)]).replayed


def test_a_plain_spell_that_matches_the_decks_tags_is_not_irrelevant() -> None:
    synergy = make("Synergy Spell", mv=3, pips={"B": 1}, synergy=True)
    assert judged([(PLAIN_SPELL, synergy)]).replayed


def test_a_spell_the_reading_could_not_follow_is_not_irrelevant() -> None:
    unmodelled = dataclasses.replace(make("Odd Spell", mv=3, pips={"B": 1}), modelled=False)
    assert judged([(PLAIN_SPELL, unmodelled)]).replayed


# --- the figures ------------------------------------------------------------------------------------------


def test_a_ramp_swap_in_a_dealt_deck_moves_the_commander_and_the_mana() -> None:
    slots = [*repeat(FOREST, 5), VANILLA, make("Filler", mv=5, pips={"G": 1})]
    boss = make("Boss", mv=3, pips={"G": 1}, kinds=CREATURE)
    base = run(slots, commanders=(boss,), games=20)
    result = compare(base, slots, [(VANILLA, SOL_RING)], (boss,), (), average_mana_value=2.0, seed=7)
    kinds = {figure.kind for figure in result.moved}
    assert {FigureKind.COMMANDER_CAST_BY, FigureKind.MANA_MEDIAN} <= kinds
    cast = next(f for f in result.moved if f.kind is FigureKind.COMMANDER_CAST_BY and f.turn == 2)
    assert (cast.subject, cast.base, cast.swapped) == ("Boss", 0.0, 1.0)
    median = next(f for f in result.moved if f.kind is FigureKind.COMMANDER_MEDIAN)
    assert (median.base, median.swapped) == (3, 2)
    assert result.any_moved


def test_a_swap_that_plays_the_same_moves_nothing_and_reports_every_figure_unmoved() -> None:
    slots = [*repeat(FOREST, 5), VANILLA, make("Filler", mv=5, pips={"G": 1})]
    same = make("Same As Bears", mv=5, kinds=CREATURE)
    base = run(slots, games=20)
    result = compare(
        base, slots, [(VANILLA, same)], (), (), average_mana_value=2.0, seed=7, skip_irrelevant=False
    )
    assert len(result.replayed) == 20
    assert result.moved == ()
    assert not result.any_moved
    assert result.figures
    assert not any(figure.moved for figure in result.figures)


def test_a_figure_of_a_card_only_one_side_has_is_zero_on_the_other() -> None:
    result = judged([(PLAIN_SPELL, HOOF)])
    added = [
        f for f in result.figures if f.kind is FigureKind.FINISHER_CASTABLE_BY and f.subject == HOOF.name
    ]
    assert added
    assert all(f.base == 0.0 for f in added)
    cut = judged([(OVERRUN, make("Filler", mv=5, pips={"G": 1}))])
    gone = [f for f in cut.figures if f.kind is FigureKind.FINISHER_CASTABLE_BY and f.subject == "Overrun"]
    assert gone
    assert all(f.swapped == 0.0 for f in gone)


def test_cutting_the_tutor_moves_the_share_of_combos_landed_by_a_fetch() -> None:
    result = judged([(TUTOR, make("Filler", mv=2, pips={"B": 1}))])
    fetched = next(f for f in result.figures if f.kind is FigureKind.COMBO_FETCHED)
    assert fetched.base
    assert fetched.swapped == 0.0
    assert fetched.moved


def test_a_share_moves_only_beyond_twice_its_paired_standard_error() -> None:
    # 3 of 100 games differ by one: 0.03 against 2 x 0.01714; 4 differ: 0.04 against 2 x 0.01969.
    assert not is_real_shift(100, 3, 3)
    assert is_real_shift(100, 4, 4)
    assert is_real_shift(100, -4, 4)
    assert not is_real_shift(100, 0, 0)
    # Every game moves the same way: no spread at all.
    assert is_real_shift(20, 20, 20)
    # One game of one: nothing to estimate a spread from, so any difference is a move.
    assert is_real_shift(1, 1, 1)


# --- bad swaps and the average mana value ----------------------------------------------------------------


def test_a_cut_the_deck_does_not_hold_is_refused() -> None:
    with pytest.raises(ValueError, match="Not In Deck"):
        judged([(make("Not In Deck", mv=1), TALISMAN)])


def test_a_card_cut_twice_is_refused() -> None:
    with pytest.raises(ValueError, match="twice"):
        judged([(PLAIN_SPELL, TALISMAN), (PLAIN_SPELL, SIGNET)])


@pytest.mark.parametrize("copies", [0, -1, 6])
def test_a_swap_of_no_copies_or_more_than_the_deck_holds_is_refused(copies: int) -> None:
    with pytest.raises(ValueError, match=rf"{copies} of Spell 3 are cut and the deck holds 5"):
        judged([PartialSwap(PLAIN_SPELL, TALISMAN, copies)])


def test_a_card_cut_in_part_and_again_is_refused() -> None:
    with pytest.raises(ValueError, match="twice"):
        judged([PartialSwap(PLAIN_SPELL, TALISMAN, 1), PartialSwap(PLAIN_SPELL, SIGNET, 1)])


def test_no_swaps_change_nothing() -> None:
    result = judged([])
    assert result.replayed == ()
    assert result.swapped == result.base
    assert not result.any_moved


def test_the_average_mana_value_follows_the_swap() -> None:
    cheap = make("Cheap", mv=2)
    dear = make("Dear", mv=5)
    slots = [FOREST, cheap, cheap, cheap]
    assert swapped_average(slots, [(cheap, dear)], 2.0) == 5.0
    # A land in a spell's place leaves one fewer spell, all the cheaper.
    assert swapped_average([FOREST, cheap, dear], [(dear, FOREST)], 3.5) == 2.0
    # A spell in a land's place counts as a new one.
    assert swapped_average([FOREST, cheap, cheap], [(FOREST, dear)], 2.0) == 3.0
    assert swapped_average([FOREST], [(FOREST, FOREST)], 0.0) == 0.0


def test_the_average_mana_value_counts_only_the_copies_swapped() -> None:
    cheap = make("Cheap", mv=2)
    dear = make("Dear", mv=5)
    slots = [FOREST, cheap, cheap, cheap]
    # One of three 2s becomes a 5: 9 over 3 spells.
    assert swapped_average(slots, [PartialSwap(cheap, dear, 1)], 2.0) == 3.0
    # Two of them go for lands, leaving one spell.
    assert swapped_average(slots, [PartialSwap(cheap, FOREST, 2)], 2.0) == 2.0
