"""The goldfish's figures as JSON and as the words that name them (ADR 0054), for the app's tab and the CLI.

Shares are fractions of the games, 0 to 1; turns are game turns; mana is the amount a turn could make.
"""

from collections.abc import Sequence

from commander.goldfish import (
    BRACKET_TURN,
    CEDH_BRACKET,
    POLICY_VERSION,
    ComboFigures,
    FigureChange,
    FigureKind,
    GoldfishReport,
)
from commander.goldfish_cards import GoldfishCombo, GoldfishDeck

type Json = dict[str, object]

# The brackets whose keep rule asks for a way to win, and the one that asks for the middle count of lands.
_FAST_RULES = 4
_MIDDLE_RULE = 3

_SHARES = frozenset(
    {
        FigureKind.KEPT_ON_SEVEN,
        FigureKind.KEPT_ON_SIX,
        FigureKind.KEPT_ON_FIVE_OR_FEWER,
        FigureKind.COMMANDER_CAST_BY,
        FigureKind.COLOUR_SCREW,
        FigureKind.COMBO_LANDED_BY,
        FigureKind.COMBO_FETCHED,
        FigureKind.FINISHER_CASTABLE_BY,
    }
)
_MANA = frozenset({FigureKind.MANA_MEDIAN, FigureKind.MANA_WORST_TENTH})
# +1 where a higher figure is better for the deck, -1 where a lower one is, and 0 where neither is.
_BETTER_WHEN = {
    FigureKind.KEPT_ON_SEVEN: 1,
    FigureKind.KEPT_ON_SIX: -1,
    FigureKind.KEPT_ON_FIVE_OR_FEWER: -1,
    FigureKind.COMMANDER_CAST_BY: 1,
    FigureKind.COMMANDER_MEDIAN: -1,
    FigureKind.MANA_MEDIAN: 1,
    FigureKind.MANA_WORST_TENTH: 1,
    FigureKind.COLOUR_SCREW: -1,
    FigureKind.COMBO_LANDED_BY: 0,
    FigureKind.COMBO_FETCHED: 0,
    FigureKind.FINISHER_CASTABLE_BY: 0,
}


_LABELS = {
    FigureKind.KEPT_ON_SEVEN: "Kept on seven cards",
    FigureKind.KEPT_ON_SIX: "Kept on six cards",
    FigureKind.KEPT_ON_FIVE_OR_FEWER: "Kept on five cards or fewer",
    FigureKind.COMMANDER_CAST_BY: "{commander} cast{by}",
    FigureKind.COMMANDER_MEDIAN: "{commander} median turn",
    FigureKind.MANA_MEDIAN: "Median mana{on}",
    FigureKind.MANA_WORST_TENTH: "Worst-tenth mana{on}",
    FigureKind.COLOUR_SCREW: "Colour screw",
    FigureKind.COMBO_LANDED_BY: "{combo}{by}",
    FigureKind.COMBO_FETCHED: "{combo} fetched by a tutor",
    FigureKind.FINISHER_CASTABLE_BY: "{finisher} castable{by}",
}


def keep_rule_text(report: GoldfishReport) -> str:
    """The opening-hand rule the games were played by, in a sentence."""
    rule = report.keep_bracket
    themes = "a card on the deck's themes or one that draws"
    if rule >= _FAST_RULES:
        sources = 2 if rule == CEDH_BRACKET else 3
        text = (
            f"a land, {sources} mana sources in all (fast mana counts) and a way to win "
            f"(a combo piece, a tutor or a finisher; with none in the deck: {themes})"
        )
    elif rule == _MIDDLE_RULE:
        text = f"2 to 4 lands (3 to 4 when the average mana value is over 3.5) and {themes}"
    else:
        text = f"3 to 4 lands and {themes}"
    note = "" if report.bracket is not None else " (no bracket set)"
    return f"bracket {rule} rule{note}: keeps seven cards with {text}"


def bracket_turn(report: GoldfishReport) -> int | None:
    """The turn of the report's bracket (9, 8, 6 or 4), None without one or in bracket 5."""
    return None if report.bracket is None else BRACKET_TURN.get(report.bracket)


def combo_label(pieces: Sequence[str]) -> str:
    return " + ".join(pieces)


def goldfish_report_json(report: GoldfishReport, deck: GoldfishDeck, seed: int) -> Json:
    """The whole report for the Goldfish tab."""
    return {
        "games": report.games,
        "seed": seed,
        "policyVersion": POLICY_VERSION,
        "bracket": report.bracket,
        "keepBracket": report.keep_bracket,
        "keepRule": keep_rule_text(report),
        "bracketTurn": bracket_turn(report),
        "turns": list(report.turns),
        "opening": {
            "seven": report.kept_on_seven,
            "six": report.kept_on_six,
            "fiveOrFewer": report.kept_on_five_or_fewer,
        },
        "commanders": [
            {"name": c.name, "castBy": list(c.cast_by), "medianTurn": c.median_turn}
            for c in report.commanders
        ],
        "mana": {"median": list(report.mana_median), "worstTenth": list(report.mana_worst_tenth)},
        "colourScrew": {"share": report.colour_screw, "turn": report.colour_screw_turn},
        "waysToWin": _ways_json(report, deck),
        "notModelled": list(report.not_modelled),
    }


def _ways_json(report: GoldfishReport, deck: GoldfishDeck) -> Json | None:
    ways = report.ways_to_win
    if ways is None:
        return None
    described = {combo.id: combo for combo in deck.combos}
    turn = bracket_turn(report)
    faster: list[Json] = (
        []
        if turn is None
        else [
            {
                "id": figures.id,
                "label": combo_label(figures.pieces),
                "turn": turn,
                "share": figures.landed_by[report.turns.index(turn)],
            }
            for figures in ways.combos
            if figures.id in ways.faster_than_bracket
        ]
    )
    return {
        "combos": [_combo_json(figures, described.get(figures.id)) for figures in ways.combos],
        "finishers": [{"name": f.name, "castableBy": list(f.castable_by)} for f in ways.finishers],
        "fasterThanBracket": faster,
    }


def _combo_json(figures: ComboFigures, combo: GoldfishCombo | None) -> Json:
    return {
        "id": figures.id,
        "label": combo_label(figures.pieces),
        "pieces": list(figures.pieces),
        "requirements": list(combo.requirements) if combo is not None else [],
        "results": list(combo.results) if combo is not None else [],
        "landedBy": list(figures.landed_by),
        "fetchedShare": figures.fetched_share,
    }


def figure_label(change: FigureChange, report: GoldfishReport) -> str:
    """The words for a figure, as the page shows it: "Commander cast by turn 6", "Worst-tenth mana on turn 6",
    "Colour screw", "Mountain + Sol Ring by turn 6"."""
    turn = change.turn
    return _LABELS[change.kind].format(
        commander=change.subject if len(report.commanders) > 1 else "Commander",
        combo=_combo_name(change.subject, report),
        finisher=change.subject,
        by=f" by turn {turn}" if turn is not None else "",
        on=f" on turn {turn}" if turn is not None else "",
    )


def _combo_name(combo_id: str, report: GoldfishReport) -> str:
    ways = report.ways_to_win
    figures = next((c for c in ways.combos if c.id == combo_id), None) if ways is not None else None
    return combo_label(figures.pieces) if figures is not None else combo_id


def value_text(kind: FigureKind, value: float | None) -> str:
    """A figure's value in words: a percentage, a turn ("never" when too few games got there) or mana."""
    if value is None:
        return "never"
    if kind in _SHARES:
        return f"{value * 100:.1f}%"
    return f"{value:.0f}" if kind in _MANA else f"turn {value:.0f}"


def change_json(change: FigureChange, report: GoldfishReport) -> Json:
    """One figure before and after a swap. `better` is true when the figure moved the way the deck wants,
    false when it moved against it and null when neither way is better (or a side is missing)."""
    return {
        "kind": change.kind.name.lower(),
        "subject": change.subject,
        "turn": change.turn,
        "label": figure_label(change, report),
        "unit": "share" if change.kind in _SHARES else "mana" if change.kind in _MANA else "turn",
        "before": change.base,
        "after": change.swapped,
        "moved": change.moved,
        "better": _better(change),
    }


def changes_json(changes: Sequence[FigureChange], report: GoldfishReport) -> list[Json]:
    return [change_json(change, report) for change in changes]


def _better(change: FigureChange) -> bool | None:
    direction = _BETTER_WHEN[change.kind]
    if direction == 0 or change.base is None or change.swapped is None or change.base == change.swapped:
        return None
    return (change.swapped > change.base) == (direction > 0)
