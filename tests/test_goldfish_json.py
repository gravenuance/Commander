"""The goldfish's figures as JSON and as words (ADR 0054): the labels the page shows as they are."""

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from commander.carddb import CardDb
from commander.deckprofile import Profile
from commander.goldfish import (
    ComboFigures,
    CommanderFigures,
    FigureChange,
    FigureKind,
    FinisherFigures,
    GoldfishReport,
    WaysToWin,
)
from commander.goldfish_cards import GoldfishCombo
from commander.goldfish_json import (
    Json,
    change_json,
    figure_label,
    goldfish_report_json,
    keep_rule_text,
    value_text,
)
from commander.goldfish_load import load_goldfish
from commander.goldfish_run import GoldfishRun, run
from commander.models import Deck
from test_meta import NOW, db, deck

__all__ = ["db", "deck"]  # fixtures shared with test_meta

COMBO = GoldfishCombo(
    "1-2", ("Thassa's Oracle", "Demonic Consultation"), ("a creature with power 5",), ("Win the game",)
)


@pytest.fixture
def played(deck: Deck, db: CardDb, tmp_path: Path) -> GoldfishRun:
    return run(load_goldfish(deck, db, Profile(), tmp_path, now=lambda: NOW), games=50)


def change(
    kind: FigureKind,
    subject: str = "",
    turn: int | None = None,
    *,
    base: float | None = 0.25,
    swapped: float | None = 0.5,
) -> FigureChange:
    return FigureChange(kind, subject, turn, base, swapped, moved=True)


def with_ways(report: GoldfishReport) -> GoldfishReport:
    """The report as if it were bracket 4 with a combo landing by turn 4 in 20% of games, and a finisher."""
    combo = ComboFigures("1-2", COMBO.pieces, (0.0, 0.1, 0.2, 0.4, 0.5), 0.75)
    return replace(
        report,
        bracket=4,
        keep_bracket=4,
        turns=(2, 3, 4, 5, 6),
        ways_to_win=WaysToWin((combo,), (FinisherFigures("Overrun", (0.0, 0.0, 0.1, 0.2, 0.3)),), ("1-2",)),
    )


@pytest.mark.parametrize(
    ("bracket", "bracketed", "expected"),
    [
        (
            5,
            5,
            "bracket 5 rule: keeps seven cards with a land, 2 mana sources in all (fast mana counts) and",
        ),
        (
            4,
            4,
            "bracket 4 rule: keeps seven cards with a land, 3 mana sources in all (fast mana counts) and",
        ),
        (
            3,
            3,
            "bracket 3 rule: keeps seven cards with 2 to 4 lands (3 to 4 when the average mana value is over",
        ),
        (
            2,
            2,
            "bracket 2 rule: keeps seven cards with 3 to 4 lands and a card on the deck's themes or one that",
        ),
        (
            1,
            1,
            "bracket 1 rule: keeps seven cards with 3 to 4 lands and a card on the deck's themes or one that",
        ),
        (3, None, "bracket 3 rule (no bracket set): keeps seven cards with 2 to 4 lands"),
    ],
)
def test_the_keep_rule_names_the_bracket_whose_rule_ran(
    played: GoldfishRun, bracket: int, bracketed: int | None, expected: str
) -> None:
    report = replace(played.report, keep_bracket=bracket, bracket=bracketed)
    assert keep_rule_text(report).startswith(expected)


def test_the_report_is_json_for_the_tab(played: GoldfishRun) -> None:
    document = goldfish_report_json(played.report, played.loaded.deck, 10000001)
    assert document["games"] == 50
    assert document["seed"] == 10000001
    assert document["policyVersion"] == 3
    assert document["bracket"] == 5
    assert document["keepBracket"] == 5
    assert document["bracketTurn"] is None
    assert document["turns"] == [1, 2, 3, 4, 5]
    assert document["waysToWin"] is None
    assert set(document) == {
        "games",
        "seed",
        "policyVersion",
        "bracket",
        "keepBracket",
        "keepRule",
        "bracketTurn",
        "turns",
        "opening",
        "commanders",
        "mana",
        "colourScrew",
        "waysToWin",
        "notModelled",
    }
    mana = document["mana"]
    assert isinstance(mana, dict)
    assert mana["median"] == [1, 2, 3, 4, 5]
    assert mana["worstTenth"] == [1, 2, 3, 4, 5]
    opening = document["opening"]
    assert isinstance(opening, dict)
    assert opening["seven"] == 1.0
    assert opening["six"] == 0.0
    assert opening["fiveOrFewer"] == 0.0
    assert document["colourScrew"] == {"share": played.report.colour_screw, "turn": 3}
    commanders = cast("list[Json]", document["commanders"])
    assert commanders[0]["name"] == "Magda, Brazen Outlaw"
    assert len(cast("list[float]", commanders[0]["castBy"])) == 5


def test_ways_to_win_carry_each_combos_pieces_label_requirements_and_the_warning(played: GoldfishRun) -> None:
    report = with_ways(played.report)
    deck = replace(played.loaded.deck, combos=(COMBO,))
    ways = goldfish_report_json(report, deck, 1)["waysToWin"]
    assert ways == {
        "combos": [
            {
                "id": "1-2",
                "label": "Thassa's Oracle + Demonic Consultation",
                "pieces": ["Thassa's Oracle", "Demonic Consultation"],
                "requirements": ["a creature with power 5"],
                "results": ["Win the game"],
                "landedBy": [0.0, 0.1, 0.2, 0.4, 0.5],
                "fetchedShare": 0.75,
            }
        ],
        "finishers": [{"name": "Overrun", "castableBy": [0.0, 0.0, 0.1, 0.2, 0.3]}],
        # Bracket 4's turn is 4, the third of the turns shown (2 to 6), where 20% of the games had landed it.
        "fasterThanBracket": [
            {"id": "1-2", "label": "Thassa's Oracle + Demonic Consultation", "turn": 4, "share": 0.2}
        ],
    }


def test_the_bracket_turn_is_given_for_brackets_1_to_4_and_not_for_5_or_none(played: GoldfishRun) -> None:
    deck = played.loaded.deck
    assert goldfish_report_json(replace(played.report, bracket=2), deck, 1)["bracketTurn"] == 8
    assert goldfish_report_json(replace(played.report, bracket=5), deck, 1)["bracketTurn"] is None
    assert goldfish_report_json(replace(played.report, bracket=None), deck, 1)["bracketTurn"] is None


def test_figures_are_named_for_the_page(played: GoldfishRun) -> None:
    report = with_ways(played.report)
    labels = {
        change(FigureKind.KEPT_ON_SEVEN): "Kept on seven cards",
        change(FigureKind.KEPT_ON_SIX): "Kept on six cards",
        change(FigureKind.KEPT_ON_FIVE_OR_FEWER): "Kept on five cards or fewer",
        change(FigureKind.COMMANDER_CAST_BY, "Magda", 6): "Commander cast by turn 6",
        change(FigureKind.COMMANDER_MEDIAN, "Magda"): "Commander median turn",
        change(FigureKind.MANA_MEDIAN, "", 6): "Median mana on turn 6",
        change(FigureKind.MANA_WORST_TENTH, "", 6): "Worst-tenth mana on turn 6",
        change(FigureKind.COLOUR_SCREW): "Colour screw",
        change(FigureKind.COMBO_LANDED_BY, "1-2", 6): "Thassa's Oracle + Demonic Consultation by turn 6",
        change(FigureKind.COMBO_FETCHED, "1-2"): "Thassa's Oracle + Demonic Consultation fetched by a tutor",
        change(FigureKind.FINISHER_CASTABLE_BY, "Overrun", 6): "Overrun castable by turn 6",
    }
    assert [figure_label(figure, report) for figure in labels] == list(labels.values())


def test_a_pair_of_commanders_is_told_apart_by_name_and_one_commander_is_just_the_commander(
    played: GoldfishRun,
) -> None:
    pair = replace(
        played.report,
        commanders=(
            CommanderFigures("Tymna the Weaver", (0.0,) * 5, None),
            CommanderFigures("Kraum, Ludevic's Opus", (0.0,) * 5, None),
        ),
    )
    cast = change(FigureKind.COMMANDER_CAST_BY, "Kraum, Ludevic's Opus", 4)
    assert figure_label(cast, pair) == "Kraum, Ludevic's Opus cast by turn 4"
    assert figure_label(cast, played.report) == "Commander cast by turn 4"


def test_a_combo_not_in_the_report_is_labelled_by_its_id(played: GoldfishRun) -> None:
    assert figure_label(change(FigureKind.COMBO_LANDED_BY, "9-9", 3), played.report) == "9-9 by turn 3"


@pytest.mark.parametrize(
    ("kind", "value", "text"),
    [
        (FigureKind.COMMANDER_CAST_BY, 0.4952, "49.5%"),
        (FigureKind.COLOUR_SCREW, 0.0, "0.0%"),
        (FigureKind.MANA_WORST_TENTH, 3, "3"),
        (FigureKind.COMMANDER_MEDIAN, 4, "turn 4"),
        (FigureKind.COMMANDER_MEDIAN, None, "never"),
    ],
)
def test_values_are_told_as_shares_mana_or_turns(kind: FigureKind, value: float | None, text: str) -> None:
    assert value_text(kind, value) == text


@pytest.mark.parametrize(
    ("kind", "base", "swapped", "unit_and_better"),
    [
        (FigureKind.COMMANDER_CAST_BY, 0.2, 0.4, ("share", True)),
        (FigureKind.COMMANDER_CAST_BY, 0.4, 0.2, ("share", False)),
        (FigureKind.COLOUR_SCREW, 0.2, 0.1, ("share", True)),
        (FigureKind.COLOUR_SCREW, 0.1, 0.2, ("share", False)),
        (FigureKind.MANA_WORST_TENTH, 3, 5, ("mana", True)),
        (FigureKind.MANA_MEDIAN, 5, 4, ("mana", False)),
        (FigureKind.COMMANDER_MEDIAN, 5, 4, ("turn", True)),
        (FigureKind.COMMANDER_MEDIAN, None, 4, ("turn", None)),
        (FigureKind.KEPT_ON_SEVEN, 0.9, 0.8, ("share", False)),
        (FigureKind.KEPT_ON_SIX, 0.05, 0.1, ("share", False)),
        (FigureKind.COMBO_LANDED_BY, 0.1, 0.4, ("share", None)),
        (FigureKind.FINISHER_CASTABLE_BY, 0.1, 0.4, ("share", None)),
    ],
)
def test_a_figure_says_its_unit_and_whether_it_moved_the_way_the_deck_wants(
    played: GoldfishRun,
    kind: FigureKind,
    base: float | None,
    swapped: float | None,
    unit_and_better: tuple[str, bool | None],
) -> None:
    document = change_json(change(kind, "x", 4, base=base, swapped=swapped), with_ways(played.report))
    assert (document["unit"], document["better"]) == unit_and_better
    assert document["before"] == base
    assert document["after"] == swapped
    assert document["moved"] is True
    assert document["turn"] == 4
    assert document["kind"] == kind.name.lower()


def test_a_change_is_json_with_its_label_and_values(played: GoldfishRun) -> None:
    document = change_json(change(FigureKind.MANA_WORST_TENTH, "", 6, base=3, swapped=4), played.report)
    assert document == {
        "kind": "mana_worst_tenth",
        "subject": "",
        "turn": 6,
        "label": "Worst-tenth mana on turn 6",
        "unit": "mana",
        "before": 3,
        "after": 4,
        "moved": True,
        "better": True,
    }
