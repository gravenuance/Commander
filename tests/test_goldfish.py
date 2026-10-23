"""The goldfish game (ADR 0054): hand-built decks whose expected figures are worked out by hand.

A deck of seven cards is dealt whole into the opening hand, whatever the shuffle, so its game is the same each
time; larger decks are checked against exact hypergeometric values with a tolerance of about four standard
errors, which a fixed seed keeps from ever flaking.
"""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from math import comb

import pytest

from commander import goldfish
from commander.goldfish import (
    BRACKET_TURN,
    GameOutcome,
    GoldfishReport,
    deck_slots,
    london_bottom,
    replay_game,
    simulate,
    turns_shown,
)
from commander.goldfish_cards import (
    CHANGELING,
    ActivatedTutor,
    CardKind,
    Draw,
    EntryCost,
    GoldfishCard,
    GoldfishCombo,
    GoldfishDeck,
    LandDestination,
    LandFace,
    LandRamp,
    ManaAbility,
    ManaNeed,
    Tutor,
    TutorDestination,
)

SPELL = frozenset({CardKind.SORCERY})
CREATURE = frozenset({CardKind.CREATURE})
ARTIFACT = frozenset({CardKind.ARTIFACT})
ANY_COLOUR = frozenset("WUBRG")


def make(
    name: str,
    *,
    mv: int = 0,
    pips: Mapping[str, int] | None = None,
    kinds: frozenset[CardKind] = SPELL,
    land: LandFace | None = None,
    mana: ManaAbility | None = None,
    tutor: Tutor | None = None,
    draw: Draw | None = None,
    combo: bool = False,
    finisher: bool = False,
    synergy: bool = False,
    x_cost: bool = False,
    copies: int = 1,
    extra_land_drops: int = 0,
    needs_sacrifice: bool = False,
    entry_cost: EntryCost | None = None,
    land_ramp: LandRamp | None = None,
    land_types: frozenset[str] = frozenset(),
) -> GoldfishCard:
    return GoldfishCard(
        name=name,
        oracle_id=f"id-{name}",
        copies=copies,
        commander=False,
        mana_value=mv,
        pips=pips or {},
        x_cost=x_cost,
        kinds=kinds,
        land=land,
        mana=mana,
        land_ramp=land_ramp,
        tutor=tutor,
        draw=draw,
        combo=combo,
        finisher=finisher,
        synergy=synergy,
        modelled=True,
        extra_land_drops=extra_land_drops,
        needs_sacrifice=needs_sacrifice,
        entry_cost=entry_cost,
        land_types=land_types,
    )


def land(name: str, colours: str = "G", *, tapped: bool = False, amount: int = 1) -> GoldfishCard:
    return make(name, kinds=frozenset({CardKind.LAND}), land=LandFace(frozenset(colours), tapped, amount))


FOREST = land("Forest", "G")
MOUNTAIN = land("Mountain", "R")
SWAMP = land("Swamp", "B")
VANILLA = make("Grizzly Bears", mv=5, kinds=CREATURE)
SOL_RING = make(
    "Sol Ring", mv=1, kinds=ARTIFACT, mana=ManaAbility(2, frozenset("C"), delayed=False, ritual=False)
)


def repeat(card: GoldfishCard, count: int) -> list[GoldfishCard]:
    return [card] * count


def run(
    slots: Sequence[GoldfishCard],
    *,
    commanders: Sequence[GoldfishCard] = (),
    combos: Sequence[GoldfishCombo] = (),
    bracket: int | None = 5,
    games: int = 20,
    seed: int = 7,
    average: float = 2.0,
) -> GoldfishReport:
    return simulate(
        slots, commanders, combos, bracket=bracket, average_mana_value=average, games=games, seed=seed
    )


def only_outcome(report: GoldfishReport) -> GameOutcome:
    """The one outcome every game of a deck dealt whole has."""
    first = report.outcomes[0]
    assert all(o == first for o in report.outcomes)
    return first


def combo_of(*pieces: GoldfishCard) -> GoldfishCombo:
    return GoldfishCombo("1-2", tuple(p.name for p in pieces), (), ())


# --- turns, slots and determinism ------------------------------------------------------------------------


def test_the_turns_shown_follow_the_bracket() -> None:
    assert turns_shown(5) == (1, 2, 3, 4, 5)
    assert turns_shown(4) == (2, 3, 4, 5, 6)
    assert turns_shown(3) == (4, 5, 6, 7, 8)
    assert turns_shown(2) == (6, 7, 8, 9, 10)
    assert turns_shown(1) == (7, 8, 9, 10, 11)
    assert turns_shown(None) == turns_shown(3)


def test_a_bracket_outside_one_to_five_is_refused() -> None:
    with pytest.raises(ValueError, match="bracket"):
        turns_shown(6)


def test_the_slots_hold_each_card_once_per_copy_in_the_decks_order() -> None:
    a, b = make("A", copies=2), make("B")
    deck = GoldfishDeck((a, b), (make("Boss"),), (), (), (), 2.0)
    assert deck_slots(deck) == (a, a, b)


def test_the_same_seed_gives_the_same_games_and_another_seed_other_ones() -> None:
    slots = [*repeat(FOREST, 38), *repeat(SOL_RING, 2), *repeat(VANILLA, 59)]
    first = run(slots, games=60, seed=3)
    assert run(slots, games=60, seed=3).outcomes == first.outcomes
    assert run(slots, games=60, seed=4).outcomes != first.outcomes


def test_a_game_replayed_alone_matches_its_outcome_in_the_full_run() -> None:
    tutor = make(
        "Tutor",
        mv=1,
        tutor=Tutor(frozenset(), TutorDestination.HAND),
        mana=None,
    )
    piece_a = make("A", mv=2, kinds=CREATURE, combo=True)
    piece_b = make("B", mv=2, kinds=CREATURE, combo=True)
    boss = make("Boss", mv=3, kinds=CREATURE)
    slots = [*repeat(FOREST, 36), *repeat(VANILLA, 59), tutor, piece_a, piece_b, SOL_RING]
    combos = (combo_of(piece_a, piece_b),)
    report = run(slots, commanders=(boss,), combos=combos, bracket=4, games=40, seed=11)
    for index in (0, 7, 23, 39):
        alone = replay_game(slots, (boss,), combos, bracket=4, average_mana_value=2.0, seed=11, index=index)
        assert alone == report.outcomes[index]


def test_a_card_in_a_slot_takes_the_same_shuffles_as_the_card_it_replaces() -> None:
    # Swapping a card for another in the same slot leaves every game that never reaches the slot unchanged.
    base = [*repeat(FOREST, 40), *repeat(VANILLA, 59)]
    swapped = list(base)
    other = make("Other", mv=5, kinds=CREATURE)
    swapped[98] = other
    first = run(base, games=80, seed=5, bracket=3)
    second = run(swapped, games=80, seed=5, bracket=3)
    # Only the last slot differs, which the first 7 + 5 cards drawn can hardly reach.
    unaffected = [
        (a, b)
        for a, b in zip(first.outcomes, second.outcomes, strict=True)
        if a.shuffle_depths[-1] < 40 and not a.searched
    ]
    assert len(unaffected) > 60
    # The cards reached are numbered apart (the other card has a number of its own), so they are left out.
    assert all(a._replace(reached=frozenset()) == b._replace(reached=frozenset()) for a, b in unaffected)


# --- hands and the keep rule -----------------------------------------------------------------------------


def test_a_deck_of_lands_is_never_a_keepable_seven_below_bracket_five_and_ends_on_five() -> None:
    report = run(repeat(FOREST, 99), bracket=3)
    assert report.kept_on_five_or_fewer == 1.0
    assert report.kept_on_seven == report.kept_on_six == 0.0
    assert report.keep_bracket == 3
    # Five lands in hand and a land drawn each turn: the mana is the turn number.
    assert report.mana_median == report.mana_worst_tenth == (4, 5, 6, 7, 8)


def test_in_bracket_five_the_same_deck_of_lands_keeps_its_first_seven() -> None:
    report = run(repeat(FOREST, 99), bracket=5)
    assert report.kept_on_seven == 1.0
    assert report.mana_median == report.mana_worst_tenth == (1, 2, 3, 4, 5)


def test_the_keep_rule_of_bracket_five_wants_a_way_to_win_and_bracket_three_wants_two_lands() -> None:
    piece = make("Piece", mv=2, kinds=CREATURE, combo=True)
    # One land, one fast mana source and a combo piece: bracket 5 keeps it, bracket 3 does not (two lands).
    slots = [FOREST, SOL_RING, piece, *repeat(VANILLA, 4)]
    combos = (combo_of(piece, make("Missing")),)
    high = run(slots, combos=combos, bracket=5)
    low = run(slots, combos=combos, bracket=3)
    assert high.kept_on_seven == 1.0
    assert low.kept_on_five_or_fewer == 1.0


def test_bracket_four_wants_one_more_mana_source_than_bracket_five() -> None:
    piece = make("Piece", mv=2, kinds=CREATURE, combo=True)
    slots = [FOREST, FOREST, piece, *repeat(VANILLA, 4)]
    combos = (combo_of(piece, make("Missing")),)
    assert run(slots, combos=combos, bracket=5).kept_on_seven == 1.0
    assert run(slots, combos=combos, bracket=4).kept_on_five_or_fewer == 1.0


def test_a_finisher_is_a_way_to_win_for_the_keep_rule_of_the_top_brackets() -> None:
    finisher = make("Overrun", mv=6, finisher=True)
    slots = [*repeat(FOREST, 4), *repeat(VANILLA, 4), finisher]
    # Nine cards deal seven, two left out. Bracket 5 sends a hand back when the finisher is left out (8 of 36
    # pairs); bracket 4, which also wants three sources, when two Forests are (6 more pairs). The free
    # mulligan squares the failure: kept on seven in 1 - (8/36)^2 and 1 - (14/36)^2 of games (4 errors: 0.02).
    for bracket, failing in ((5, 8 / 36), (4, 14 / 36)):
        report = run(slots, bracket=bracket, games=2000, seed=6)
        assert report.kept_on_seven == pytest.approx(1 - failing**2, abs=0.02)


def test_a_deck_with_no_combo_or_finisher_is_kept_by_the_bracket_three_clause_even_with_tutors() -> None:
    tutor = make("Tutor", mv=1, tutor=Tutor(frozenset(), TutorDestination.HAND))
    sweet = make("Sweet", mv=2, kinds=CREATURE, synergy=True)
    slots = [*repeat(FOREST, 4), tutor, *repeat(VANILLA, 2), sweet, sweet]
    # The tutor is no way to win here, so a hand needs a dominant-tag card: both left out is 1 in 36 hands.
    report = run(slots, bracket=5, games=2000, seed=6)
    assert report.kept_on_seven > 0.99


def test_a_seven_without_a_way_to_win_is_not_kept_in_the_top_brackets() -> None:
    piece = make("Piece", mv=2, kinds=CREATURE, combo=True)
    slots = [*repeat(FOREST, 3), *repeat(VANILLA, 4)]
    # The deck holds no piece or tutor, so the rule is waived; with one in the library it is not.
    assert run(slots, bracket=5).kept_on_seven == 1.0
    held_back = [*repeat(FOREST, 3), *repeat(VANILLA, 3), piece, *repeat(VANILLA, 2)]
    # A nine-card deck deals seven: some hands miss the piece, so some are sent back.
    report = run(held_back, combos=(combo_of(piece, VANILLA),), bracket=5, games=400)
    assert 0.0 < report.kept_on_seven < 1.0


def test_bracket_three_wants_two_to_four_lands_and_bracket_two_three_to_four() -> None:
    cards = [FOREST, FOREST, make("Sweet", mv=2, kinds=CREATURE, synergy=True), *repeat(VANILLA, 4)]
    assert run(cards, bracket=3).kept_on_seven == 1.0
    assert run(cards, bracket=2).kept_on_five_or_fewer == 1.0
    assert run(cards, bracket=None).kept_on_seven == 1.0


def test_bracket_three_wants_three_lands_when_the_deck_is_expensive() -> None:
    cards = [FOREST, FOREST, make("Sweet", mv=2, kinds=CREATURE, synergy=True), *repeat(VANILLA, 4)]
    assert run(cards, bracket=3, average=3.5).kept_on_seven == 1.0
    assert run(cards, bracket=3, average=3.6).kept_on_five_or_fewer == 1.0


def test_a_hand_without_a_synergy_or_draw_card_is_sent_back_when_the_deck_has_some() -> None:
    sweet = make("Sweet", mv=2, kinds=CREATURE, synergy=True)
    # Eight cards deal seven: one of them is left out, and the keep needs the one synergy card.
    deck = [FOREST, FOREST, FOREST, sweet, *repeat(VANILLA, 4)]
    report = run(deck, bracket=3, games=400)
    # The rule fails only when the card left out is the synergy one, in 1 of 8 hands.
    assert 0.9 < report.kept_on_seven < 1.0
    draw = make("Draw", mv=2, kinds=CREATURE, draw=Draw(1, engine=False))
    drawing = [FOREST, FOREST, FOREST, draw, *repeat(VANILLA, 4)]
    assert run(drawing, bracket=3, games=400).kept_on_seven < 1.0


def test_the_london_bottom_puts_the_extra_lands_before_the_spells() -> None:
    # Six lands and the ring never pass bracket 3; the two cards put back are lands, so the ring stays.
    report = run([*repeat(FOREST, 6), SOL_RING], bracket=3)
    # Four lands in hand and two drawn, then the ring's two on top of the lands played.
    assert only_outcome(report).mana == (3, 4, 5, 6, 7, 8, 8, 8)
    assert report.kept_on_five_or_fewer == 1.0


def names(cards: Sequence[GoldfishCard]) -> list[str]:
    return [card.name for card in cards]


def creature(name: str, mv: int, *, combo: bool = False, finisher: bool = False) -> GoldfishCard:
    return make(name, mv=mv, kinds=CREATURE, combo=combo, finisher=finisher)


def test_the_london_bottom_puts_the_dearest_spell_first() -> None:
    hand = [FOREST, FOREST, creature("Two", 2), creature("Five", 5), creature("Three", 3), creature("One", 1)]
    assert names(london_bottom(hand, 2)) == ["Five", "Three"]
    assert names(london_bottom(hand, 1)) == ["Five"]
    assert london_bottom(hand, 0) == []


def test_the_london_bottom_puts_lands_past_four_before_any_spell() -> None:
    hand = [*repeat(FOREST, 6), creature("Nine", 9)]
    assert names(london_bottom(hand, 2)) == ["Forest", "Forest"]
    # With five lands one goes first; then four are kept and the dearest spell follows.
    hand = [*repeat(FOREST, 5), creature("Nine", 9), creature("Two", 2)]
    assert names(london_bottom(hand, 2)) == ["Forest", "Nine"]


def test_the_london_bottom_keeps_combo_pieces_tutors_and_finishers_while_other_spells_remain() -> None:
    piece = creature("Piece", 6, combo=True)
    finisher = creature("Finisher", 7, finisher=True)
    tutor = make("Tutor", mv=5, tutor=Tutor(frozenset(), TutorDestination.HAND))
    hand = [FOREST, FOREST, piece, finisher, tutor, creature("Three", 3), creature("Two", 2)]
    # The cheaper plain spells go before the dearer cards that win.
    assert names(london_bottom(hand, 2)) == ["Three", "Two"]


def test_the_london_bottom_puts_the_dearest_of_the_winning_cards_when_only_they_remain() -> None:
    piece = creature("Piece", 6, combo=True)
    finisher = creature("Finisher", 7, finisher=True)
    tutor = make("Tutor", mv=5, tutor=Tutor(frozenset(), TutorDestination.HAND))
    assert names(london_bottom([FOREST, FOREST, piece, finisher, tutor], 2)) == ["Finisher", "Piece"]


def test_the_london_bottom_puts_a_tapped_land_before_an_untapped_one_when_no_spell_is_left() -> None:
    tapped = land("Tapped", "G", tapped=True)
    assert names(london_bottom([FOREST, tapped, FOREST], 1)) == ["Tapped"]
    dual = land("Dual", "GU")
    assert names(london_bottom([dual, FOREST], 1)) == ["Forest"]


def test_a_deck_with_no_cards_plays_out_without_mana() -> None:
    report = run([], bracket=3, games=3)
    assert report.mana_median == (0, 0, 0, 0, 0)
    assert report.kept_on_five_or_fewer == 1.0


def test_the_share_kept_on_seven_six_and_five_follow_the_hypergeometric_odds() -> None:
    # Bracket 5 with no way to win to ask for keeps a seven with two lands or more.
    slots = [*repeat(FOREST, 40), *repeat(VANILLA, 59)]
    fail = (comb(59, 7) + 40 * comb(59, 6)) / comb(99, 7)
    report = run(slots, bracket=5, games=4000, seed=1)
    # The free mulligan gives a second seven; a six is judged on the lands of the seven it was cut from.
    expected = {
        "seven": 1 - fail**2,
        "six": fail**2 * (1 - fail),
        "five": fail**3,
    }
    # Four standard errors of a share near one half.
    assert report.kept_on_seven == pytest.approx(expected["seven"], abs=0.032)
    assert report.kept_on_six == pytest.approx(expected["six"], abs=0.032)
    assert report.kept_on_five_or_fewer == pytest.approx(expected["five"], abs=0.032)


# --- mana ------------------------------------------------------------------------------------------------


def test_sol_ring_cast_on_turn_one_pays_for_itself_twice_over() -> None:
    slots = [FOREST, FOREST, SOL_RING, *repeat(VANILLA, 4)]
    report = run(slots)
    # Turn 1: the land and the ring's two; turn 2: the second land as well; nothing more to draw.
    assert only_outcome(report).mana == (3, 4, 4, 4, 4)


def test_the_mana_median_and_worst_tenth_come_from_the_games_in_order() -> None:
    # Eleven cards: ten lands and the ring, which is among the 8 seen by turn 1 in 8 games of 11, by turn 2
    # in 9 of 11.
    slots = [*repeat(FOREST, 10), SOL_RING]
    report = run(slots, games=3000, seed=9)
    assert report.mana_median[:2] == (3, 4)
    # Turn 1 has 1 mana in 3 of 11 games and turn 2 has 2 in 2 of 11: both above a tenth.
    assert report.mana_worst_tenth[:2] == (1, 2)


def test_extra_land_drops_raise_the_lands_played_each_turn() -> None:
    exploration = make("Exploration", mv=1, kinds=frozenset({CardKind.ENCHANTMENT}), extra_land_drops=1)
    slots = [*repeat(FOREST, 5), exploration, VANILLA]
    assert only_outcome(run(slots)).mana == (2, 4, 5, 5, 5)


def test_a_mana_aura_is_not_cast_without_a_land_to_put_it_on() -> None:
    wild_growth = make(
        "Wild Growth",
        kinds=frozenset({CardKind.ENCHANTMENT}),
        mana=ManaAbility(1, frozenset("G"), delayed=False, ritual=False, on_land=True),
    )
    # Free to cast, and no land anywhere: it must stay in the hand.
    assert only_outcome(run([wild_growth, *repeat(VANILLA, 6)])).mana == (0, 0, 0, 0, 0)
    # With a land it is cast on turn one and adds its mana at once.
    assert only_outcome(run([wild_growth, FOREST, FOREST, *repeat(VANILLA, 4)])).mana == (2, 3, 3, 3, 3)


def test_a_mana_creature_gives_its_mana_the_turn_after_it_is_cast() -> None:
    elf = make(
        "Elf",
        mv=1,
        kinds=CREATURE,
        mana=ManaAbility(1, frozenset("G"), delayed=True, ritual=False),
    )
    assert only_outcome(run([FOREST, FOREST, elf, *repeat(VANILLA, 4)])).mana == (1, 3, 3, 3, 3)


def test_a_land_that_enters_tapped_gives_its_mana_the_turn_after() -> None:
    tapped = land("Tapped", "G", tapped=True)
    assert only_outcome(run([tapped, tapped, *repeat(VANILLA, 5)])).mana == (0, 1, 2, 2, 2)


def test_a_land_with_two_mana_counts_twice() -> None:
    tomb = land("Ancient Tomb", "C", amount=2)
    # Both lands are untapped and the Tomb pays more, so it goes down first.
    assert only_outcome(run([tomb, FOREST, *repeat(VANILLA, 5)])).mana == (2, 3, 3, 3, 3)


def test_city_of_traitors_is_sacrificed_when_the_next_land_is_played_after_its_mana_is_made() -> None:
    city = make(
        "City of Traitors",
        kinds=frozenset({CardKind.LAND}),
        land=LandFace(frozenset("C"), tapped=False, amount=2, lost_to_land_drop=True),
    )
    boss = make("Boss", mv=3, kinds=CREATURE)
    outcome = only_outcome(run([city, FOREST, *repeat(VANILLA, 5)], commanders=(boss,)))
    # Turn two taps the City for two before the Forest goes down: three mana cast the commander, and from then
    # on the Forest alone is left.
    assert outcome.commander_turns == (2,)
    assert outcome.mana == (2, 1, 1, 1, 1)


def test_an_untapped_land_is_played_ahead_of_a_tapped_one_when_the_mana_is_wanted() -> None:
    tapped = land("Tapped", "G", tapped=True)
    one_drop = make("One Drop", mv=1, kinds=CREATURE)
    # A one-drop wants turn one's mana: the untapped land first; the tapped one comes on turn two.
    report = run([tapped, FOREST, one_drop, *repeat(VANILLA, 4)])
    assert only_outcome(report).mana == (1, 1, 2, 2, 2)


def test_a_tapped_land_is_played_first_when_nothing_would_use_turn_ones_mana() -> None:
    tapped = land("Tapped", "G", tapped=True)
    # Only five-drops to cast: turn one's mana would be wasted, so the tapped land goes down first.
    assert only_outcome(run([tapped, FOREST, *repeat(VANILLA, 5)])).mana == (0, 2, 2, 2, 2)


def test_chrome_mox_exiles_a_spell_from_the_hand_and_gives_its_mana_at_once() -> None:
    mox = make(
        "Chrome Mox",
        kinds=ARTIFACT,
        mana=ManaAbility(1, ANY_COLOUR, delayed=False, ritual=False),
        entry_cost=EntryCost.NONLAND_CARD,
    )
    assert only_outcome(run([FOREST, FOREST, mox, *repeat(VANILLA, 4)])).mana == (2, 3, 3, 3, 3)


def test_chrome_mox_stays_in_the_hand_with_no_other_spell_to_exile() -> None:
    mox = make(
        "Chrome Mox",
        kinds=ARTIFACT,
        mana=ManaAbility(1, ANY_COLOUR, delayed=False, ritual=False),
        entry_cost=EntryCost.NONLAND_CARD,
    )
    assert only_outcome(run([*repeat(FOREST, 6), mox])).mana == (1, 2, 3, 4, 5)


def test_mox_diamond_discards_a_land_for_its_mana() -> None:
    diamond = make(
        "Mox Diamond",
        kinds=ARTIFACT,
        mana=ManaAbility(1, ANY_COLOUR, delayed=False, ritual=False),
        entry_cost=EntryCost.LAND_CARD,
    )
    # Five lands, one discarded for the diamond: four are played, one a turn, and the diamond is the fifth.
    assert only_outcome(run([*repeat(FOREST, 5), diamond])).mana == (2, 3, 4, 5, 5)


def test_a_spell_that_needs_a_creature_to_sacrifice_waits_for_one() -> None:
    sacrifice = make(
        "Artifact",
        kinds=ARTIFACT,
        needs_sacrifice=True,
        mana=ManaAbility(1, frozenset("G"), delayed=False, ritual=False),
    )
    creature = make("Creature", mv=1, kinds=CREATURE)
    alone = [*repeat(FOREST, 4), sacrifice, VANILLA, VANILLA]
    assert only_outcome(run(alone)).mana == (1, 2, 3, 4, 4)
    # The creature comes down first, then the artifact is cast at the cost of the creature.
    with_creature = [*repeat(FOREST, 4), sacrifice, creature, VANILLA]
    assert only_outcome(run(with_creature)).mana == (2, 3, 4, 5, 5)


def test_sacrificing_a_mana_creature_takes_only_its_own_mana_not_a_tapped_lands() -> None:
    elves = make(
        "Elves",
        mv=1,
        pips={"G": 1},
        kinds=CREATURE,
        mana=ManaAbility(1, frozenset("G"), delayed=True, ritual=False),
    )
    sacrifice = make("Sacrifice", mv=1, pips={"G": 1}, needs_sacrifice=True)
    nine_drop = make("Nine", mv=9, kinds=CREATURE)
    slots = [FOREST, land("Tapped Forest", "G", tapped=True), FOREST, elves, sacrifice, nine_drop, nine_drop]
    # Turn one: Forest, Elves. Turn two: the tapped land is played, Sacrifice eats Elves, one Forest is left.
    # Turn three: the tapped land is ready and the third Forest comes down: three. Elves' mana is gone.
    assert only_outcome(run(slots)).mana == (1, 1, 3, 3, 3)


def test_sacrificing_a_creature_that_grants_land_drops_takes_the_drops_away() -> None:
    oracle = make("Oracle", mv=1, pips={"G": 1}, kinds=CREATURE, extra_land_drops=1)
    sacrifice = make("Sacrifice", mv=1, pips={"G": 1}, needs_sacrifice=True)
    slots = [*repeat(FOREST, 5), oracle, sacrifice]
    # Turn one: Forest, Oracle, a second Forest, Sacrifice eats Oracle. Then one land a turn: 2, 3, 4, 5, 5.
    assert only_outcome(run(slots)).mana == (2, 3, 4, 5, 5)


def test_sacrificing_a_mana_commander_takes_its_mana_away() -> None:
    boss = make(
        "Boss",
        mv=1,
        pips={"G": 1},
        kinds=CREATURE,
        mana=ManaAbility(1, frozenset("G"), delayed=False, ritual=False),
    )
    sacrifice = make("Sacrifice", mv=1, pips={"G": 1}, needs_sacrifice=True)
    slots = [*repeat(FOREST, 5), sacrifice, VANILLA]
    # Turn one: Forest, Boss (its mana comes at once), Sacrifice eats Boss: only the Forest's mana is left.
    outcome = only_outcome(run(slots, commanders=(boss,)))
    assert outcome.commander_turns == (1,)
    assert outcome.mana == (1, 2, 3, 4, 5)


def test_a_sacrificed_draw_engine_draws_no_more_cards() -> None:
    engine = make("Engine", mv=1, pips={"G": 1}, kinds=CREATURE, draw=Draw(1, engine=True))
    sacrifice = make("Sacrifice", mv=1, pips={"G": 1}, needs_sacrifice=True)
    report = run([*repeat(FOREST, 16)], commanders=(engine, sacrifice))
    outcome = only_outcome(report)
    # Turn one casts the engine, turn two casts Sacrifice after drawing two cards and eats the engine.
    assert outcome.commander_turns == (1, 2)
    # Cards drawn: one on turn one, two on turn two, then one a turn: 7 + 1 + 2 + 3.
    assert outcome.shuffle_depths == (13,)


def test_a_ritual_is_cast_only_to_pay_for_something_this_turn() -> None:
    ritual = make(
        "Dark Ritual",
        mv=1,
        pips={"B": 1},
        kinds=frozenset({CardKind.INSTANT}),
        mana=ManaAbility(3, frozenset("B"), delayed=False, ritual=True),
    )
    boss = make("Boss", mv=4, kinds=CREATURE)
    slots = [SWAMP, SWAMP, ritual, *repeat(VANILLA, 4)]
    # Two swamps and the ritual make four mana on turn two for the four-drop; on turn one they make three.
    assert only_outcome(run(slots, commanders=(boss,))).commander_turns == (2,)
    # Without the commander to pay for, the ritual is never cast and the mana is only the lands.
    assert only_outcome(run(slots)).mana == (1, 2, 2, 2, 2)


def morph() -> GoldfishCard:
    """Manamorphose: two mana of any colour for two, and a card."""
    return make(
        "Morph", mv=2, draw=Draw(1, engine=False), mana=ManaAbility(2, ANY_COLOUR, delayed=False, ritual=True)
    )


def test_a_spell_that_makes_no_more_mana_than_it_costs_is_cast_for_its_card() -> None:
    drawn = only_outcome(run(repeat(FOREST, 16))).shuffle_depths[-1]
    report = run([*repeat(FOREST, 14), *repeat(morph(), 2)])
    # A morph among the cards dealt is cast on turn two, and draws a card the Forests alone would not.
    assert max(o.shuffle_depths[-1] for o in report.outcomes) > drawn


def test_a_spell_that_makes_no_more_mana_than_it_costs_gives_its_mana_back() -> None:
    piece = make("Piece", mv=2, kinds=CREATURE, combo=True)
    slots = [*repeat(FOREST, 3), morph(), piece, VANILLA, VANILLA]
    # Turn two's two Forests pay for the morph, whose two mana pay for the piece; without them, turn three.
    assert only_outcome(run(slots, combos=(combo_of(piece),))).combo_turns == (2,)


def cantor(
    mv: int = 1, pips: Mapping[str, int] | None = None, colours: frozenset[str] = ANY_COLOUR
) -> GoldfishCard:
    """Wild Cantor (mv 1) or Reckless Barbarian (mv 2, two mana): a creature that sacrifices itself for the
    mana it cost, so that it nets none."""
    return make(
        "Cantor",
        mv=mv,
        pips=pips or {"G": 1},
        kinds=CREATURE,
        mana=ManaAbility(mv, colours, delayed=False, ritual=True),
    )


def test_a_creature_that_sacrifices_itself_for_its_cost_is_cast_with_spare_mana_and_kept() -> None:
    boss = make("Boss", mv=3)
    slots = [*repeat(FOREST, 4), cantor(), *repeat(VANILLA, 2)]
    # Turn one's Forest is spare and pays for the Cantor, which gives nothing yet; turn two's two Forests and
    # its mana pay for the three-drop, where the Forests alone would on turn three.
    outcome = only_outcome(run(slots, commanders=(boss,)))
    assert outcome.commander_turns == (2,)
    assert outcome.mana == (1, 2, 3, 4, 4)


def test_a_creature_that_sacrifices_itself_for_its_cost_is_not_cast_by_a_ritual() -> None:
    ritual = make(
        "Dark Ritual",
        mv=1,
        pips={"B": 1},
        kinds=frozenset({CardKind.INSTANT}),
        mana=ManaAbility(3, frozenset("B"), delayed=False, ritual=True),
    )
    barbarian = make(
        "Barbarian",
        mv=2,
        pips={"B": 2},
        kinds=CREATURE,
        mana=ManaAbility(2, frozenset("B"), delayed=False, ritual=True),
    )
    boss = make("Boss", mv=6)
    slots = [*repeat(SWAMP, 4), ritual, barbarian, VANILLA]
    # Turn one's Swamp cannot pay for the barbarian, and the ritual is not spent on it. Turn two's two Swamps
    # do. Turn three: three Swamps and its two make five, and the ritual makes six; had the ritual been spent
    # on turn one, the six would come on turn four.
    assert only_outcome(run(slots, commanders=(boss,))).commander_turns == (3,)


def test_a_creature_that_sacrifices_itself_for_its_cost_is_not_cast_by_a_treasure() -> None:
    maker = replace(make("Maker", mv=1, kinds=CREATURE), treasures_on_entry=1)
    barbarian = cantor(3)
    boss = make("Boss", mv=6)
    slots = [*repeat(FOREST, 4), maker, barbarian, VANILLA]
    # Turn two's two Forests and the Treasure could pay for the barbarian, but the Treasure is kept: it is
    # cast on turn three, and turn four's four Forests, the Treasure and its three make eight, for the six.
    assert only_outcome(run(slots, commanders=(boss,))).commander_turns == (4,)


def test_a_creature_that_sacrifices_itself_for_its_cost_is_cast_after_the_spells_it_crowds() -> None:
    lossy = make(
        "Lossy",
        mv=2,
        pips={"B": 2},
        kinds=CREATURE,
        mana=ManaAbility(1, frozenset("B"), delayed=False, ritual=True),
    )
    piece = make("Piece", mv=2, pips={"B": 2}, kinds=CREATURE, combo=True)
    slots = [*repeat(SWAMP, 3), lossy, piece, *repeat(VANILLA, 2)]
    # Turn two's two Swamps pay for the piece, not for the creature that would give back one mana of two.
    assert only_outcome(run(slots, combos=(combo_of(piece),))).combo_turns == (2,)


def test_a_creature_that_sacrifices_itself_for_mana_is_not_spent_where_a_spell_needs_it_to_sacrifice() -> (
    None
):
    boss = make("Boss", mv=3, needs_sacrifice=True)
    slots = [*repeat(FOREST, 4), cantor(), *repeat(VANILLA, 2)]
    # Turn two's two Forests and the Cantor's mana would make the three, but then nothing is left to
    # sacrifice for the boss: it is cast on turn three, eating the Cantor.
    assert only_outcome(run(slots, commanders=(boss,))).commander_turns == (3,)


def test_a_creature_that_sacrificed_itself_for_mana_is_no_longer_there_to_attack() -> None:
    raider = replace(cantor(), treasures_on_attack=1)
    piece = make("Piece", mv=4, kinds=CREATURE, combo=True)
    boss = make("Boss", mv=3)
    slots = [*repeat(FOREST, 4), raider, piece, VANILLA]
    # Turn two's two Forests and its mana pay for the boss before combat, so it does not attack for a
    # Treasure, and turn three's three Forests fall short of the piece's four until turn four.
    outcome = only_outcome(run(slots, commanders=(boss,), combos=(combo_of(piece),)))
    assert outcome.commander_turns == (2,)
    assert outcome.combo_turns == (4,)


def test_a_creature_that_sacrifices_itself_for_mana_is_sacrificed_after_a_creature_that_does_not() -> None:
    bear = make("Bear", mv=1, kinds=CREATURE)
    plunge = make(
        "Infernal Plunge",
        mv=1,
        pips={"R": 1},
        needs_sacrifice=True,
        mana=ManaAbility(3, frozenset("R"), delayed=False, ritual=True),
    )
    boss = make("Boss", mv=6)
    slots = [*repeat(MOUNTAIN, 4), cantor(pips={"R": 1}), bear, plunge]
    # The bear is cast on turn one and the Cantor with the spare Mountain on turn two. Turn three: three
    # Mountains and the ritual that sacrifices the bear make five, and the Cantor's mana makes the six.
    assert only_outcome(run(slots, commanders=(boss,))).commander_turns == (3,)


def test_a_creature_that_sacrifices_itself_for_mana_is_no_artifact() -> None:
    opal = make(
        "Opal",
        kinds=ARTIFACT,
        mana=ManaAbility(1, ANY_COLOUR, delayed=False, ritual=False, needs=ManaNeed.ARTIFACTS),
    )
    trinket = make("Trinket", mv=1, kinds=ARTIFACT)
    slots = [MOUNTAIN, MOUNTAIN, opal, trinket, cantor(), *repeat(VANILLA, 2)]
    # The Opal and the trinket are two artifacts, and the Cantor from turn two does not make a third.
    assert only_outcome(run(slots)).mana == (1, 2, 2, 2, 2)


def test_a_ritual_is_kept_from_a_plain_spell_for_the_commander() -> None:
    ritual = make(
        "Dark Ritual",
        mv=1,
        pips={"B": 1},
        kinds=frozenset({CardKind.INSTANT}),
        mana=ManaAbility(3, frozenset("B"), delayed=False, ritual=True),
    )
    boss = make("Boss", mv=4, kinds=CREATURE)
    bear = make("Bear", mv=3, kinds=CREATURE)
    slots = [*repeat(SWAMP, 4), ritual, bear, VANILLA]
    # Turn one's Swamp and ritual would cast the three-drop; held, the ritual casts the commander on turn two.
    assert only_outcome(run(slots, commanders=(boss,))).commander_turns == (2,)


def test_a_ritual_that_sacrifices_a_creature_waits_for_one_and_sacrifices_it() -> None:
    plunge = make(
        "Infernal Plunge",
        mv=1,
        pips={"R": 1},
        needs_sacrifice=True,
        mana=ManaAbility(3, frozenset("R"), delayed=False, ritual=True),
    )
    boss = make("Boss", mv=3, kinds=CREATURE)
    slots = [*repeat(MOUNTAIN, 4), plunge, *repeat(VANILLA, 2)]
    # With no creature to sacrifice, turn one's Mountain and the ritual do not make three.
    assert only_outcome(run(slots, commanders=(boss,))).commander_turns == (3,)
    # Turn two sacrifices the mana creature cast on turn one: two Mountains, its mana and the ritual's net two
    # make five, and its mana is gone from then on.
    dork = make(
        "Dork",
        mv=1,
        pips={"R": 1},
        kinds=CREATURE,
        mana=ManaAbility(1, frozenset("R"), delayed=True, ritual=False),
    )
    slots = [*repeat(MOUNTAIN, 4), plunge, dork, VANILLA]
    outcome = only_outcome(run(slots, commanders=(make("Big", mv=5, kinds=CREATURE),)))
    assert outcome.commander_turns == (2,)
    assert outcome.mana == (1, 2, 3, 4, 4)


def test_a_ritual_is_not_paid_for_with_the_creature_that_pays_life_for_mana() -> None:
    ogre = make(
        "Ogre",
        mv=4,
        pips={"R": 1},
        kinds=CREATURE,
        mana=ManaAbility(1, frozenset("R"), delayed=False, ritual=False, life=3),
    )
    plunge = make(
        "Infernal Plunge",
        mv=1,
        pips={"R": 1},
        needs_sacrifice=True,
        mana=ManaAbility(3, frozenset("R"), delayed=False, ritual=True),
    )
    slots = [*repeat(MOUNTAIN, 4), ogre, plunge, make("Giant", mv=20, kinds=CREATURE)]
    # Turn five: sacrificing the ogre to the ritual makes six; keeping it, four Mountains and ten life make
    # the fourteen.
    boss = make("Boss", mv=14, kinds=CREATURE)
    assert only_outcome(run(slots, commanders=(boss,))).commander_turns == (5,)


def test_a_ritual_for_a_sacrificed_creature_adds_its_mana_value() -> None:
    metamorphosis = make(
        "Metamorphosis",
        mv=1,
        pips={"G": 1},
        needs_sacrifice=True,
        mana=ManaAbility(1, ANY_COLOUR, delayed=False, ritual=True, spend_on=CREATURE, plus_sacrificed=True),
    )
    beast = make("Beast", mv=3, kinds=CREATURE)
    slots = [*repeat(FOREST, 4), metamorphosis, beast, VANILLA]
    # Turn three casts the three-drop; turn four sacrifices it for four mana, which with three Forests left
    # after paying make seven for the six-drop.
    assert only_outcome(run(slots, commanders=(make("Boss", mv=6, kinds=CREATURE),))).commander_turns == (4,)


def test_lions_eye_diamond_casts_the_commander_and_discards_the_hand_but_pays_for_nothing_else() -> None:
    diamond = make(
        "Lion's Eye Diamond",
        kinds=ARTIFACT,
        mana=ManaAbility(3, ANY_COLOUR, delayed=False, ritual=True, discards_hand=True),
    )
    slots = [*repeat(MOUNTAIN, 3), diamond, *repeat(make("Bear", mv=3, kinds=CREATURE), 3)]
    outcome = only_outcome(run(slots, commanders=(make("Boss", mv=4, kinds=CREATURE),)))
    # Turn one: a Mountain and the Diamond's three cast the four-drop, and the other Mountains go with the
    # hand.
    assert outcome.commander_turns == (1,)
    assert outcome.mana == (1, 1, 1, 1, 1)
    # A three-drop is no reason to use it: the Mountains are played as they come.
    assert only_outcome(run(slots)).mana == (1, 2, 3, 3, 3)


def test_a_spirit_guide_pays_for_a_two_drop_on_turn_one() -> None:
    guide = make(
        "Elvish Spirit Guide",
        mv=3,
        pips={"G": 1},
        kinds=CREATURE,
        mana=ManaAbility(1, frozenset("G"), delayed=False, ritual=True, from_hand=True),
    )
    boss = make("Boss", mv=2, pips={"G": 1}, kinds=CREATURE)
    slots = [FOREST, FOREST, guide, *repeat(VANILLA, 4)]
    assert only_outcome(run(slots, commanders=(boss,))).commander_turns == (1,)
    # A spirit guide is exiled for its mana, never cast: it takes no permanent mana.
    assert only_outcome(run(slots)).mana == (1, 2, 2, 2, 2)
    without = [FOREST, FOREST, *repeat(VANILLA, 5)]
    assert only_outcome(run(without, commanders=(boss,))).commander_turns == (2,)


def test_a_rock_that_taps_a_creature_makes_mana_once_a_creature_is_out_even_a_new_one() -> None:
    drum = make(
        "Drum",
        mv=1,
        kinds=ARTIFACT,
        mana=ManaAbility(1, ANY_COLOUR, delayed=False, ritual=False, needs=ManaNeed.CREATURE),
    )
    bear = make("Bear", mv=1, kinds=CREATURE)
    slots = [FOREST, FOREST, drum, bear, *repeat(VANILLA, 3)]
    # Turn one casts the drum with nothing to tap; turn two the bear, which the drum taps at once.
    assert only_outcome(run(slots)).mana == (1, 3, 3, 3, 3)


SPRINGLEAF_DRUM = make(
    "Drum",
    mv=1,
    kinds=ARTIFACT,
    mana=ManaAbility(1, ANY_COLOUR, delayed=False, ritual=False, needs=ManaNeed.CREATURE),
)


def test_a_rock_that_taps_a_creature_cannot_tap_one_that_is_tapped_for_its_own_mana() -> None:
    elf = make(
        "Elf",
        mv=1,
        pips={"G": 1},
        kinds=CREATURE,
        mana=ManaAbility(1, frozenset("G"), delayed=True, ritual=False),
    )
    slots = [FOREST, FOREST, SPRINGLEAF_DRUM, elf, *repeat(VANILLA, 3)]
    # Whichever of the two comes first, turn two has the two Forests and the elf's mana or the drum's, which
    # taps the elf while it is summoning sick; from turn three the elf taps for itself: 3 and not 4.
    assert {o.mana for o in run(slots).outcomes} == {(1, 3, 3, 3, 3)}


def test_a_creature_tapped_for_a_rock_does_not_attack() -> None:
    raider = replace(make("Raider", mv=1, kinds=CREATURE), treasures_on_attack=1)
    piece = make("Piece", mv=4, kinds=CREATURE, combo=True)
    slots = [FOREST, FOREST, SPRINGLEAF_DRUM, raider, piece, VANILLA, VANILLA]
    # Two Forests and the drum tapping the raider make 3, and the tapped raider makes no Treasure for a 4.
    assert only_outcome(run(slots, combos=(combo_of(piece),))).combo_turns == (0,)
    # Without the drum it attacks from turn two: turn three has three Forests and a Treasure, for the 4.
    slots = [FOREST, FOREST, FOREST, raider, piece, VANILLA, VANILLA]
    assert only_outcome(run(slots, combos=(combo_of(piece),))).combo_turns == (3,)


def test_a_creature_tapped_for_its_own_mana_does_not_attack() -> None:
    raider = replace(
        make(
            "Raider",
            mv=1,
            pips={"G": 1},
            kinds=CREATURE,
            mana=ManaAbility(1, frozenset("G"), delayed=True, ritual=False),
        ),
        treasures_on_attack=1,
    )
    piece = make("Piece", mv=4, kinds=CREATURE, combo=True)
    slots = [FOREST, FOREST, raider, piece, *repeat(VANILLA, 3)]
    # Turn two: two Forests and the raider's mana are 3; attacking would add a Treasure, but it is tapped.
    assert only_outcome(run(slots, combos=(combo_of(piece),))).combo_turns == (0,)


def test_mox_opal_makes_mana_from_the_third_artifact_on() -> None:
    opal = make(
        "Opal",
        kinds=ARTIFACT,
        mana=ManaAbility(1, ANY_COLOUR, delayed=False, ritual=False, needs=ManaNeed.ARTIFACTS),
    )
    trinket = make("Trinket", mv=1, kinds=ARTIFACT)
    slots = [MOUNTAIN, MOUNTAIN, opal, trinket, trinket, *repeat(VANILLA, 2)]
    # Turn one: the Opal and one trinket; turn two the second trinket makes three artifacts.
    assert only_outcome(run(slots)).mana == (1, 3, 3, 3, 3)


def test_mana_paid_for_with_life_is_used_down_to_ten_life() -> None:
    ogre = make(
        "Ogre",
        mv=4,
        pips={"R": 1},
        kinds=CREATURE,
        mana=ManaAbility(1, frozenset("R"), delayed=False, ritual=False, life=3),
    )
    slots = [*repeat(MOUNTAIN, 4), ogre, *repeat(make("Giant", mv=20, kinds=CREATURE), 2)]
    # Turn four casts the ogre; forty life down to ten pays for ten mana at once, and no more.
    assert only_outcome(run(slots, commanders=(make("Ten", mv=10, kinds=CREATURE),))).commander_turns == (4,)
    assert only_outcome(run(slots, commanders=(make("Eleven", mv=11, kinds=CREATURE),))).commander_turns == (
        5,
    )


def test_a_creature_that_sacrifices_a_land_for_mana_does_it_once_its_turn_has_passed() -> None:
    jack = make(
        "Lumberjack",
        mv=1,
        pips={"R": 1},
        kinds=CREATURE,
        mana=ManaAbility(
            3, frozenset("RG"), delayed=True, ritual=False, land_sacrificed=frozenset({"forest"})
        ),
    )
    forest = replace(FOREST, land_types=frozenset({"forest"}))
    giant = make("Giant", mv=20, kinds=CREATURE)
    slots = [MOUNTAIN, forest, forest, jack, *repeat(giant, 3)]
    boss = make("Boss", mv=5, kinds=CREATURE)
    outcome = only_outcome(run(slots, commanders=(boss,)))
    # Turn two: two lands and the three mana for a Forest cast the five-drop; the Forest is gone after.
    assert outcome.commander_turns == (2,)
    assert outcome.mana == (1, 1, 2, 2, 2)


def test_two_creatures_that_sacrifice_a_forest_share_the_one_forest_there_is() -> None:
    def jack(name: str) -> GoldfishCard:
        mana = ManaAbility(
            3, frozenset("RG"), delayed=True, ritual=False, land_sacrificed=frozenset({"forest"})
        )
        return make(name, mv=1, pips={"R": 1}, kinds=CREATURE, mana=mana)

    forest = replace(FOREST, land_types=frozenset({"forest"}))
    giant = make("Giant", mv=20, kinds=CREATURE)
    slots = [MOUNTAIN, forest, jack("Jack"), jack("Jill"), *repeat(giant, 3)]
    # One Forest feeds one of them, once: turn three has the two lands and 3 mana, and never 6.
    five = only_outcome(run(slots, commanders=(make("Five", mv=5, kinds=CREATURE),)))
    assert five.commander_turns == (3,)
    six = only_outcome(run(slots, commanders=(make("Six", mv=6, kinds=CREATURE),)))
    assert six.commander_turns == (0,)


def test_mana_spent_only_on_creatures_casts_a_creature_and_its_rest_pays_for_nothing_else() -> None:
    def surge(spend_on: frozenset[CardKind]) -> GoldfishCard:
        mana = ManaAbility(7, ANY_COLOUR, delayed=False, ritual=True, spend_on=spend_on)
        return make("Surge", mv=4, pips={"R": 4}, mana=mana)

    beast = make("Beast", mv=5, kinds=CREATURE)
    relic = make("Relic", mv=2, pips={"G": 2}, kinds=ARTIFACT)
    slots = [*repeat(MOUNTAIN, 4), surge(CREATURE), *repeat(VANILLA, 2)]
    # Four Mountains pay for the surge on turn four; its seven mana casts the five-drop creature, and the two
    # left over cannot pay for the green artifact, which the Mountains never could.
    assert only_outcome(run(slots, commanders=(beast, relic))).commander_turns == (4, 0)
    # Restricted to artifacts, it pays for the green artifact and not for the creature.
    slots = [*repeat(MOUNTAIN, 4), surge(ARTIFACT), *repeat(VANILLA, 2)]
    assert only_outcome(run(slots, commanders=(beast, relic))).commander_turns == (0, 4)


def test_the_commander_is_cast_once_on_the_first_turn_its_colours_are_paid() -> None:
    boss = make("Boss", mv=3, pips={"R": 1}, kinds=CREATURE)
    slots = [MOUNTAIN, FOREST, FOREST, *repeat(VANILLA, 4)]
    report = run(slots, commanders=(boss,))
    assert only_outcome(report).commander_turns == (3,)
    assert report.commanders[0].cast_by == (0.0, 0.0, 1.0, 1.0, 1.0)
    assert report.commanders[0].median_turn == 3


def test_a_commander_cast_in_less_than_half_the_games_has_no_median_turn() -> None:
    boss = make("Boss", mv=9, kinds=CREATURE)
    report = run([*repeat(FOREST, 7)], commanders=(boss,))
    assert report.commanders[0].cast_by == (0.0,) * 5
    assert report.commanders[0].median_turn is None


def test_two_commanders_are_figured_apart() -> None:
    small = make("Small", mv=1, kinds=CREATURE)
    big = make("Big", mv=2, kinds=CREATURE)
    report = run([*repeat(FOREST, 7)], commanders=(small, big))
    assert [c.name for c in report.commanders] == ["Small", "Big"]
    assert [c.median_turn for c in report.commanders] == [1, 2]


# --- colours ---------------------------------------------------------------------------------------------


def test_a_stuck_spell_is_no_screw_when_all_the_mana_went_on_other_plays() -> None:
    red = make("Red One", mv=1, pips={"R": 1}, kinds=CREATURE)
    green_one = make("Green One", mv=1, pips={"G": 1}, kinds=CREATURE)
    green_two_a = make("Green Two A", mv=2, pips={"G": 1}, kinds=CREATURE)
    green_two_b = make("Green Two B", mv=2, pips={"G": 1}, kinds=CREATURE)
    # Two Forests: turn one's mana pays the one-drop, turns two and three's the two-drops; the red spell never
    # had mana to spare.
    slots = [FOREST, FOREST, red, green_one, green_two_a, green_two_b, VANILLA]
    assert run(slots).colour_screw == 0.0


def test_a_stuck_spell_is_a_screw_when_mana_was_left_unspent() -> None:
    red = make("Red One", mv=1, pips={"R": 1}, kinds=CREATURE)
    assert run([FOREST, FOREST, red, *repeat(VANILLA, 4)]).colour_screw == 1.0


def test_a_spell_stuck_on_colours_that_the_policy_holds_back_is_no_screw() -> None:
    red_finisher = make("Red Finisher", mv=1, pips={"R": 1}, finisher=True)
    assert run([FOREST, FOREST, red_finisher, *repeat(VANILLA, 4)]).colour_screw == 0.0


def test_off_colour_lands_leave_a_spell_with_the_mana_but_not_the_colours() -> None:
    red = make("Red Spell", mv=2, pips={"R": 2}, kinds=CREATURE)
    report = run([*repeat(FOREST, 4), red, VANILLA, VANILLA])
    assert report.colour_screw == 1.0
    assert report.colour_screw_turn == 3


def test_a_mountain_in_the_hand_is_played_for_the_red_spell_and_there_is_no_screw() -> None:
    red = make("Red Spell", mv=2, pips={"R": 1}, kinds=CREATURE)
    report = run([MOUNTAIN, *repeat(FOREST, 3), red, VANILLA, VANILLA])
    assert report.colour_screw == 0.0


def test_a_commander_stuck_on_colours_counts_as_a_screw() -> None:
    boss = make("Boss", mv=2, pips={"U": 1}, kinds=CREATURE)
    report = run([*repeat(FOREST, 7)], commanders=(boss,))
    assert report.colour_screw == 1.0


def test_two_coloured_costs_are_paid_from_dual_lands_that_cover_both() -> None:
    dual = land("Dual", "WU")
    spell = make("Spell", mv=2, pips={"W": 1, "U": 1}, kinds=CREATURE)
    boss = make("Boss", mv=2, pips={"W": 1}, kinds=CREATURE)
    # Two duals pay {W}{U}; the commander is cast first (turn two) and the spell is stuck behind it.
    report = run([dual, dual, spell, *repeat(VANILLA, 4)], commanders=(boss,))
    assert report.colour_screw == 0.0
    assert only_outcome(report).commander_turns == (2,)


def test_a_hybrid_pip_needs_a_unit_of_either_of_its_colours() -> None:
    oona = goldfish.cost_of(2, {"U/B": 2})
    assert not goldfish.can_pay([C, C], oona)
    assert not goldfish.can_pay([W, G], oona)
    assert not goldfish.can_pay([U, W], oona)
    assert goldfish.can_pay([U, B_], oona)
    assert goldfish.can_pay([B_, B_], oona)
    assert goldfish.can_pay([U | W, B_ | G], oona)


def test_a_hybrid_pip_beside_a_fixed_one_is_matched_by_hall() -> None:
    mixed = goldfish.cost_of(2, {"W": 1, "W/U": 1})
    assert goldfish.can_pay([W, U], mixed)
    assert goldfish.can_pay([W, W], mixed)
    assert not goldfish.can_pay([U, U], mixed)
    assert not goldfish.can_pay([W, C], mixed)


def test_a_hybrid_pip_takes_the_least_flexible_unit_of_either_colour() -> None:
    units = [C, U, W | G]
    goldfish.pay(units, goldfish.cost_of(2, {"W/U": 1}))
    assert units == [W | G]


def test_a_hybrid_commander_is_not_cast_from_colourless_mana() -> None:
    boss = make("Boss", mv=2, pips={"U/B": 2}, kinds=CREATURE)
    wastes = land("Wastes", "C")
    for lands in ([wastes, wastes], [FOREST, FOREST]):
        slots = [*lands, *repeat(VANILLA, 5)]
        assert only_outcome(run(slots, commanders=(boss,))).commander_turns == (0,)


def test_a_hybrid_commander_is_cast_with_either_of_its_colours() -> None:
    boss = make("Boss", mv=2, pips={"U/B": 2}, kinds=CREATURE)
    island = land("Island", "U")
    for slots in ([island, SWAMP], [island, island], [SWAMP, SWAMP]):
        assert only_outcome(run([*slots, *repeat(VANILLA, 5)], commanders=(boss,))).commander_turns == (2,)


def test_a_land_of_a_colour_a_hybrid_spell_asks_for_is_played_first() -> None:
    boss = make("Boss", mv=2, pips={"G": 1, "U/B": 1}, kinds=CREATURE)
    plains = land("Plains", "W")
    island = land("Island", "U")
    # Turn one takes the Forest. Turn two takes the Island, not the Plains, which pays neither of the hybrid
    # colours, so the commander comes out on turn two.
    slots = [FOREST, plains, island, *repeat(VANILLA, 4)]
    assert only_outcome(run(slots, commanders=(boss,))).commander_turns == (2,)


def test_a_hybrid_spell_stuck_on_colours_counts_as_a_screw() -> None:
    boss = make("Boss", mv=2, pips={"U/B": 1}, kinds=CREATURE)
    assert run([*repeat(FOREST, 7)], commanders=(boss,)).colour_screw == 1.0
    island = land("Island", "U")
    assert run([island, island, *repeat(VANILLA, 5)], commanders=(boss,)).colour_screw == 0.0


# --- ways to win -----------------------------------------------------------------------------------------


def test_a_deck_without_combos_or_finishers_has_no_ways_to_win_and_plays_none() -> None:
    report = run([*repeat(FOREST, 7)])
    assert report.ways_to_win is None
    outcome = only_outcome(report)
    assert outcome.combo_turns == ()
    assert outcome.finisher_turns == ()


def test_a_two_card_combo_of_cheap_permanents_lands_on_turn_two() -> None:
    a = make("A", mv=1, kinds=ARTIFACT, combo=True)
    b = make("B", mv=1, kinds=CREATURE, combo=True)
    slots = [*repeat(FOREST, 3), a, b, VANILLA, VANILLA]
    report = run(slots, combos=(combo_of(a, b),), bracket=4)
    assert only_outcome(report).combo_turns == (2,)
    ways = report.ways_to_win
    assert ways is not None
    assert ways.combos[0].landed_by == (1.0, 1.0, 1.0, 1.0, 1.0)
    assert ways.combos[0].fetched_share == 0.0
    # It lands by bracket 4's turn four in every game.
    assert ways.faster_than_bracket == ("1-2",)


def test_a_combo_is_never_faster_than_bracket_five_or_a_deck_with_no_bracket() -> None:
    a = make("A", mv=1, kinds=ARTIFACT, combo=True)
    b = make("B", mv=1, kinds=CREATURE, combo=True)
    slots = [*repeat(FOREST, 3), a, b, VANILLA, VANILLA]
    for bracket in (5, None):
        ways = run(slots, combos=(combo_of(a, b),), bracket=bracket).ways_to_win
        assert ways is not None
        assert ways.faster_than_bracket == ()


def test_a_combo_landing_later_than_the_bracket_expects_is_not_warned_about() -> None:
    a = make("A", mv=7, kinds=ARTIFACT, combo=True)
    b = make("B", mv=7, kinds=CREATURE, combo=True)
    slots = [*repeat(FOREST, 8), a, b, VANILLA]
    # Each piece wants seven lands and one is cast a turn: far later than bracket 3's turn six.
    report = run(slots, combos=(combo_of(a, b),), bracket=3)
    ways = report.ways_to_win
    assert ways is not None
    assert ways.faster_than_bracket == ()
    # The first piece on turn seven, the second on turn eight: the last turn shown.
    assert ways.combos[0].landed_by == (0.0, 0.0, 0.0, 0.0, 1.0)


def test_the_warning_needs_a_quarter_of_the_games() -> None:
    a = make("A", mv=1, kinds=ARTIFACT, combo=True)
    b = make("B", mv=1, kinds=CREATURE, combo=True)
    # Fourteen cards deal seven plus the draws; the pair is together by turn four in a minority of games.
    slots = [*repeat(FOREST, 7), a, b, *repeat(VANILLA, 40)]
    report = run(slots, combos=(combo_of(a, b),), bracket=4, games=1000, seed=3)
    ways = report.ways_to_win
    assert ways is not None
    # Both among the first 11 cards of 49: (11 choose 2) / (49 choose 2) = 55 / 1176, about 5%.
    assert ways.combos[0].landed_by[2] < goldfish.WARN_SHARE
    assert ways.faster_than_bracket == ()


def test_a_commander_piece_counts_once_it_is_out_and_is_not_free() -> None:
    boss = make("Boss", mv=3, kinds=CREATURE, combo=True)
    piece = make("Piece", mv=1, kinds=ARTIFACT, combo=True)
    slots = [*repeat(FOREST, 4), piece, VANILLA, VANILLA]
    combos = (combo_of(boss, piece),)
    # The piece is cast on turn one, the commander on turn three, and the pair is out then.
    assert only_outcome(run(slots, commanders=(boss,), combos=combos)).combo_turns == (3,)


def test_a_commander_piece_is_castable_with_the_mana_left_for_a_held_piece() -> None:
    boss = make("Boss", mv=2, kinds=CREATURE, combo=True)
    held = make("Held", mv=1, kinds=frozenset({CardKind.INSTANT}), combo=True)
    # Turn two has two lands: the commander is cast for two, and the instant waits for turn three's mana.
    slots = [*repeat(FOREST, 4), held, VANILLA, VANILLA]
    combos = (combo_of(boss, held),)
    assert only_outcome(run(slots, commanders=(boss,), combos=combos)).combo_turns == (3,)


def test_an_instant_or_sorcery_piece_that_also_draws_is_held_for_the_winning_turn() -> None:
    piece = make("Piece", mv=2, kinds=CREATURE, combo=True)
    cantrip = make("Cantrip", mv=1, combo=True, draw=Draw(1, engine=False))
    slots = [*repeat(FOREST, 4), piece, cantrip, VANILLA]
    # The sorcery is not cast for its card on turn one; the creature lands on turn two and the pair on three.
    assert only_outcome(run(slots, combos=(combo_of(piece, cantrip),))).combo_turns == (3,)


def test_a_commander_piece_that_needs_only_the_command_zone_is_free() -> None:
    boss = make("Boss", mv=3, kinds=CREATURE, combo=True)
    piece = make("Piece", mv=1, kinds=ARTIFACT, combo=True)
    slots = [*repeat(FOREST, 4), piece, VANILLA, VANILLA]
    combos = (GoldfishCombo("1-2", ("Boss", "Piece"), (), (), command_zone=("Boss",)),)
    assert only_outcome(run(slots, commanders=(boss,), combos=combos)).combo_turns == (1,)


def test_a_tutor_that_fetches_a_missing_piece_lands_the_combo_and_is_counted() -> None:
    tutor = make(
        "Tutor",
        mv=1,
        kinds=frozenset({CardKind.SORCERY}),
        tutor=Tutor(frozenset({CardKind.ARTIFACT, CardKind.CREATURE}), TutorDestination.HAND),
    )
    a = make("A", mv=1, kinds=ARTIFACT, combo=True)
    b = make("B", mv=1, kinds=CREATURE, combo=True)
    # Nine cards: seven dealt, the turn-one draw the eighth. The ninth stays in the library, and when it is
    # a piece the tutor fetches it on turn one; either way the pair is cast together on turn two.
    slots = [*repeat(FOREST, 5), tutor, a, b, VANILLA]
    report = run(slots, combos=(combo_of(a, b),), bracket=4, games=3000, seed=5)
    ways = report.ways_to_win
    assert ways is not None
    assert all(o.combo_turns == (2,) for o in report.outcomes)
    # The last card is a piece in 2 of 9 games; four standard errors are 0.03.
    assert ways.combos[0].fetched_share == pytest.approx(2 / 9, abs=0.035)
    fetched = [o for o in report.outcomes if o.combo_fetched & 1]
    assert fetched
    assert all(o.searched for o in fetched)
    assert all(o.shuffle_depths[-1] == 9 for o in fetched)


def test_a_tutor_that_puts_the_card_on_top_delays_the_combo_by_a_turn() -> None:
    free_a = make("A", kinds=ARTIFACT, combo=True)
    free_b = make("B", kinds=CREATURE, combo=True)
    finds = frozenset({CardKind.ARTIFACT, CardKind.CREATURE})
    slots_for = {
        destination: [
            *repeat(FOREST, 5),
            make("Tutor", mv=1, tutor=Tutor(finds, destination)),
            free_a,
            free_b,
            VANILLA,
        ]
        for destination in (TutorDestination.HAND, TutorDestination.TOP)
    }
    combos = (combo_of(free_a, free_b),)
    ways_hand = run(slots_for[TutorDestination.HAND], combos=combos, games=3000, seed=5).ways_to_win
    ways_top = run(slots_for[TutorDestination.TOP], combos=combos, games=3000, seed=5).ways_to_win
    assert ways_hand is not None
    assert ways_top is not None
    # In hand the pair is cast at once, so every game has landed by turn one.
    assert ways_hand.combos[0].landed_by[0] == 1.0
    # On top it is drawn on turn two: the 2 of 9 games with a piece left in the library land then.
    assert ways_top.combos[0].landed_by[0] == pytest.approx(7 / 9, abs=0.035)
    assert ways_top.combos[0].landed_by[1] == 1.0


def test_a_tutor_limited_by_mana_value_fetches_only_cards_within_it() -> None:
    cheap_tutor = make(
        "Cheap Tutor",
        mv=1,
        tutor=Tutor(frozenset({CardKind.CREATURE}), TutorDestination.HAND, max_mana_value=2),
    )
    a = make("A", mv=1, kinds=CREATURE, combo=True)
    dear = make("B", mv=4, kinds=CREATURE, combo=True)
    slots = [*repeat(FOREST, 5), cheap_tutor, a, dear]
    report = run(slots, combos=(combo_of(a, dear),), bracket=4, games=500)
    # The four-drop is out of its reach, so a search never resolves for it.
    assert not any(o.combo_fetched for o in report.outcomes)


def fetched_share(tutor: Tutor, a: GoldfishCard, b: GoldfishCard) -> float:
    """The share of the games landing the pair that a tutor helped, in a nine-card deck whose ninth card is
    left in the library after turn one's draw: 1 in 9 when the tutor finds only A, 2 in 9 when it finds
    both."""
    slots = [*repeat(FOREST, 5), make("Tutor", mv=1, tutor=tutor), a, b, VANILLA]
    ways = run(slots, combos=(combo_of(a, b),), bracket=4, games=3000, seed=5).ways_to_win
    assert ways is not None
    return ways.combos[0].fetched_share


def test_a_tutor_for_a_colour_finds_only_cards_of_that_colour() -> None:
    green = make("A", mv=1, pips={"G": 1}, kinds=CREATURE, combo=True)
    colourless = make("B", mv=1, kinds=CREATURE, combo=True)
    tutor = Tutor(frozenset({CardKind.CREATURE}), TutorDestination.HAND, colours=frozenset("G"))
    # Four standard errors are 0.03.
    assert fetched_share(tutor, green, colourless) == pytest.approx(1 / 9, abs=0.035)
    assert fetched_share(replace(tutor, colours=frozenset()), green, colourless) == pytest.approx(
        2 / 9, abs=0.035
    )


def test_a_tutor_for_a_power_ceiling_finds_only_creatures_within_it() -> None:
    small = replace(make("A", mv=1, kinds=CREATURE, combo=True), power=2)
    big = replace(make("B", mv=1, kinds=CREATURE, combo=True), power=3)
    tutor = Tutor(frozenset({CardKind.CREATURE}), TutorDestination.HAND, max_power=2)
    assert fetched_share(tutor, small, big) == pytest.approx(1 / 9, abs=0.035)


def test_a_tutor_for_an_artifact_or_a_subtype_finds_either_and_a_changeling_has_every_type() -> None:
    dragon = replace(make("A", mv=1, kinds=CREATURE, combo=True), subtypes=frozenset({"dragon"}))
    elf = replace(make("B", mv=1, kinds=CREATURE, combo=True), subtypes=frozenset({"elf"}))
    tutor = Tutor(
        frozenset({CardKind.ARTIFACT}), TutorDestination.HAND, subtypes=frozenset({"dragon"}), either=True
    )
    assert fetched_share(tutor, dragon, elf) == pytest.approx(1 / 9, abs=0.035)
    shifter = replace(elf, subtypes=frozenset({CHANGELING}))
    assert fetched_share(tutor, dragon, shifter) == pytest.approx(2 / 9, abs=0.035)
    relic = make("B", mv=1, kinds=ARTIFACT, combo=True)
    assert fetched_share(tutor, dragon, relic) == pytest.approx(2 / 9, abs=0.035)


def test_a_changeling_has_every_creature_type_and_no_other_subtype() -> None:
    sword = replace(make("A", mv=1, kinds=ARTIFACT, combo=True), subtypes=frozenset({"equipment"}))
    shifter = replace(make("B", mv=1, kinds=CREATURE, combo=True), subtypes=frozenset({CHANGELING}))
    # Four standard errors are 0.035: only the Equipment is found, as 1 in 9, and not the changeling.
    tutor = Tutor(frozenset(), TutorDestination.HAND, subtypes=frozenset({"equipment"}))
    assert fetched_share(tutor, sword, shifter) == pytest.approx(1 / 9, abs=0.035)
    elf = replace(shifter, subtypes=frozenset({"elf"}))
    tutor = Tutor(frozenset(), TutorDestination.HAND, subtypes=frozenset({"goblin", "equipment"}))
    assert fetched_share(tutor, sword, elf) == pytest.approx(1 / 9, abs=0.035)
    assert fetched_share(tutor, sword, shifter) == pytest.approx(2 / 9, abs=0.035)


def test_a_tutor_to_the_hand_fetches_the_piece_it_can_cast_before_a_dearer_one() -> None:
    first = make("First", mv=1, kinds=CREATURE, combo=True)
    second = make("Second", mv=1, kinds=CREATURE, combo=True)
    dear = make("Dear", mv=9, kinds=CREATURE, combo=True)
    cheap = make("Cheap", mv=1, kinds=CREATURE, combo=True)
    tutor = make("Tutor", mv=1, tutor=Tutor(frozenset({CardKind.CREATURE}), TutorDestination.HAND))
    # Two combos of a commander-free piece each: the dear one first in the deck's order, the cheap one after.
    combos = (
        GoldfishCombo("1", ("First", "Dear"), (), (), command_zone=("First",)),
        GoldfishCombo("2", ("Second", "Cheap"), (), (), command_zone=("Second",)),
    )
    slots = [*repeat(FOREST, 10), tutor, dear, cheap]
    report = run(slots, commanders=(first, second), combos=combos, games=2000, seed=3)
    # The nine-drop is never cast by turn five, so a fetch of it is wasted. Taking it whenever it is in the
    # library, as the combos' order alone would, leaves the one-drop fetched in about a fifth of the games
    # (0.21 at this seed, one standard error 0.01); taking the one-drop first, in over a quarter.
    assert sum(1 for o in report.outcomes if o.combo_fetched & 2) / len(report.outcomes) > 0.24


def test_a_combo_piece_that_taps_lands_the_turn_after_it_comes() -> None:
    ritual = make(
        "Ritual", mv=1, pips={"G": 1}, mana=ManaAbility(3, frozenset("G"), delayed=False, ritual=True)
    )
    druid = replace(make("Druid", mv=2, kinds=CREATURE, combo=True), tap_ability=True)
    oracle = make("Oracle", mv=1, kinds=frozenset({CardKind.INSTANT}), combo=True)
    slots = [*repeat(FOREST, 4), ritual, druid, oracle]
    # Turn one's Forest and ritual pay for both, but the druid's tap ability waits for turn two.
    assert only_outcome(run(slots, combos=(combo_of(druid, oracle),))).combo_turns == (2,)


def test_a_treasure_made_on_entering_waits_in_play_for_a_later_turn() -> None:
    barbarian = replace(make("Barbarian", mv=1, kinds=CREATURE), treasures_on_entry=1)
    piece = make("Piece", mv=3, kinds=CREATURE, combo=True)
    # Turn one's Forest pays for the barbarian; turn two's two Forests and the Treasure pay for the piece.
    slots = [FOREST, FOREST, barbarian, piece]
    assert only_outcome(run(slots, combos=(combo_of(piece),))).combo_turns == (2,)


def test_a_creature_attacks_for_its_treasure_only_once_it_is_no_longer_summoning_sick() -> None:
    raider = replace(make("Raider", mv=1, kinds=CREATURE), treasures_on_attack=1)
    piece = make("Piece", mv=4, kinds=CREATURE, combo=True)
    # Cast on turn one, it attacks on turns two and three: two Forests and two Treasures pay on turn three.
    slots = [FOREST, FOREST, raider, piece]
    assert only_outcome(run(slots, combos=(combo_of(piece),))).combo_turns == (3,)


def hulk_landed(piece_value: int, *, outlet: bool) -> list[tuple[int, ...]]:
    """The combo turns of a deck whose commanders are a one-mana Protean Hulk and a one-mana creature, a
    free sacrifice outlet or not, and whose library holds seven Forests, four tutors that find nothing and the
    combo's one piece, a creature too dear to cast by turn five."""
    hulk = replace(
        make("Hulk", mv=1, kinds=CREATURE),
        dies_tutor=Tutor(frozenset({CardKind.CREATURE}), TutorDestination.BATTLEFIELD, total_mana_value=6),
    )
    seer = replace(make("Seer", mv=1, kinds=CREATURE), free_outlet=outlet)
    piece = make("Piece", mv=piece_value, kinds=CREATURE, combo=True)
    junk = make("Junk", mv=9, tutor=Tutor(frozenset({CardKind.INSTANT}), TutorDestination.HAND))
    slots = [*repeat(FOREST, 7), piece, *repeat(junk, 4)]
    report = run(slots, commanders=(hulk, seer), combos=(combo_of(piece),), games=3000)
    return [o.combo_turns for o in report.outcomes]


def test_a_creature_that_searches_as_it_dies_is_sacrificed_to_a_free_outlet_for_its_search() -> None:
    # The second commander comes on turn two, when the Hulk dies and fetches the piece if it is still among
    # the three cards of twelve left in the library: a quarter of the games (all but 1 in 792 hands kept).
    # Four standard errors are 0.032.
    turns = hulk_landed(6, outlet=True)
    assert set(turns) == {(0,), (2,)}
    assert turns.count((2,)) / len(turns) == pytest.approx(0.25, abs=0.032)
    assert set(hulk_landed(6, outlet=False)) == {(0,)}
    # A piece dearer than the total is not found.
    assert set(hulk_landed(7, outlet=True)) == {(0,)}


def test_treasures_from_a_dwarf_attacking_pay_for_a_search_that_sacrifices_them() -> None:
    finds = Tutor(frozenset({CardKind.ARTIFACT}), TutorDestination.BATTLEFIELD)
    magda = replace(
        make("Magda", mv=1, kinds=CREATURE, combo=True),
        subtypes=frozenset({"dwarf"}),
        treasure_on_tap=frozenset({"dwarf"}),
        activated_tutor=ActivatedTutor(finds, treasures=1),
    )
    relic = make("Relic", mv=1, kinds=ARTIFACT, combo=True)
    slots = [*repeat(FOREST, 3), relic, *repeat(VANILLA, 6)]
    report = run(slots, commanders=(magda,), combos=(combo_of(magda, relic),), bracket=None, games=3000)
    ways = report.ways_to_win
    assert ways is not None
    # Magda comes on turn one and attacks on turn two, which takes the Relic from the library when it is the
    # tenth card, the one turn two leaves there: 1 in 10, among the hands kept for two Forests or more
    # (11/12 of those with the Relic tenth, 49/60 of all), so 11/98. Four standard errors are 0.023.
    assert ways.combos[0].fetched_share == pytest.approx(11 / 98, abs=0.025)


def test_a_finisher_is_held_and_its_turn_is_the_first_it_could_be_cast() -> None:
    finisher = make("Overrun", mv=3, pips={"G": 1}, finisher=True)
    report = run([*repeat(FOREST, 4), finisher, VANILLA, VANILLA])
    ways = report.ways_to_win
    assert ways is not None
    assert ways.finishers[0].name == "Overrun"
    assert ways.finishers[0].castable_by == (0.0, 0.0, 1.0, 1.0, 1.0)
    assert ways.faster_than_bracket == ()


def test_a_finisher_is_never_cast_so_it_stays_to_be_found_by_a_tutor() -> None:
    finisher = make("Overrun", mv=1, finisher=True)
    report = run([*repeat(FOREST, 6), finisher])
    # Cast on turn one it would leave the hand; still castable every later turn proves it is held.
    ways = report.ways_to_win
    assert ways is not None
    assert ways.finishers[0].castable_by == (1.0,) * 5
    assert only_outcome(report).finisher_turns == (1,)


def test_a_finisher_with_x_in_its_cost_needs_the_mana_for_ten_more(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(goldfish, "FINISHER_X", 3)
    finisher = make("Craterhoof", mv=1, finisher=True, x_cost=True)
    report = run([*repeat(FOREST, 6), finisher])
    ways = report.ways_to_win
    assert ways is not None
    assert ways.finishers[0].castable_by == (0.0, 0.0, 0.0, 1.0, 1.0)


def test_a_tutor_goes_for_a_finisher_when_no_combo_piece_is_missing() -> None:
    finisher = make("Overrun", mv=6, finisher=True)
    tutor = make("Tutor", mv=1, tutor=Tutor(frozenset({CardKind.SORCERY}), TutorDestination.HAND))
    # Nine cards: the one left in the library after turn one's draw is the finisher in 1 game of 9.
    slots = [*repeat(FOREST, 5), tutor, finisher, VANILLA, VANILLA]
    report = run(slots, games=500, seed=2)
    assert any(o.searched for o in report.outcomes)


def typed_search(*land_types: str) -> GoldfishCard:
    return make(
        "Search",
        mv=2,
        land_ramp=LandRamp(
            1, basic_only=False, destination=LandDestination.UNTAPPED, land_types=frozenset(land_types)
        ),
    )


def test_a_typed_land_search_fetches_only_lands_of_its_types() -> None:
    tomb = land("Tomb", "C", amount=2)
    report = run([typed_search("forest"), *repeat(tomb, 49)], games=300, seed=3)
    # No Forest to find: every game plays a Tomb a turn for two mana, whether or not the search is cast.
    assert all(o.mana == (2, 4, 6, 8, 10) for o in report.outcomes)
    assert any(o.searched for o in report.outcomes)


def test_a_typed_land_search_fetches_a_land_with_one_of_its_types() -> None:
    forest = replace(FOREST, land_types=frozenset({"forest"}))
    report = run([typed_search("plains", "forest"), *repeat(forest, 49)], games=300, seed=3)
    # Cast on turn two with two Forests out, the search puts a third untapped.
    assert any(o.mana[1] == 3 for o in report.outcomes)


# --- the payment solver ----------------------------------------------------------------------------------


W, U, B_, R, G, C = 1, 2, 4, 8, 16, 32


def cost(total: int, **pips: int) -> goldfish.Cost:
    return goldfish.cost_of(total, pips)


def test_a_dual_land_pays_for_either_of_two_colours_but_not_both() -> None:
    wu = cost(2, W=1, U=1)
    assert goldfish.can_pay([W | U, W], wu)
    assert goldfish.can_pay([W | U, U], wu)
    assert not goldfish.can_pay([W, W], wu)
    assert not goldfish.can_pay([W | U], wu)


def test_three_duals_can_pay_three_colours_by_hall_matching() -> None:
    three = cost(3, W=1, U=1, B=1)
    assert goldfish.can_pay([W | U, U | B_, W | B_], three)
    assert goldfish.can_pay([W | U, U | B_, U], three)
    assert not goldfish.can_pay([W | U, W | U, U], three)


def test_generic_mana_comes_out_of_the_units_that_pay_the_fewest_colours() -> None:
    units = [C, G, W | U | B_ | R | G]
    goldfish.pay(units, cost(2, G=1))
    assert units == [W | U | B_ | R | G]


def test_colourless_mana_is_paid_out_before_a_single_colour() -> None:
    units = [W, C, W | G, C, U]
    goldfish.sort_units(units)
    assert units == [C, C, W, U, W | G]
    goldfish.pay(units, cost(2))
    assert units == [W, U, W | G]


def test_generic_costs_are_paid_with_colourless_mana_so_the_colours_are_left_for_a_later_spell() -> None:
    plains = land("Plains", "W")
    wastes = land("Wastes", "C")
    stone = make(
        "Mind Stone", mv=2, kinds=ARTIFACT, mana=ManaAbility(1, frozenset("C"), delayed=False, ritual=False)
    )
    vessel = make(
        "Thought Vessel",
        mv=2,
        kinds=ARTIFACT,
        mana=ManaAbility(1, frozenset("C"), delayed=False, ritual=False),
    )
    boss = make("Boss", mv=2, pips={"W": 2}, kinds=CREATURE)
    slots = [plains, wastes, plains, stone, vessel, VANILLA, VANILLA]
    # Turn two: Wastes pays for Mind Stone with Plains. Turn three has Plains, Wastes, Plains and the stone;
    # the vessel takes the two colourless units and the two Plains pay for the commander.
    assert only_outcome(run(slots, commanders=(boss,))).commander_turns == (3,)


def test_a_coloured_pip_takes_the_least_flexible_unit_that_can_pay_it() -> None:
    units = [U, W | U, W | G]
    goldfish.pay(units, cost(2, U=1, W=1))
    assert units == [W | G]


def test_the_brackets_turns_are_those_of_the_commander_brackets_update() -> None:
    assert BRACKET_TURN == {1: 9, 2: 8, 3: 6, 4: 4}
