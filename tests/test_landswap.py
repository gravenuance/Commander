"""A swap involving a land is allowed only while the land count and every colour's sources hold."""

from dataclasses import replace

from commander.board import ReplaceOption
from commander.carddb import CardDb, ResolvedCard, Role
from commander.landswap import ManaGuard, comparison_lists, mana_guard
from commander.models import Deck
from commander.targets import CutRules, TargetSource, role_targets, targets_for, with_ideas
from test_manabase import ISLAND, LEADER, PLAINS, by_name, card, deck_of
from test_meta import TOP_CUT_LISTS, db, deck, meta_data, peer
from test_targets import board_card, info, names
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
CARDS = by_name(LEADER, PLAINS, ISLAND, FILLER, COUNTERSPELL, WASTES, GUILDGATE)


def guard(plains: int, islands: int, spells: int, land_range: tuple[float, float] | None = None) -> ManaGuard:
    """A white and blue deck of one gold commander, lands and two-mana filler.

    Karsten asks 37.7 lands of it, and 17.2 to 17.7 sources of each colour for the commander, by the count.
    """
    return ManaGuard.of(
        deck_of(("Plains", plains), ("Island", islands), ("Filler", spells)), CARDS, land_range=land_range
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
    # A range of one or two lists is no range to go by.
    assert mana_guard(below_five, db, meta_data([], runs[:2])).land_range is None
    assert mana_guard(below_five, db, meta_data([], runs[:1])).land_range is None
    assert mana_guard(deck, db, None).land_range is None


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
