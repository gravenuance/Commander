"""`cmdr goldfish`: how a deck plays out over many solitaire games, and what swaps would change (ADR 0054)."""

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from commander.cli_output import progress_printer, write_json
from commander.clibase import EXIT_OK, Context, UsageError, one_deck, open_card_db, workspace
from commander.config import cache_dir
from commander.deckprofile import read_profile
from commander.goldfish import GAMES, WARN_SHARE, GoldfishComparison, GoldfishReport
from commander.goldfish_json import (
    bracket_turn,
    changes_json,
    combo_label,
    figure_label,
    goldfish_report_json,
    keep_rule_text,
    value_text,
)
from commander.goldfish_load import GoldfishError, GoldfishInput, load_goldfish
from commander.goldfish_run import GoldfishRun, SwapRequest, compare_swaps, headline, run, seed_of
from commander.models import Deck
from commander.plan import Plan, read_plan

DESCRIPTION = """\
Deal and play the deck by a fixed policy against an empty table, from cached data alone. Nothing is
fetched; `cmdr sync` keeps the combos and the bracket estimate it reads.

examples:
  cmdr goldfish ellivere                  how the deck plays out over 10,000 games
  cmdr goldfish ellivere --swap "Arcane Signet=Wild Growth"
                                          what putting the first in for the second changes
  cmdr goldfish --plan plan.toml          the same for each add of a plan paired with a remove
  cmdr goldfish ellivere --json           everything as JSON

A swap is "<card in>=<card out>" and takes the place of every copy of the card out. A plan pairs its adds
with its removes in order; each remove must name a card of the deck and keep none of it. A figure is
shown as moved only when the change is larger than the luck of the same draws allows.
"""

_COLUMN = 7
_LABEL = 34
_PERCENT = 100


def add_goldfish_command(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    goldfish = add_parser(
        "goldfish",
        help="play a deck out 10,000 times: opening hands, commander, mana, ways to win; try swaps",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    goldfish.add_argument(
        "deck", nargs="?", help="deck id, or part of its name or commander (the plan's deck with --plan)"
    )
    goldfish.add_argument(
        "--swap",
        action="append",
        default=[],
        metavar="IN=OUT",
        help="a card to put in and the deck card it replaces; repeat for more",
    )
    goldfish.add_argument(
        "--plan", metavar="FILE", help="a plan file as `cmdr apply` reads it, or - for stdin"
    )
    goldfish.add_argument("--workspace", help="folder that holds the deck folders")
    goldfish.add_argument("--json", action="store_true", help="print the figures as JSON")
    goldfish.set_defaults(handler=_cmd_goldfish)


def _cmd_goldfish(args: argparse.Namespace, ctx: Context) -> int:
    plan = read_plan(ctx.stdin if args.plan == "-" else Path(args.plan)) if args.plan else None
    stored = one_deck(workspace(args, ctx), _deck_query(args.deck, plan, args, ctx))
    swaps = [*_typed_swaps(args.swap), *(_plan_swaps(plan) if plan else [])]
    progress = progress_printer(ctx.err)
    with open_card_db(ctx) as db:
        progress(f"reading {stored.deck.name}")
        loaded = load_goldfish(stored.deck, db, read_profile(stored.folder), cache_dir(ctx.env), now=ctx.now)
        progress(f"playing {GAMES:,} games")
        played = run(loaded)
        try:
            comparison = compare_swaps(played, db, swaps) if swaps else None
        except GoldfishError as error:
            raise UsageError(str(error)) from error
    if args.json:
        write_json(_json(stored.deck, played, swaps, comparison), ctx.out)
    else:
        _print_report(stored.deck, played, ctx.out)
        if comparison is not None:
            _print_comparison(swaps, comparison, ctx.out)
    return EXIT_OK


def _deck_query(typed: str | None, plan: Plan | None, args: argparse.Namespace, ctx: Context) -> str:
    """The deck asked for: the one named, else the plan's. A plan for another deck than the one named is an
    error, since its swaps would be read against the wrong cards."""
    if typed is None:
        if plan is None:
            raise UsageError("name a deck, or give --plan")
        return plan.deck
    if plan is not None:
        space = workspace(args, ctx)
        if one_deck(space, typed).deck.id != one_deck(space, plan.deck).deck.id:
            raise UsageError(f"the plan is for {plan.deck!r}, not {typed!r}")
    return typed


def _typed_swaps(entries: Sequence[str]) -> list[SwapRequest]:
    swaps: list[SwapRequest] = []
    for entry in entries:
        incoming, divider, cut = (part.strip() for part in entry.partition("="))
        if not divider or not incoming or not cut:
            raise UsageError(f'--swap takes "<card in>=<card out>", not {entry!r}')
        swaps.append(SwapRequest(incoming, cut))
    return swaps


def _plan_swaps(plan: Plan) -> list[SwapRequest]:
    """The plan's adds paired with its removes from the deck, in order."""
    removes = [remove for remove in plan.remove if remove.category is None]
    if len(plan.add) != len(removes):
        raise UsageError(
            f"the plan adds {len(plan.add)} cards and removes {len(removes)} from the deck; "
            "a goldfish swap pairs each add with a remove, in order"
        )
    for remove in removes:
        if remove.keep:
            raise UsageError(f"{remove.name}: the plan keeps {remove.keep}, which is not played as a swap")
    return [SwapRequest(add.name, cut.name, add.quantity) for add, cut in zip(plan.add, removes, strict=True)]


# --- text -------------------------------------------------------------------------------------------------


def _percent(share: float) -> str:
    return f"{share * _PERCENT:.1f}%"


def _row(label: str, cells: Sequence[str]) -> str:
    return f"  {label:<{_LABEL}}" + "".join(f"{cell:>{_COLUMN}}" for cell in cells)


def _print_report(deck: Deck, played: GoldfishRun, out: TextIO) -> None:
    report, loaded = played.report, played.loaded
    print(f"{deck.name} ({deck.id}): {report.games:,} games, seed {seed_of(loaded)}", file=out)
    print(_bracket_line(deck, loaded), file=out)
    print(keep_rule_text(report), file=out)
    print(
        f"opening hand: kept on seven {_percent(report.kept_on_seven)}, "
        f"on six {_percent(report.kept_on_six)}, on five or fewer {_percent(report.kept_on_five_or_fewer)}",
        file=out,
    )
    print(_row("turn", [str(turn) for turn in report.turns]), file=out)
    for commander in report.commanders:
        print(_row(f"{commander.name} cast by", [_percent(share) for share in commander.cast_by]), file=out)
        median = "not within the turns played" if commander.median_turn is None else commander.median_turn
        print(f"    median turn: {median}", file=out)
    print(_row("mana, median", [str(value) for value in report.mana_median]), file=out)
    print(_row("mana, worst tenth", [str(value) for value in report.mana_worst_tenth]), file=out)
    print(
        f"colour screw by turn {report.colour_screw_turn}: {_percent(report.colour_screw)} of games "
        "(mana for a spell, but not in its colours)",
        file=out,
    )
    _print_ways(report, played, out)
    if report.not_modelled:
        print(f"not modelled: {', '.join(report.not_modelled)}", file=out)


def _bracket_line(deck: Deck, loaded: GoldfishInput) -> str:
    if deck.bracket is not None:
        return f"bracket {deck.bracket} on Archidekt"
    if loaded.bracket is not None:
        return f"no bracket set; played as bracket {loaded.bracket}, the lowest its cached estimate allows"
    return "no bracket set and no cached estimate; played by the bracket 3 rule, with no bracket verdict"


def _print_ways(report: GoldfishReport, played: GoldfishRun, out: TextIO) -> None:
    ways = report.ways_to_win
    if ways is None:
        print("ways to win: no Spellbook combo or finisher in the deck, so none is played", file=out)
        return
    print("ways to win", file=out)
    described = {combo.id: combo for combo in played.loaded.deck.combos}
    for combo in ways.combos:
        label = combo_label(combo.pieces)
        print(_row(f"{label} landed by", [_percent(share) for share in combo.landed_by]), file=out)
        fetched = _percent(combo.fetched_share)
        print(f"    a tutor fetched a piece in {fetched} of the games it landed", file=out)
        detail = described.get(combo.id)
        if detail is not None and detail.requirements:
            print(f"    also needs: {'; '.join(detail.requirements)}", file=out)
    for finisher in ways.finishers:
        cells = [_percent(share) for share in finisher.castable_by]
        print(_row(f"{finisher.name} castable by", cells), file=out)
    turn = bracket_turn(report)
    for combo in ways.combos:
        if combo.id in ways.faster_than_bracket and turn is not None:
            share = _percent(combo.landed_by[report.turns.index(turn)])
            print(
                f"! {combo_label(combo.pieces)} lands by turn {turn} in {share} of games, over the "
                f"{_percent(WARN_SHARE)} that bracket {report.bracket} allows: faster than its bracket",
                file=out,
            )


def _print_comparison(swaps: Sequence[SwapRequest], comparison: GoldfishComparison, out: TextIO) -> None:
    print("\nswaps", file=out)
    for swap in swaps:
        times = f"{swap.copies} x " if swap.copies > 1 else ""
        print(f"  + {times}{swap.add}  - {times}{swap.cut}", file=out)
    if not comparison.any_moved:
        print("no figure moved by more than the luck of the draws allows", file=out)
        return
    played_again = f"{len(comparison.replayed):,} of {comparison.base.games:,} games played again"
    print(f"figures that moved ({played_again})", file=out)
    for change in comparison.moved:
        before = value_text(change.kind, change.base)
        after = value_text(change.kind, change.swapped)
        print(f"  {figure_label(change, comparison.base)}: {before} -> {after}", file=out)


# --- JSON -------------------------------------------------------------------------------------------------


def _json(
    deck: Deck, played: GoldfishRun, swaps: Sequence[SwapRequest], comparison: GoldfishComparison | None
) -> dict[str, object]:
    document: dict[str, object] = {
        "deck": {"id": deck.id, "name": deck.name, "short": deck.short_name},
        "report": goldfish_report_json(played.report, played.loaded.deck, seed_of(played.loaded)),
    }
    if comparison is not None:
        document["swaps"] = [{"add": s.add, "cut": s.cut, "copies": s.copies} for s in swaps]
        document["changes"] = changes_json(comparison.moved, comparison.base)
        document["headline"] = changes_json(headline(comparison), comparison.base)
        document["replayed"] = len(comparison.replayed)
    return document
