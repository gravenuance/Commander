"""`cmdr goldfish --cuts`: a board card played against each deck card it could replace, in the order the app
shows them (ADR 0056)."""

import argparse
from collections.abc import Sequence
from typing import TextIO

from commander.board import BoardCard, read_board
from commander.cli_meta import cached_pool
from commander.cli_output import progress_printer, write_json
from commander.clibase import EXIT_OK, Context, UsageError, one_deck, open_card_db, workspace
from commander.config import cache_dir
from commander.deckprofile import read_profile
from commander.goldfish import GAMES, FigureChange, GoldfishComparison
from commander.goldfish_cuts import RankedCut, rank_cuts
from commander.goldfish_json import Json, changes_json, figure_label, value_text
from commander.goldfish_load import GoldfishError, load_goldfish
from commander.goldfish_run import SwapRequest, compare_swaps, headline, run, seed_of
from commander.models import Deck
from commander.targets import Target, deck_cut_rules, targets_for, with_ideas


def cmd_cuts(args: argparse.Namespace, ctx: Context) -> int:
    if args.swap or args.plan:
        raise UsageError("--cuts plays a card against its own targets; leave out --swap and --plan")
    if args.deck is None:
        raise UsageError("name a deck for --cuts")
    stored = one_deck(workspace(args, ctx), args.deck)
    deck = stored.deck
    card = read_board(stored.folder).find(args.cuts)
    if card is None:
        raise UsageError(
            f"{args.cuts!r} is not on the board of {deck.short_name}; `cmdr board add` puts it there"
        )
    progress = progress_printer(ctx.err)
    with open_card_db(ctx) as db:
        profile = read_profile(stored.folder)
        main = db.resolve(deck.main_cards)
        info = db.cards([card.oracle_id]).get(card.oracle_id)
        rules = deck_cut_rules(deck, main, profile, db, cached_pool(deck, args, ctx), now=ctx.now)
        targets = targets_for(card, info, main, with_ideas(rules, [info] if info else [], db))
        if not targets:
            raise UsageError(f"{card.name} has no cut targets in {deck.short_name}")
        progress(f"reading {deck.name}")
        loaded = load_goldfish(deck, db, profile, cache_dir(ctx.env), now=ctx.now)
        progress(f"playing {GAMES:,} games")
        played = run(loaded)

        def compare(target: Target) -> GoldfishComparison:
            return compare_swaps(played, db, [SwapRequest(card.name, target.name, card.copies)])

        try:
            ranked = rank_cuts(targets, compare, everything=True)
        except GoldfishError as error:
            raise UsageError(str(error)) from error
    if args.json:
        write_json(_json(deck, card, ranked), ctx.out)
    else:
        _print(deck, card, ranked, seed_of(loaded), ctx.out)
    return EXIT_OK


def _headline(cut: RankedCut) -> tuple[list[FigureChange], GoldfishComparison | None]:
    """The headline figures the target's swap moved, with the comparison they belong to."""
    if cut.verdict is None:
        return [], None
    return headline(cut.verdict.comparison), cut.verdict.comparison


def _print(
    deck: Deck,
    card: BoardCard,
    ranked: Sequence[RankedCut],
    seed: int,
    out: TextIO,
) -> None:
    print(
        f"{deck.name} ({deck.id}): {card.name} against its cut targets, {GAMES:,} games, seed {seed}",
        file=out,
    )
    for place, cut in enumerate(ranked, start=1):
        score = "" if cut.target.score is None else f" (score {cut.target.score:.2f})"
        print(f"{place}. {cut.target.name}{score}", file=out)
        if cut.moved_up:
            print(f"     goldfish pick, up from place {cut.score_place}: {cut.why}", file=out)
        changes, comparison = _headline(cut)
        if comparison is None or not changes:
            print("     no headline figure moved", file=out)
            continue
        for change in changes:
            before = value_text(change.kind, change.base)
            after = value_text(change.kind, change.swapped)
            print(f"     {figure_label(change, comparison.base)}: {before} -> {after}", file=out)


def _json(deck: Deck, card: BoardCard, ranked: Sequence[RankedCut]) -> Json:
    cuts: list[Json] = []
    for place, cut in enumerate(ranked, start=1):
        changes, comparison = _headline(cut)
        cuts.append(
            {
                "name": cut.target.name,
                "place": place,
                "scorePlace": cut.score_place,
                "score": cut.target.score,
                "reason": cut.target.reason,
                "source": cut.target.source.value,
                "movedUp": cut.moved_up,
                "why": cut.why,
                "figures": changes_json(changes, comparison.base) if comparison is not None else [],
            }
        )
    return {
        "deck": {"id": deck.id, "name": deck.name, "short": deck.short_name},
        "card": card.name,
        "copies": card.copies,
        "cuts": cuts,
    }
