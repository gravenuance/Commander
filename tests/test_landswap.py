"""A swap involving a land is allowed only while the land count and every colour's sources hold."""

from collections.abc import Mapping
from dataclasses import replace

import pytest

from commander import landswap
from commander.board import ReplaceOption
from commander.carddb import CardDb, CardMana, ResolvedCard, Role
from commander.landswap import ManaGuard, comparison_lists, mana_guard
from commander.manabase import ManaBase
from commander.models import Deck
from commander.peercounts import PeerCounts
from commander.targets import MAX_TARGETS, CutRules, TargetSource, role_targets, targets_for, with_ideas
from test_manabase import ISLAND, LEADER, PLAINS, by_name, card, deck_of
from test_meta import TOP_CUT_LISTS, db, deck, meta_data, peer
from test_targets import board_card, info, names, table
from test_targets import card as resolved

__all__ = ["db", "deck"]  # fixtures shared with test_meta

FILLER = card("Filler", "{2}", 2, "Artifact")
COUNTERSPELL = card("Counterspell", "{U}{U}", 2, text="Counter target spell.")
WASTES = card("Wastes", type_line="Land", text="{T}: Add {C}.")
GUILDGATE = card(
    "Azorius Guildgate",
    type_line="Land — Gate",
    text="This land enters tapped.\n{T}: Add {W} or {U}.",
    produced="WU",
)
GADGET = card("Gadget", "{2}", 2, "Artifact")
CARDS = by_name(LEADER, PLAINS, ISLAND, FILLER, COUNTERSPELL, WASTES, GUILDGATE, GADGET)


def guard(
    plains: int,
    islands: int,
    spells: int,
    land_range: tuple[float, float] | None = None,
    land_median: float | None = None,
) -> ManaGuard:
    """A white and blue deck of one gold commander, lands and two-mana filler.

    Karsten asks 37.7 lands of it, and 17.2 to 17.7 sources of each colour for the commander, by the count.
    """
    return ManaGuard.of(
        deck_of(("Plains", plains), ("Island", islands), ("Filler", spells)),
        CARDS,
        land_range=land_range,
        land_median=land_median,
    )


def test_a_swap_within_one_land_of_karstens_count_is_allowed() -> None:
    # 37 lands now, Karsten's 37.7; 38 after.
    assert guard(19, 18, 62).allows("Plains", "Filler")
    # 36 lands is 1.7 short, but 37 is 0.7 short.
    assert guard(18, 18, 63).allows("Island", "Filler")
    # Cutting a land for a spell moves it 37 to 36, 1.7 short: refused.
    assert not guard(19, 18, 62).allows("Filler", "Plains")


def test_a_swap_outside_one_land_of_karstens_count_and_the_peer_range_is_refused() -> None:
    # 38 lands is 0.3 over; 39 is 1.3 over.
    assert not guard(19, 19, 61).allows("Plains", "Filler")
    # Peers run 37 to 41 lands: 39 is inside.
    assert guard(19, 19, 61, land_range=(37, 41)).allows("Plains", "Filler")
    # Peers who run fewer lands do not allow it.
    assert not guard(19, 19, 61, land_range=(34, 38)).allows("Plains", "Filler")


def test_a_swap_that_moves_the_land_count_closer_to_karstens_is_allowed_even_when_far_off() -> None:
    # 41 lands is 3.3 over Karsten's 37.7: a land for a spell makes 40, 2.3 over, which is closer.
    over = guard(21, 20, 59)
    assert over.allows("Filler", "Plains")
    # A spell for a land makes 42, further off.
    assert not over.allows("Plains", "Filler")
    # 34 lands is 3.7 under: a spell for a land makes 35, closer; a land for a spell makes 33, further.
    under = guard(17, 17, 66)
    assert under.allows("Plains", "Filler")
    assert not under.allows("Filler", "Plains")


def test_a_land_swapped_for_a_spell_is_refused_when_a_colour_would_lose_the_sources_it_needs() -> None:
    # Eighteen Islands meet the commander's 17.7 at 38 lands; seventeen, with 37 lands, miss 17.2.
    assert not guard(20, 18, 61).allows("Filler", "Island")
    # Nineteen Plains meet 17.2 after a cut.
    assert guard(20, 18, 61).allows("Filler", "Plains")


def test_a_spell_taking_a_lands_place_is_refused_when_it_is_short_of_sources_itself() -> None:
    # Counterspell wants about 28 blue sources of 38 lands; the deck has 18.
    assert not guard(20, 18, 61).allows("Counterspell", "Plains")


def test_a_land_swapped_for_another_keeps_every_colours_sources() -> None:
    base = guard(20, 18, 61)
    # An Island becoming a Plains, or a colourless land, leaves blue 17 sources short of 17.7.
    assert not base.allows("Plains", "Island")
    assert not base.allows("Wastes", "Island")
    # A Plains becoming an Island, a colourless land or a dual keeps white at 19 or 20.
    assert base.allows("Island", "Plains")
    assert base.allows("Wastes", "Plains")
    assert base.allows("Azorius Guildgate", "Plains")


def test_a_land_for_land_swap_leaves_the_count_alone_even_when_it_is_off() -> None:
    # 36 lands is 1.7 under Karsten's count, and no swap of one land for another changes that. Seventeen
    # Islands meet the commander's 16.8; sixteen do not.
    assert guard(19, 17, 63).allows("Azorius Guildgate", "Plains")
    assert not guard(19, 17, 63).allows("Wastes", "Island")


def test_a_swap_involving_a_card_the_data_does_not_know_is_refused() -> None:
    assert not guard(20, 18, 61).allows("Mystery Land", "Plains")


def test_a_deck_already_short_of_sources_is_refused_only_what_makes_it_worse() -> None:
    # Fifteen Islands miss the commander's 17.7 already.
    base = guard(23, 15, 61)
    assert base.allows("Plains", "Island") is False
    assert base.allows("Island", "Plains")


def test_the_peer_lists_of_a_bracket_5_deck_are_its_top_cut_lists(deck: Deck) -> None:
    label, lists = comparison_lists(deck, meta_data(TOP_CUT_LISTS))
    assert label == "top-cut lists"
    assert lists[0] == {"Mind Stone": 1, "Battered Golem": 1, "High-Speed Hoverbike": 1, "Jeska's Will": 1}
    assert len(lists) == 5


def test_other_brackets_compare_with_the_recent_decks_that_say_how_many_basics_they_run(deck: Deck) -> None:
    known = replace(peer(1, ["Battered Golem", "Mountain"]), multiples=(("Mountain", 36),))
    old = replace(peer(2, ["Mountain"]), multiples=None)
    label, lists = comparison_lists(replace(deck, bracket=3), meta_data([], [known, old]))
    assert (label, lists) == ("recent decks", [{"Battered Golem": 1, "Mountain": 36}])
    assert comparison_lists(deck, None) == ("", [])


def test_the_guard_of_a_deck_takes_its_land_range_from_at_least_three_lists_compared(
    deck: Deck, db: CardDb
) -> None:
    runs = [replace(peer(n, ["Battered Golem", "Mountain"]), multiples=(("Mountain", m),)) for n, m in LANDS]
    below_five = replace(deck, bracket=3)
    assert mana_guard(below_five, db, meta_data([], runs)).land_range == (36.0, 38.0)
    assert mana_guard(below_five, db, meta_data([], runs)).land_median == 37.0
    # A range of one or two lists is no range to go by.
    assert mana_guard(below_five, db, meta_data([], runs[:2])).land_range is None
    assert mana_guard(below_five, db, meta_data([], runs[:2])).land_median is None
    assert mana_guard(below_five, db, meta_data([], runs[:1])).land_range is None
    assert mana_guard(deck, db, None).land_range is None
    assert mana_guard(deck, db, None).land_median is None


LANDS = [(1, 36), (2, 38), (3, 37)]


def target_rules(mana: ManaGuard) -> CutRules:
    return CutRules((), None, mana=mana)


def deck_cards() -> list[ResolvedCard]:
    commander = resolved("Leader", categories=("Commander",), type_line="Legendary Creature — Human")
    lands = [
        resolved("Plains", rank=1000, categories=("Land",), type_line="Basic Land — Plains"),
        resolved("Island", rank=3000, categories=("Land",), type_line="Basic Land — Island"),
    ]
    return [commander, *lands, resolved("Filler", rank=500, type_line="Artifact")]


def test_a_refused_land_target_falls_through_to_the_next_candidate() -> None:
    wastes = info("Wastes", type_line="Land")
    mana = guard(20, 18, 61)
    # Without the guard the weakest land, the Island, comes first.
    assert names(role_targets(wastes, deck_cards(), CutRules((), None))) == ["Island", "Plains"]
    # Cutting the Island would leave blue short; the Plains still do.
    assert names(role_targets(wastes, deck_cards(), target_rules(mana))) == ["Plains"]


def test_a_refused_hand_picked_cut_gives_way_to_the_weakest_like_it() -> None:
    wastes = info("Wastes", type_line="Land")
    rules = target_rules(guard(20, 18, 61))
    picked = board_card("Wastes", (ReplaceOption("Island", "worst"),))
    found = targets_for(picked, wastes, deck_cards(), rules)
    assert [(t.name, t.source) for t in found] == [("Plains", TargetSource.ROLE)]
    ok = board_card("Wastes", (ReplaceOption("Plains", "extra"),))
    assert [(t.name, t.source) for t in targets_for(ok, wastes, deck_cards(), rules)] == [
        ("Plains", TargetSource.PICKED)
    ]


def test_ideas_that_are_not_lands_are_not_checked() -> None:
    spell = info("Another Filler", type_line="Artifact")
    rules = target_rules(guard(20, 18, 61))
    assert names(role_targets(spell, deck_cards(), rules)) == ["Filler"]


def test_the_rules_learn_the_mana_of_the_ideas_they_are_given(db: CardDb) -> None:
    base = replace(target_rules(guard(20, 18, 61)), mana=None)
    assert with_ideas(base, [info("Arcane Signet", (Role.RAMP,))], db).mana is None
    mana = guard(20, 18, 61)
    known = with_ideas(target_rules(mana), [info("Mountain", type_line="Basic Land — Mountain")], db).mana
    assert known is not None
    assert "mountain" in known.cards


def test_the_lands_a_deck_could_lose_go_down_to_karstens_count_less_the_tolerance() -> None:
    # Karsten's 37.7 less a tolerance of one is 36.7: 37 lands can lose none, 38 one, 39 two, 41 four.
    assert guard(19, 18, 62).spare_lands() == 0
    assert guard(19, 19, 61).spare_lands() == 1
    assert guard(20, 19, 60).spare_lands() == 2
    assert guard(21, 20, 58).spare_lands() == 4


def test_the_lands_a_deck_could_lose_stop_at_the_peers_median_when_that_is_higher() -> None:
    # 39 lands. A median of 37 lands is above 36.7 and still leaves two; 38 leaves one; 39 none.
    assert guard(20, 19, 60, land_median=37.0).spare_lands() == 2
    assert guard(20, 19, 60, land_median=38.0).spare_lands() == 1
    assert guard(20, 19, 60, land_median=38.5).spare_lands() == 0
    assert guard(20, 19, 60, land_median=39.0).spare_lands() == 0
    # A median under Karsten's count less the tolerance lowers nothing, and a deck under it has none to spare.
    assert guard(20, 19, 60, land_median=34.0).spare_lands() == 2
    assert guard(19, 18, 62, land_median=30.0).spare_lands() == 0


def test_the_peer_ranges_low_end_is_no_floor_for_spare_lands() -> None:
    # Peers running 34 to 41 lands once let 38 lose four; now only Karsten's floor counts.
    assert guard(19, 19, 61, land_range=(34, 41)).spare_lands() == 1


def test_the_sources_a_colour_could_lose_are_what_its_most_demanding_spell_leaves() -> None:
    # 39 lands scale the commander's 18 sources to 17.1 and add one for its two colours: 18.1.
    base = guard(20, 19, 60)
    assert base.spare_sources("Plains") == pytest.approx(1.9)
    assert base.spare_sources("Island") == pytest.approx(0.9)
    # A land making both colours has the least of them to spare.
    assert base.spare_sources("Azorius Guildgate") == pytest.approx(0.9)
    # A land making no colour, or one the data does not know, has none to speak of.
    assert base.spare_sources("Wastes") is None
    assert base.spare_sources("Mystery Land") is None


GADGET_IDEA = info("Gadget", type_line="Artifact")
NONBASIC = resolved("Azorius Guildgate", rank=2000, categories=("Land",), type_line="Land — Gate")


def land_rules(mana: ManaGuard, played: dict[str, float], idea: str = "gadget") -> CutRules:
    """The idea at 90% of the lists and the named cards as given; unnamed ones no list plays."""
    return CutRules((), table({idea: 0.9, **played}), mana=mana, peers=PeerCounts({}, {}))


def test_a_spare_land_may_go_for_a_spell_and_only_the_basic_with_most_sources_to_spare_is_offered() -> None:
    # 39 lands can lose two. White has 1.9 sources to spare, blue 0.9: of the two basics, the Plains.
    found = role_targets(GADGET_IDEA, deck_cards(), land_rules(guard(20, 19, 60), {"filler": 0.3}))
    assert names(found) == ["Filler", "Plains"]
    assert found[1].reason.startswith("spare land: the mana base holds with 2 fewer lands; in 0% of")
    flipped = role_targets(GADGET_IDEA, deck_cards(), land_rules(guard(19, 20, 60), {"filler": 0.3}))
    assert names(flipped) == ["Filler", "Island"]


def test_a_land_target_scores_per_spare_land_against_the_other_candidates() -> None:
    # A barely played idea needs a cut at most half as played, which keeps Filler's score low enough for a
    # basic to pass it: Filler 0.10 + 0.1 - 0.04 = 0.16. Plains with two spare lands: 0.06, with six: 0.18.
    played = {"gadget": 0.1, "filler": 0.04}
    two = land_rules(guard(20, 19, 60), played)
    assert names(role_targets(GADGET_IDEA, deck_cards(), two)) == ["Filler", "Plains"]
    six = land_rules(guard(22, 21, 56), played)
    assert names(role_targets(GADGET_IDEA, deck_cards(), six)) == ["Plains", "Filler"]


def test_a_single_spare_land_reads_in_the_singular_and_a_card_as_played_as_the_idea_is_no_target() -> None:
    found = role_targets(GADGET_IDEA, deck_cards(), land_rules(guard(20, 18, 61), {"filler": 0.9}))
    assert names(found) == ["Plains"]
    assert found[0].reason.startswith("spare land: the mana base holds with 1 fewer land;")


def test_a_nonbasic_land_less_played_than_the_idea_adds_its_gap_to_the_score() -> None:
    cards = [*deck_cards(), NONBASIC]
    mana = ManaGuard.of(
        deck_of(("Plains", 20), ("Island", 18), ("Azorius Guildgate", 1), ("Filler", 60)), CARDS
    )
    # 39 lands can lose two. Guildgate at 20%: 0.06 + 0.7, ahead of Filler's 0.10 + 0.6 and the Plains' 0.06.
    found = role_targets(GADGET_IDEA, cards, land_rules(mana, {"filler": 0.3, "azorius guildgate": 0.2}))
    assert names(found) == ["Azorius Guildgate", "Filler", "Plains"]
    assert found[0].reason.startswith("spare land: the mana base holds with 2 fewer lands;")
    # As played as the idea it is not offered, though a basic still is.
    level = land_rules(mana, {"filler": 0.3, "azorius guildgate": 0.9})
    assert names(role_targets(GADGET_IDEA, cards, level)) == ["Filler", "Plains"]


@pytest.mark.parametrize(
    ("roles", "categories", "themes"),
    [
        ((Role.RECURSION,), ("Land",), frozenset[str]()),
        ((Role.EXTRA_TURN,), ("Land",), frozenset[str]()),
        ((), ("Land", "Ramp"), frozenset[str]()),
        ((), ("Land",), frozenset({"Gates"})),
    ],
    ids=["role tag", "role outside the analysed ones", "role by category", "theme"],
)
def test_a_nonbasic_land_with_a_role_or_a_theme_is_never_a_spare_land(
    roles: tuple[Role, ...], categories: tuple[str, ...], themes: frozenset[str]
) -> None:
    utility = resolved("Azorius Guildgate", roles, rank=2000, categories=categories, type_line="Land — Gate")
    mana = ManaGuard.of(
        deck_of(("Plains", 20), ("Island", 18), ("Azorius Guildgate", 1), ("Filler", 60)), CARDS
    )
    rules = replace(
        land_rules(mana, {"filler": 0.3, "azorius guildgate": 0.2}),
        themes={"azorius guildgate": themes},
    )
    assert names(role_targets(GADGET_IDEA, [*deck_cards(), utility], rules)) == ["Filler", "Plains"]


VESUVA = "You may have this land enter tapped as a copy of any land on the battlefield."
COMMAND_TOWER = "{T}: Add one mana of any color in your commander's color identity."
FIELD_OF_THE_DEAD = (
    "This land enters tapped.\n{T}: Add {C}.\nWhenever this land or another land you control enters, if you "
    "control seven or more lands with different names, create a 2/2 black Zombie creature token."
)


@pytest.mark.parametrize(
    ("name", "text", "produced", "offered"),
    [
        ("Vesuva", VESUVA, "", False),
        ("Field of the Dead", FIELD_OF_THE_DEAD, "C", False),
        ("Command Tower", COMMAND_TOWER, "WUBRG", True),
    ],
)
def test_only_a_land_that_does_nothing_but_make_mana_is_a_spare_land(
    name: str, text: str, produced: str, *, offered: bool
) -> None:
    land = card(name, type_line="Land", text=text, produced=produced)
    mana = ManaGuard.of(
        deck_of(("Plains", 20), ("Island", 18), (name, 1), ("Filler", 60)), {**CARDS, name.casefold(): land}
    )
    cut = resolved(name, rank=2000, categories=("Land",), type_line="Land")
    rules = land_rules(mana, {"filler": 0.3, name.casefold(): 0.2})
    assert (name in names(role_targets(GADGET_IDEA, [*deck_cards(), cut], rules))) is offered


def test_no_land_is_offered_when_the_mana_base_has_none_to_spare() -> None:
    # 37 lands cannot lose one.
    rules = land_rules(guard(19, 18, 62), {"filler": 0.3})
    assert names(role_targets(GADGET_IDEA, deck_cards(), rules)) == ["Filler"]


def test_no_land_is_offered_when_the_guard_refuses_the_swap_itself() -> None:
    # 38 lands can lose one, but Counterspell wants about 28 blue sources and the deck has 18.
    mana = guard(20, 18, 61)
    assert mana.spare_lands() == 1
    rules = land_rules(mana, {"filler": 0.3}, idea="counterspell")
    assert names(role_targets(info("Counterspell", type_line="Instant"), deck_cards(), rules)) == ["Filler"]


def test_a_land_idea_never_gets_the_spare_land_cuts_of_a_spell() -> None:
    rules = land_rules(guard(20, 19, 60), {"plains": 0.1, "island": 0.2}, idea="wastes")
    # Wastes makes no colour, so only the Plains can go: blue is down to 18 of the 18.1 it needs.
    assert names(role_targets(info("Wastes", type_line="Land"), deck_cards(), rules)) == ["Plains"]


def test_land_targets_need_the_shares_the_peers_and_the_mana_data() -> None:
    mana = guard(20, 19, 60)
    full = land_rules(mana, {"filler": 0.3})
    assert names(role_targets(GADGET_IDEA, deck_cards(), full)) == ["Filler", "Plains"]
    for lacking in (replace(full, shares=None), replace(full, peers=None), replace(full, mana=None)):
        assert names(role_targets(GADGET_IDEA, deck_cards(), lacking)) == ["Filler"]
    # A guard that knows none of the cards refuses every swap of a land.
    assert names(role_targets(GADGET_IDEA, deck_cards(), replace(full, mana=replace(mana, cards={})))) == [
        "Filler"
    ]


def test_a_kept_land_is_never_a_spare_land_cut() -> None:
    rules = replace(land_rules(guard(20, 19, 60), {"filler": 0.3}), protected={"plains"})
    assert names(role_targets(GADGET_IDEA, deck_cards(), rules)) == ["Filler", "Island"]


def test_at_most_four_targets_are_offered_by_score_with_the_lands_among_them() -> None:
    extras = [resolved(f"Extra {n}", rank=500 + n, type_line="Artifact") for n in range(1, 5)]
    found = role_targets(
        GADGET_IDEA, [*deck_cards(), *extras], land_rules(guard(20, 19, 60), {"filler": 0.4})
    )
    # Each extra scores 0.10 + 0.9; Filler 0.10 + 0.5 and the Plains 0.06 fall behind.
    assert MAX_TARGETS == 4
    assert sorted(names(found)) == ["Extra 1", "Extra 2", "Extra 3", "Extra 4"]


def test_a_basic_land_needs_no_gap_to_the_idea_so_a_well_played_basic_is_still_offered() -> None:
    # Basics are in nearly every list; the Plains at 95% is more played than the idea at 90%.
    rules = land_rules(guard(20, 19, 60), {"filler": 0.3, "plains": 0.95})
    assert names(role_targets(GADGET_IDEA, deck_cards(), rules)) == ["Filler", "Plains"]


def test_only_the_basic_of_the_colour_with_most_to_spare_is_considered_so_a_refusal_offers_no_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_plains(self: ManaGuard, add: str, cut: str) -> bool:  # noqa: ARG001 — replaces a method
        return cut != "Plains"

    monkeypatch.setattr(ManaGuard, "allows", refuse_plains)
    # White has the most to spare (1.9 against 0.9), the guard refuses its Plains, and the Island stays out.
    rules = land_rules(guard(20, 19, 60), {"filler": 0.3})
    assert names(role_targets(GADGET_IDEA, deck_cards(), rules)) == ["Filler"]


def test_a_land_count_with_a_part_land_counts_the_whole_lands_above_the_floor() -> None:
    # 38 lands and a land/spell card as 0.38 of a land make 38.38; the floor is Karsten's 37.7 less one.
    base = guard(19, 19, 61)

    def spare(lands: float) -> int:
        return replace(base, lands=replace(base.lands, lands=lands)).spare_lands()

    assert spare(38.38) == 1
    assert spare(38.69) == 1
    assert spare(38.7) == 2
    assert spare(36.69) == 0


def test_a_basic_land_filed_as_two_entries_counts_all_its_copies_in_a_swap() -> None:
    # Ten and nine Plains are the same 19 Plains as one entry: both swaps are allowed alike.
    split = ManaGuard.of(deck_of(("Plains", 10), ("Plains", 9), ("Island", 18), ("Filler", 62)), CARDS)
    assert split.allows("Plains", "Filler")
    assert split.allows("Island", "Filler")
    # Cutting one of the 19 Plains for a spell still leaves 36 lands, 1.7 short: refused as with one entry.
    assert not split.allows("Filler", "Plains")


def count_analyses(monkeypatch: pytest.MonkeyPatch) -> list[Deck]:
    """The decks `analyse_mana` is run on from now on, as the guard judges a swap."""
    runs: list[Deck] = []
    analysed = landswap.analyse_mana

    def counting(deck: Deck, cards: Mapping[str, CardMana], *, alternative_cost: frozenset[str]) -> ManaBase:
        runs.append(deck)
        return analysed(deck, cards, alternative_cost=alternative_cost)

    monkeypatch.setattr(landswap, "analyse_mana", counting)
    return runs


def test_a_swap_asked_again_gets_the_same_answer_without_analysing_the_mana_base_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = guard(20, 18, 61)
    runs = count_analyses(monkeypatch)
    answers = [base.allows("Island", "Plains"), base.allows("Plains", "Island")]
    assert answers == [True, False]
    assert len(runs) == 2
    # Both an allowed and a refused swap are remembered; a pair asked the other way round is another swap.
    assert [base.allows("Island", "Plains"), base.allows("Plains", "Island")] == answers
    assert len(runs) == 2
    assert base.allows("Filler", "Plains")
    assert len(runs) == 3


def test_a_guard_given_more_cards_judges_for_itself_and_knows_the_new_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = ManaGuard.of(
        deck_of(("Plains", 20), ("Island", 18), ("Filler", 61)), by_name(LEADER, PLAINS, ISLAND, FILLER)
    )
    runs = count_analyses(monkeypatch)
    assert base.allows("Island", "Plains")
    # A card the guard has no mana data for is refused, and the refusal is not remembered.
    assert not base.allows("Gadget", "Filler")
    wider = base.with_cards(by_name(GADGET))
    assert wider.allows("Gadget", "Filler")
    assert len(runs) == 2
    # The wider guard has its own answers: it analyses the swap the first guard had already judged.
    assert wider.allows("Island", "Plains")
    assert len(runs) == 3
    assert not base.allows("Gadget", "Filler")


def test_a_basic_land_filed_as_two_entries_gets_the_same_answers_asked_twice() -> None:
    split = ManaGuard.of(deck_of(("Plains", 10), ("Plains", 9), ("Island", 18), ("Filler", 62)), CARDS)
    swaps = [("Plains", "Filler"), ("Island", "Filler"), ("Filler", "Plains")]
    assert [split.allows(add, cut) for add, cut in swaps * 2] == [True, True, False] * 2
