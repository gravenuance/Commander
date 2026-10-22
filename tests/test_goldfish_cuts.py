"""The goldfish's tie-break between cut targets (ADR 0056): which targets tie on score, and how the figures
their swaps move put them in order."""

import pytest

from commander.goldfish import FigureChange, FigureKind, GoldfishComparison
from commander.goldfish_cuts import TIE_MARGIN, RankedCut, cuts_json, rank_cuts, tie_groups
from commander.targets import Target, TargetSource
from test_goldfish import FOREST, repeat, run

# Bracket 3, so the headline figures are read on turn 6.
BASE = run(repeat(FOREST, 7), bracket=3, games=2)


def change(
    kind: FigureKind, subject: str, turn: int | None, before: float, after: float, *, moved: bool = True
) -> FigureChange:
    return FigureChange(kind, subject, turn, before, after, moved)


SCREW_WORSE = change(FigureKind.COLOUR_SCREW, "", None, 0.1, 0.3)
SCREW_BETTER = change(FigureKind.COLOUR_SCREW, "", None, 0.3, 0.1)
MANA_WORSE = change(FigureKind.MANA_WORST_TENTH, "", 6, 5, 3)
MANA_BETTER = change(FigureKind.MANA_WORST_TENTH, "", 6, 3, 5)
CAST_BETTER = change(FigureKind.COMMANDER_CAST_BY, "Magda", 6, 0.5, 0.7)
CAST_WORSE = change(FigureKind.COMMANDER_CAST_BY, "Magda", 6, 0.7, 0.5)


def target(name: str, score: float | None) -> Target:
    return Target(
        name, f"{name} is cut", TargetSource.ROLE if score is not None else TargetSource.PICKED, score
    )


class Plays:
    """Compares an idea with a target by the figures set for the target, counting the targets played."""

    def __init__(self, figures: dict[str, list[FigureChange]]) -> None:
        self.figures = figures
        self.played: list[str] = []

    def __call__(self, cut: Target) -> GoldfishComparison:
        self.played.append(cut.name)
        return GoldfishComparison(BASE, BASE, tuple(self.figures.get(cut.name, [])), ())


def order(ranked: list[RankedCut]) -> list[str]:
    return [r.target.name for r in ranked]


# --- which targets tie -------------------------------------------------------------------------------------


def scores(*values: float | None) -> list[Target]:
    return [target(f"T{i}", value) for i, value in enumerate(values)]


def test_targets_within_the_margin_of_the_first_of_a_run_tie_and_a_farther_one_starts_a_new_run() -> None:
    assert TIE_MARGIN == 0.10
    assert tie_groups(scores(0.50, 0.45, 0.41, 0.39)) == [range(3), range(3, 4)]


def test_two_scores_exactly_the_margin_apart_tie_despite_float_rounding() -> None:
    # 0.4 - 0.3 is 0.10000000000000003 in floats.
    assert tie_groups(scores(0.4, 0.3)) == [range(2)]
    assert tie_groups(scores(0.4, 0.29)) == [range(1), range(1, 2)]


def test_a_run_is_measured_from_its_first_target_so_a_chain_of_near_scores_does_not_all_tie() -> None:
    assert tie_groups(scores(0.50, 0.42, 0.34)) == [range(2), range(2, 3)]


def test_a_target_without_a_score_ties_with_none() -> None:
    assert tie_groups(scores(None, None)) == [range(1), range(1, 2)]
    assert tie_groups(scores(0.5, None, 0.5)) == [range(1), range(1, 2), range(2, 3)]
    assert tie_groups([]) == []


# --- the order within a run --------------------------------------------------------------------------------


def test_the_target_whose_swap_makes_fewer_figures_worse_goes_first_and_names_the_figure() -> None:
    targets = [target("Island", 0.50), target("Forest", 0.45)]
    ranked = rank_cuts(targets, Plays({"Island": [SCREW_WORSE]}))
    assert order(ranked) == ["Forest", "Island"]
    assert [(r.moved_up, r.why) for r in ranked] == [
        (True, "Colour screw is worse with Island"),
        (False, None),
    ]


def test_of_two_that_worsen_as_many_the_one_that_betters_more_goes_first() -> None:
    targets = [target("Island", 0.50), target("Forest", 0.45)]
    ranked = rank_cuts(targets, Plays({"Island": [MANA_BETTER], "Forest": [MANA_BETTER, CAST_BETTER]}))
    assert order(ranked) == ["Forest", "Island"]
    assert ranked[0].why == "Commander cast by turn 6 is better with Forest"


def test_fewer_worse_figures_beat_more_better_ones() -> None:
    targets = [target("Island", 0.50), target("Forest", 0.45)]
    figures = {"Island": [MANA_BETTER, CAST_BETTER, SCREW_WORSE], "Forest": []}
    ranked = rank_cuts(targets, Plays(figures))
    assert order(ranked) == ["Forest", "Island"]
    assert ranked[0].why == "Colour screw is worse with Island"


def test_targets_that_move_the_same_figures_keep_the_scores_order_and_none_is_marked() -> None:
    targets = [target("Island", 0.50), target("Forest", 0.45), target("Swamp", 0.44)]
    figures = {name: [MANA_BETTER, SCREW_WORSE] for name in ("Island", "Forest", "Swamp")}
    ranked = rank_cuts(targets, Plays(figures))
    assert order(ranked) == ["Island", "Forest", "Swamp"]
    assert [(r.moved_up, r.why) for r in ranked] == [(False, None)] * 3


def test_a_target_that_moved_nothing_is_not_worse_than_one_that_made_a_figure_better() -> None:
    targets = [target("Island", 0.50), target("Forest", 0.45)]
    ranked = rank_cuts(targets, Plays({"Island": [], "Forest": [SCREW_BETTER]}))
    assert order(ranked) == ["Forest", "Island"]
    assert ranked[0].why == "Colour screw is better with Forest"


def test_three_in_a_run_are_sorted_and_only_the_one_that_went_up_past_the_others_is_marked() -> None:
    targets = [target("Island", 0.50), target("Swamp", 0.48), target("Forest", 0.45)]
    figures = {"Island": [CAST_WORSE], "Swamp": [MANA_WORSE, SCREW_WORSE], "Forest": []}
    ranked = rank_cuts(targets, Plays(figures))
    assert order(ranked) == ["Forest", "Island", "Swamp"]
    assert [r.moved_up for r in ranked] == [True, False, False]
    # It passed Island, the first the score ranked above it.
    assert ranked[0].why == "Commander cast by turn 6 is worse with Island"


def test_a_target_that_moved_down_is_not_marked_and_one_that_stayed_put_is_not_either() -> None:
    targets = [target("Island", 0.50), target("Swamp", 0.48), target("Forest", 0.45)]
    figures = {"Island": [SCREW_WORSE], "Swamp": [SCREW_WORSE], "Forest": [SCREW_WORSE]}
    figures["Swamp"] = []
    ranked = rank_cuts(targets, Plays(figures))
    assert order(ranked) == ["Swamp", "Island", "Forest"]
    assert [r.moved_up for r in ranked] == [True, False, False]


def test_only_figures_that_moved_at_the_headline_turn_count() -> None:
    quiet = change(FigureKind.COLOUR_SCREW, "", None, 0.1, 0.3, moved=False)
    early = change(FigureKind.MANA_WORST_TENTH, "", 5, 5, 3)
    median = change(FigureKind.MANA_MEDIAN, "", 6, 5, 3)
    fetched = change(FigureKind.COMBO_FETCHED, "1-2", None, 0.5, 0.2)
    combo = change(FigureKind.COMBO_LANDED_BY, "1-2", 6, 0.5, 0.2)
    targets = [target("Island", 0.50), target("Forest", 0.45)]
    ranked = rank_cuts(targets, Plays({"Island": [quiet, early, median, fetched, combo]}))
    assert order(ranked) == ["Island", "Forest"]
    assert ranked[0].verdict is not None
    assert (ranked[0].verdict.worse, ranked[0].verdict.better) == ((), ())


# --- what is played and what is left alone -----------------------------------------------------------------


def test_only_targets_in_a_run_of_two_or_more_are_played() -> None:
    targets = [target("Island", 0.90), target("Swamp", 0.50), target("Forest", 0.45), target("Plains", 0.20)]
    plays = Plays({"Swamp": [SCREW_WORSE]})
    ranked = rank_cuts(targets, plays)
    assert plays.played == ["Swamp", "Forest"]
    assert order(ranked) == ["Island", "Forest", "Swamp", "Plains"]
    assert [r.verdict is None for r in ranked] == [True, False, False, True]


def test_a_target_far_below_never_passes_one_above_whatever_its_figures() -> None:
    targets = [target("Island", 0.50), target("Forest", 0.30)]
    ranked = rank_cuts(targets, Plays({"Island": [SCREW_WORSE], "Forest": [MANA_BETTER]}), everything=True)
    assert order(ranked) == ["Island", "Forest"]
    assert [r.moved_up for r in ranked] == [False, False]


def test_everything_plays_every_target_for_its_figures_and_changes_no_order() -> None:
    targets = [target("Island", 0.90), target("Forest", 0.45)]
    plays = Plays({"Island": [SCREW_WORSE]})
    ranked = rank_cuts(targets, plays, everything=True)
    assert plays.played == ["Island", "Forest"]
    assert order(ranked) == ["Island", "Forest"]
    assert ranked[0].verdict is not None
    assert ranked[0].verdict.worse == (SCREW_WORSE,)


def test_picked_targets_are_neither_played_nor_moved() -> None:
    targets = [target("Island", None), target("Forest", None)]
    plays = Plays({"Island": [SCREW_WORSE]})
    assert order(rank_cuts(targets, plays)) == ["Island", "Forest"]
    assert plays.played == []


def test_no_targets_give_no_order() -> None:
    assert rank_cuts([], Plays({})) == []


def test_the_page_gets_the_targets_in_order_with_whether_each_moved_up_and_why() -> None:
    targets = [target("Island", 0.50), target("Forest", 0.45)]
    document = cuts_json(rank_cuts(targets, Plays({"Island": [SCREW_WORSE]})))
    assert document == {
        "cuts": [
            {"name": "Forest", "up": True, "why": "Colour screw is worse with Island"},
            {"name": "Island", "up": False, "why": None},
        ]
    }


@pytest.mark.parametrize("figures", [{}, {"Island": [SCREW_WORSE], "Forest": [SCREW_WORSE]}])
def test_an_idea_that_changes_nothing_between_its_targets_leaves_the_scores_order(
    figures: dict[str, list[FigureChange]],
) -> None:
    ranked = rank_cuts([target("Island", 0.50), target("Forest", 0.45)], Plays(figures))
    assert order(ranked) == ["Island", "Forest"]
