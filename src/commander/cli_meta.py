"""`cmdr meta`: what performing tournament lists and recent Archidekt decks play with the deck's commander,
and which of the meta's cards answer its key cards."""

import argparse
from collections.abc import Callable, Sequence
from typing import TextIO

import httpx

from commander.bracket import CACHE_FOLDER as SPELLBOOK_CACHE_FOLDER
from commander.bracket import estimate_cache, estimated_decks
from commander.carddb import CardDb
from commander.cli_output import progress_printer, write_json
from commander.clibase import EXIT_FAILURE, EXIT_OK, Context, UsageError, one_deck, open_card_db, workspace
from commander.config import MetaSettings, cache_dir, config_path, read_config, resolve_user
from commander.deckcache import AnswerSource
from commander.deckprofile import read_profile
from commander.ideas import compile_ideas_with_adjacent, ideas_summary
from commander.meta import (
    Basis,
    CardCheck,
    MetaAnswer,
    MetaReport,
    analyse,
    check_card,
)
from commander.metacombos import AnswerRole, ComboAnswer, MetaCombo, group_combos
from commander.metadata import (
    NOT_READ,
    TOURNAMENT_BRACKETS,
    MetaData,
    MetaSources,
    Progress,
    gather,
    gather_for,
)
from commander.metadata import meta_sources as shared_meta_sources
from commander.metajson import report_json
from commander.models import short_commander_name
from commander.playstyle import Fit
from commander.spellbook import SpellbookClient
from commander.store import StoredDeck, Workspace
from commander.topdeck import CREDIT, SITE_URL

META_CREDIT = (
    f"{CREDIT}, {SITE_URL}; also Archidekt (archidekt.com) and Commander Spellbook "
    "(commanderspellbook.com); none of them endorses this tool"
)
THREATS_PER_KIND = 5
COMBO_ANSWERS_SHOWN = 6
_DESCRIPTION = """examples:
  cmdr meta                                       every bracket 4 and 5 deck side by side
  cmdr meta magda                                 the full report for one deck
  cmdr meta ellivere --card "Sylvan Safekeeper"   how lists use one card
  cmdr meta etali --to-board                      put the staples it misses on its board

Compares a deck with its commander's tournament lists from TopDeck.gg (top cuts first, else every list; needs
a TopDeck key, which `cmdr topdeck` saves) and the
most-viewed recent Archidekt decks, and lists the cards the most-played commanders answer its key
cards with: the commander, its combos, the cards filed under a combo or win category (by you or by
other players), cards costing far above the deck's curve, and what profile.toml keeps. Answers are
cached for a day (Spellbook's combos for a week); the window and sample sizes are set in the config's
[meta] table.

A deck with no bracket set counts as the lowest bracket of its Commander Spellbook estimate. Bracket 4 decks
read their commander's tournament lists and recent Archidekt decks, not the most-played commanders.
"""


def add_meta_command(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    meta = add_parser(
        "meta",
        help="what performing tournament lists and recent Archidekt decks play, and meta threats",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    meta.add_argument(
        "deck",
        nargs="?",
        help="deck id, or part of its name or commander; omit for every bracket 4 and 5 deck",
    )
    meta.add_argument("--workspace", help="folder that holds the deck folders")
    meta.add_argument("--user", help="Archidekt username whose own decks are left out")
    meta.add_argument(
        "--card", action="append", default=[], metavar="NAME", help="check one card (repeatable)"
    )
    meta.add_argument(
        "-r", "--refresh", action="store_true", help="fetch again even if a recent answer is cached"
    )
    meta.add_argument("--json", action="store_true", help="print the report as JSON")
    meta.add_argument(
        "--to-board", action="store_true", help="put the staples the deck is missing on its board (one deck)"
    )
    meta.set_defaults(handler=_cmd_meta)


def _cmd_meta(args: argparse.Namespace, ctx: Context) -> int:
    if (args.to_board or args.card) and args.deck is None:
        raise UsageError("--to-board and --card need one deck")
    file = read_config(config_path(args.config, ctx.env))
    user = resolve_user(args.user, ctx.env, file)
    space = workspace(args, ctx)
    progress = progress_printer(ctx.err)
    results: list[tuple[StoredDeck, MetaData, MetaReport]] = []
    with open_card_db(ctx) as db, ctx.client_factory() as http:
        sources = meta_sources(http, ctx, user, file.meta)
        for deck in _decks(space, args.deck, sources.spellbook, ctx, refresh=args.refresh):
            bracket = deck.deck.bracket
            if bracket not in TOURNAMENT_BRACKETS:
                held = "has no bracket" if bracket is None else f"is bracket {bracket}"
                print(
                    f"cmdr: {deck.deck.short_name} {held}; the tournament lists in this report are cEDH",
                    file=ctx.err,
                )
            # Bracket 4 reads its lists and Archidekt decks only (ADR 0027); a deck named below 4, all.
            read = gather_for if bracket in TOURNAMENT_BRACKETS else gather
            data = read(
                deck.deck,
                sources,
                cache_dir(ctx.env),
                now=ctx.now,
                refresh=args.refresh,
                on_progress=progress,
            )
            report = analyse(deck.deck, data, read_profile(deck.folder), db, now=ctx.now)
            results.append((deck, data, report))
            if args.to_board:
                _to_board(deck, data, db, sources, ctx, refresh=args.refresh, progress=progress)
    for _, _, report in results:
        _warn_sources(report, ctx.err)
    _print_results(args, results, ctx.out)
    unavailable = any(
        s.source is AnswerSource.UNAVAILABLE and s.error not in NOT_READ
        for _, _, r in results
        for s in r.sources
    )
    return EXIT_FAILURE if unavailable else EXIT_OK


def meta_sources(http: httpx.Client, ctx: Context, user: str, settings: MetaSettings) -> MetaSources:
    """TopDeck, Archidekt search and Spellbook clients over one HTTP client, each at its own pace."""
    return shared_meta_sources(http, ctx.pacer_factory, user, settings, cache_dir(ctx.env))


def _print_results(
    args: argparse.Namespace, results: Sequence[tuple[StoredDeck, MetaData, MetaReport]], out: TextIO
) -> None:
    if args.json:
        checks = [check_card(name, s.deck, data) for s, data, _ in results for name in args.card]
        write_json(
            {
                "reports": [report_json(r) for _, _, r in results],
                "cards": [check_json(c) for c in checks],
                "credit": META_CREDIT,
            },
            out,
        )
        return
    if args.card:
        [(deck, data, report)] = results
        for name in args.card:
            print_check(check_card(name, deck.deck, data), report, out)
            print(file=out)
    elif args.deck:
        print_report(results[0][2], out)
        print(file=out)
    else:
        _print_table([r for _, _, r in results], out)
    print(META_CREDIT, file=out)


def _decks(
    space: Workspace, query: str | None, client: SpellbookClient, ctx: Context, *, refresh: bool
) -> list[StoredDeck]:
    """The named deck, or every deck at bracket 4 or 5, each with its effective bracket (ADR 0051): a deck
    with none set is asked of Spellbook's estimate, which is cached."""
    found = [one_deck(space, query)] if query is not None else space.load_all()
    cache = estimate_cache(cache_dir(ctx.env) / SPELLBOOK_CACHE_FOLDER)
    decks = estimated_decks([s.deck for s in found], client, cache, now=ctx.now, refresh=refresh)
    chosen = [StoredDeck(s.folder, deck) for s, deck in zip(found, decks, strict=True)]
    if query is not None:
        return chosen
    chosen = [s for s in chosen if s.deck.bracket in TOURNAMENT_BRACKETS]
    if not chosen:
        raise UsageError("no bracket 4 or 5 decks; name a deck to check it anyway")
    return chosen


def _to_board(
    deck: StoredDeck,
    data: MetaData,
    db: CardDb,
    sources: MetaSources,
    ctx: Context,
    *,
    refresh: bool,
    progress: Progress,
) -> None:
    def warn(message: str) -> None:
        print(f"cmdr: {message}", file=ctx.err)

    added = compile_ideas_with_adjacent(
        deck,
        data,
        db,
        sources,
        cache_dir(ctx.env),
        now=ctx.now,
        refresh=refresh,
        warn=warn,
        on_progress=progress,
    )
    print(f"{deck.deck.short_name} board: {ideas_summary(added)}", file=ctx.err)


def _warn_sources(report: MetaReport, err: TextIO) -> None:
    for state in report.sources:
        if state.error in NOT_READ:
            continue
        if state.source is AnswerSource.STALE and state.fetched_at is not None:
            print(
                f"cmdr: {report.deck}: {state.name} failed ({state.error}); "
                f"showing the copy from {state.fetched_at:%Y-%m-%d %H:%M} UTC",
                file=err,
            )
        elif state.source is AnswerSource.UNAVAILABLE:
            print(f"cmdr: {report.deck}: {state.name} unavailable: {state.error}", file=err)


def _percent(share: float) -> str:
    return f"{round(share * 100):>3}%"


def print_report(report: MetaReport, out: TextIO) -> None:
    sample = report.sample
    print(f"{report.deck}: {report.commander}, events since {report.since}", file=out)
    if not sample.known:
        print("  TopDeck has no lists for this commander", file=out)
    else:
        print(
            f"  {sample.entries} entries, {sample.with_lists} with decklists, {sample.top_cuts} top cuts; "
            f"reading {sample.performing} {sample.basis.value}",
            file=out,
        )
    if sample.known and sample.thin:
        reason = "no top cuts" if sample.basis is Basis.ALL else "few lists"
        print(f"  thin data ({reason}): read shares as hints, not evidence", file=out)
    _print_missing(report, out)
    _print_outliers(report, out)
    _print_threats(report, out)
    _print_own_combos(report, out)
    _print_combos(report, out)
    _print_field(report, out)
    _print_peers(report, out)


def _print_missing(report: MetaReport, out: TextIO) -> None:
    style = report.style.label
    weighed = f"; weighed for {style}" if style else ""
    print(f"\nStaples you don't play (share of lists read · recent Archidekt lists{weighed})", file=out)
    if not report.missing:
        print("  none", file=out)
    for card in report.missing:
        note = ""
        if card.fit is Fit.FAVOURED:
            note += f"  [fits {style}]"
        elif card.fit is Fit.OFF_PLAN:
            note += f"  [off-plan for {style}]"
        print(
            f"  {_percent(card.share)} · {card.peer_lists}/{len(report.peers)}  {card.name}{note}", file=out
        )


def _print_outliers(report: MetaReport, out: TextIO) -> None:
    print("\nYour own picks (in under 10% of the lists read)", file=out)
    if not report.outliers:
        print("  none", file=out)
    for card in report.outliers:
        note = "  [kept by your profile]" if card.flagged else ""
        print(
            f"  {_percent(card.share)} · {card.peer_lists}/{len(report.peers)}  {card.name}{note}", file=out
        )


def _print_threats(report: MetaReport, out: TextIO) -> None:
    print(f"\nYour key cards ({len(report.key_cards)})", file=out)
    for key in report.key_cards:
        print(f"  {key.name}: {'; '.join(key.reasons)}", file=out)
    print("  (add one under [keep] in the profile; leave one out under [key_cards] ignore)", file=out)
    print("\nMeta answers to your key cards", file=out)
    total = sum(stats.meta_share for stats in report.field)
    if not report.threats or total == 0:
        print("  none found", file=out)
        return
    print("  (share of the top commanders' lists, weighted by how often each is played)", file=out)
    by_kind: dict[str, list[str]] = {}
    for threat in report.threats:
        rows = by_kind.setdefault(threat.kinds[0].label, [])
        if len(rows) >= THREATS_PER_KIND:
            continue
        players = ", ".join(
            f"{short_commander_name(name)} {round(share * 100)}%" for name, share in threat.played_by[:3]
        )
        hits = "every key card" if len(threat.hits) == len(report.key_cards) else _names(threat.hits)
        rows.append(f"    {_percent(threat.exposure / total)}  {threat.name}: hits {hits}  ({players})")
    for kind, rows in by_kind.items():
        print(f"  {kind}", file=out)
        for row in rows:
            print(row, file=out)


def _print_own_combos(report: MetaReport, out: TextIO) -> None:
    print("\nYour combos and what the meta stops them with (commonly played cards)", file=out)
    if not report.own_combos:
        print("  none found", file=out)
        return
    for combo in report.own_combos:
        print(f"  {' + '.join(combo.pieces)}: {'; '.join(combo.results[:2])}", file=out)
        if not combo.answers:
            print("        nothing the meta commonly plays stops it", file=out)
            continue
        for role in AnswerRole:
            answers = [a for a in combo.answers if a.role is role]
            if answers:
                shown = [f"{_answer_shown(a)} [{_players(a.played_by)}]" for a in answers]
                print(f"        {role.value}: {_names(shown, COMBO_ANSWERS_SHOWN)}", file=out)


def _players(played_by: Sequence[tuple[str, float]]) -> str:
    return ", ".join(f"{short_commander_name(name)} {round(share * 100)}%" for name, share in played_by[:3])


def _print_combos(report: MetaReport, out: TextIO) -> None:
    print("\nMeta combos and your answers (share of the top commanders' lists, weighted)", file=out)
    total = sum(stats.meta_share for stats in report.field)
    if not report.combos or total == 0:
        print("  none found", file=out)
        return
    for group in group_combos(report.combos):
        if group.shared:
            print(f"  Shared: {', '.join(group.shared)}", file=out)
        indent = "    " if group.shared else ""
        for combo in group.combos:
            pieces = " + ".join(p for p in combo.pieces if p not in group.shared) or "only the shared pieces"
            head = f"{indent}  {_percent(combo.exposure / total)}  {pieces}"
            _print_combo(report, combo, head, indent, out)


def _print_combo(report: MetaReport, combo: MetaCombo, head: str, indent: str, out: TextIO) -> None:
    players = ", ".join(
        f"{short_commander_name(name)} {round(share * 100)}%" for name, share in combo.played_by[:3]
    )
    results = "; ".join(combo.results[:2])
    rising = "  rising" if combo.rising else ""
    print(f"{head}: {results}  ({players}){rising}", file=out)
    if combo.requirements:
        print(f"{indent}        also needs: {'; '.join(combo.requirements)}", file=out)
    if not combo.answers:
        racing = f" ({report.style.label}: racing, not answering)" if report.style.races else ""
        print(f"{indent}        no answer in the deck{racing}", file=out)
        return
    for role in AnswerRole:
        answers = [a for a in combo.answers if a.role is role]
        if not answers:
            continue
        shown = [_answer_shown(a) for a in answers]
        print(f"{indent}        {role.value}: {_names(shown, COMBO_ANSWERS_SHOWN)}", file=out)


def _answer_shown(answer: ComboAnswer | MetaAnswer) -> str:
    """Stax answers name how they stop the combo; removal and counters need only the card and any catch."""
    if answer.role is AnswerRole.STAX:
        return f"{answer.card} ({answer.how})"
    return f"{answer.card} ({answer.catch})" if answer.catch else answer.card


def _names(names: Sequence[str], shown: int = 3) -> str:
    if len(names) <= shown:
        return ", ".join(names)
    return f"{', '.join(names[:shown])} and {len(names) - shown} more"


def _print_field(report: MetaReport, out: TextIO) -> None:
    print(f"\nMost-played commanders since {report.since} (entries · top cuts · conversion)", file=out)
    if not report.field:
        print("  unavailable", file=out)
    for stats in report.field:
        print(
            f"  {stats.entries:>5} · {stats.top_cuts:>4} · {_percent(stats.conversion)}  {stats.name}",
            file=out,
        )


def _print_peers(report: MetaReport, out: TextIO) -> None:
    print(f"\nMost-viewed Archidekt decks updated since {report.since}", file=out)
    if not report.peers:
        print("  none", file=out)
    for peer in report.peers:
        print(f"  {peer.views:>6} views  {peer.updated_at:%Y-%m-%d}  {peer.name}  {peer.url}", file=out)
    if report.peers_skipped:
        print(f"  ({report.peers_skipped} more could not be read)", file=out)


def print_check(check: CardCheck, report: MetaReport, out: TextIO) -> None:
    print(f"{check.name} for {report.deck} (events since {report.since})", file=out)
    print(f"  in your deck: {'yes' if check.in_deck else 'no'}", file=out)
    print(
        f"  tournaments: in {check.performing_with} of {check.performing} {report.sample.basis.value}, "
        f"{check.lists_with} of {check.lists} lists in all",
        file=out,
    )
    if check.lists:
        print(
            f"  top cuts: {check.top_cuts_with} with it, {check.top_cuts_without} without; "
            f"average finish {_finish(check.mean_placing_with)} with it, "
            f"{_finish(check.mean_placing_without)} without",
            file=out,
        )
    print(f"  Archidekt: in {check.peers_with} of {check.peers} recent decks", file=out)
    print(f"  meta: in {check.meta_lists_with} of {check.meta_lists} top commanders' lists", file=out)
    if report.sample.thin:
        print("  thin data: read these counts as hints, not evidence", file=out)


def _finish(placing: float | None) -> str:
    return "n/a" if placing is None else f"top {round(placing * 100)}%"


def _print_table(reports: Sequence[MetaReport], out: TextIO) -> None:
    print("deck        lists  top cuts  read  missing  threats  unanswered combos", file=out)
    for r in reports:
        thin = " thin" if r.sample.thin else ""
        # A deck that races counts no unanswered combo as a gap (ADR 0031).
        open_combos = "racing" if r.style.races else str(sum(1 for c in r.combos if not c.answers))
        print(
            f"{r.deck[:10]:<10}  {r.sample.with_lists:>5}  {r.sample.top_cuts:>8}  {r.sample.performing:>4}"
            f"  {len(r.missing):>7}  {len(r.threats):>7}  {open_combos:>17}{thin}",
            file=out,
        )


def check_json(check: CardCheck) -> dict[str, object]:
    return {
        "name": check.name,
        "inDeck": check.in_deck,
        "read": check.performing,
        "readWith": check.performing_with,
        "lists": check.lists,
        "listsWith": check.lists_with,
        "topCutsWith": check.top_cuts_with,
        "topCutsWithout": check.top_cuts_without,
        "meanPlacingWith": check.mean_placing_with,
        "meanPlacingWithout": check.mean_placing_without,
        "peers": check.peers,
        "peersWith": check.peers_with,
        "metaLists": check.meta_lists,
        "metaListsWith": check.meta_lists_with,
    }
