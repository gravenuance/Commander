"""`cmdr meta`: what performing EDHTop16 lists and recent Archidekt decks play with the deck's commander, and
which of the meta's cards answer its key cards."""

import argparse
import json
from collections.abc import Callable, Sequence
from typing import TextIO

import httpx

from commander.carddb import CardDb
from commander.cli_output import progress_printer
from commander.clibase import EXIT_FAILURE, EXIT_OK, Context, UsageError, one_deck, open_card_db, workspace
from commander.config import MetaSettings, cache_dir, config_path, read_config, resolve_user
from commander.deckcache import AnswerSource
from commander.deckprofile import read_profile
from commander.edhtop16 import commander_url
from commander.ideas import compile_ideas, ideas_summary
from commander.meta import (
    Basis,
    CardCheck,
    CardShare,
    MetaAnswer,
    MetaReport,
    analyse,
    check_card,
)
from commander.metacombos import AnswerRole, ComboAnswer
from commander.metadata import CEDH_BRACKET, MetaData, MetaSources, gather
from commander.metadata import meta_sources as shared_meta_sources
from commander.models import short_commander_name
from commander.playstyle import Fit
from commander.store import StoredDeck, Workspace

META_CREDIT = (
    "data from EDHTop16 (edhtop16.com), Archidekt (archidekt.com) and Commander Spellbook "
    "(commanderspellbook.com); none of them endorses this tool"
)
THREATS_PER_KIND = 5
EVENT_NAME_WIDTH = 48
COMBO_ANSWERS_SHOWN = 6
_DESCRIPTION = """examples:
  cmdr meta                                       every bracket 5 deck side by side
  cmdr meta magda                                 the full report for one deck
  cmdr meta ellivere --card "Sylvan Safekeeper"   how lists use one card
  cmdr meta etali --to-board                      put the staples it misses on its board

Compares a deck with its commander's EDHTop16 lists (top cuts first, else every list) and the
most-viewed recent Archidekt decks, and lists the cards the most-played commanders answer its key
cards with: the commander, its combos, the cards filed under a combo or win category (by you or by
other players), cards costing far above the deck's curve, and what profile.toml keeps. Answers are
cached for a day;
the window and sample sizes are set in the config's [meta] table.
"""


def add_meta_command(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    meta = add_parser(
        "meta",
        help="what performing tournament lists and recent Archidekt decks play, and meta threats",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    meta.add_argument(
        "deck", nargs="?", help="deck id, or part of its name or commander; omit for every bracket 5 deck"
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
    stored = _decks(space, args.deck)
    progress = progress_printer(ctx.err)
    results: list[tuple[StoredDeck, MetaData, MetaReport]] = []
    with open_card_db(ctx) as db, ctx.client_factory() as http:
        sources = meta_sources(http, ctx, user, file.meta)
        for deck in stored:
            if deck.deck.bracket != CEDH_BRACKET:
                print(
                    f"cmdr: {deck.deck.short_name} is not bracket 5; EDHTop16 covers cEDH only", file=ctx.err
                )
            data = gather(
                deck.deck,
                sources,
                cache_dir(ctx.env),
                now=ctx.now,
                refresh=args.refresh,
                on_progress=progress,
            )
            report = analyse(deck.deck, data, read_profile(deck.folder), db)
            results.append((deck, data, report))
            if args.to_board:
                _to_board(deck, data, db, ctx)
    for _, _, report in results:
        _warn_sources(report, ctx.err)
    _print_results(args, results, ctx.out)
    unavailable = any(s.source is AnswerSource.UNAVAILABLE for _, _, r in results for s in r.sources)
    return EXIT_FAILURE if unavailable else EXIT_OK


def meta_sources(http: httpx.Client, ctx: Context, user: str, settings: MetaSettings) -> MetaSources:
    """EDHTop16, Archidekt search and Spellbook clients over one HTTP client, each at its own pace."""
    return shared_meta_sources(http, ctx.pacer_factory, user, settings)


def _print_results(
    args: argparse.Namespace, results: Sequence[tuple[StoredDeck, MetaData, MetaReport]], out: TextIO
) -> None:
    if args.json:
        checks = [check_card(name, s.deck, data) for s, data, _ in results for name in args.card]
        json.dump(
            {"reports": [report_json(r) for _, _, r in results], "cards": [check_json(c) for c in checks]},
            out,
            ensure_ascii=False,
            indent=2,
        )
        out.write("\n")
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


def _decks(space: Workspace, query: str | None) -> list[StoredDeck]:
    if query is not None:
        return [one_deck(space, query)]
    chosen = [s for s in space.load_all() if s.deck.bracket == CEDH_BRACKET]
    if not chosen:
        raise UsageError("no bracket 5 decks; name a deck to check it anyway")
    return chosen


def _to_board(deck: StoredDeck, data: MetaData, db: CardDb, ctx: Context) -> None:
    added = compile_ideas(deck, data, db, now=ctx.now)
    print(f"{deck.deck.short_name} board: {ideas_summary(added)}", file=ctx.err)


def _warn_sources(report: MetaReport, err: TextIO) -> None:
    for state in report.sources:
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


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")  # noqa: PLR2004 — English ordinals
    return f"{n}{suffix}"


def print_report(report: MetaReport, out: TextIO) -> None:
    sample = report.sample
    print(f"{report.deck}: {report.commander}, events since {report.since}", file=out)
    print(f"  EDHTop16: {commander_url(report.commander)}", file=out)
    if not sample.known:
        print("  EDHTop16 has no lists for this commander", file=out)
    else:
        print(
            f"  {sample.entries} entries, {sample.with_lists} with decklists, {sample.top_cuts} top cuts; "
            f"reading {sample.performing} {sample.basis.value}",
            file=out,
        )
    if sample.known and sample.thin:
        reason = "few top cuts" if sample.basis is Basis.ALL else "few lists"
        print(f"  thin data ({reason}): read shares as hints, not evidence", file=out)
    _print_missing(report, out)
    _print_spicy(report, out)
    _print_outliers(report, out)
    _print_threats(report, out)
    _print_own_combos(report, out)
    _print_combos(report, out)
    _print_field(report, out)
    _print_peers(report, out)


def _print_missing(report: MetaReport, out: TextIO) -> None:
    style = report.style.label
    weighed = f"; {style} plan first" if style else ""
    print(f"\nStaples you don't play (share of lists read · recent Archidekt lists{weighed})", file=out)
    if not report.missing:
        print("  none", file=out)
    for card in report.missing:
        note = "  [on your Sideboard or Maybeboard]" if card.flagged else ""
        if card.fit is Fit.FAVOURED:
            note += f"  [fits {style}]"
        elif card.fit is Fit.OFF_PLAN:
            note += f"  [off-plan for {style}]"
        print(
            f"  {_percent(card.share)} · {card.peer_lists}/{len(report.peers)}  {card.name}{note}", file=out
        )


def _print_spicy(report: MetaReport, out: TextIO) -> None:
    print("\nSpicy picks in those lists (spice 3-4: new or rarely played)", file=out)
    if not report.spicy:
        print("  none", file=out)
    for pick in report.spicy:
        best = pick.best
        event = best.tournament
        name = " ".join(event.name.split())
        place = f"{_ordinal(best.standing)} of {event.size}, {name[:EVENT_NAME_WIDTH]} ({event.held_on})"
        print(f"  spice {pick.spice}  {pick.lists} list(s)  {pick.name}  best: {place}", file=out)


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
    for combo in report.combos:
        players = ", ".join(
            f"{short_commander_name(name)} {round(share * 100)}%" for name, share in combo.played_by[:3]
        )
        results = "; ".join(combo.results[:2])
        print(
            f"  {_percent(combo.exposure / total)}  {' + '.join(combo.pieces)}: {results}  ({players})",
            file=out,
        )
        if combo.requirements:
            print(f"        also needs: {'; '.join(combo.requirements)}", file=out)
        if not combo.answers:
            racing = f" ({report.style.label}: racing, not answering)" if report.style.races else ""
            print(f"        no answer in the deck{racing}", file=out)
            continue
        for role in AnswerRole:
            answers = [a for a in combo.answers if a.role is role]
            if not answers:
                continue
            shown = [_answer_shown(a) for a in answers]
            print(f"        {role.value}: {_names(shown, COMBO_ANSWERS_SHOWN)}", file=out)


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
        f"  EDHTop16: in {check.performing_with} of {check.performing} {report.sample.basis.value}, "
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
    if check.spice is not None:
        print(f"  spice: up to {check.spice} of 4", file=out)
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
        # A deck that races counts no unanswered combo as a gap (ADR 0022).
        open_combos = "racing" if r.style.races else str(sum(1 for c in r.combos if not c.answers))
        print(
            f"{r.deck[:10]:<10}  {r.sample.with_lists:>5}  {r.sample.top_cuts:>8}  {r.sample.performing:>4}"
            f"  {len(r.missing):>7}  {len(r.threats):>7}  {open_combos:>17}{thin}",
            file=out,
        )


def report_json(report: MetaReport) -> dict[str, object]:
    sample = report.sample
    return {
        "deck": report.deck,
        "commander": report.commander,
        "since": report.since.isoformat(),
        "sample": {
            "known": sample.known,
            "entries": sample.entries,
            "withLists": sample.with_lists,
            "topCuts": sample.top_cuts,
            "read": sample.performing,
            "basis": sample.basis.name.lower(),
            "thin": sample.thin,
        },
        "playStyle": [style.value for style in report.style.styles],
        "racing": report.style.races,
        "missing": [{**_share_json(c, "onSideboard"), "fit": c.fit.value or None} for c in report.missing],
        "spicy": [
            {
                "name": p.name,
                "spice": p.spice,
                "lists": p.lists,
                "best": {
                    "standing": p.best.standing,
                    "size": p.best.tournament.size,
                    "event": p.best.tournament.name,
                    "date": p.best.tournament.held_on.isoformat(),
                    "decklist": p.best.decklist_url,
                },
            }
            for p in report.spicy
        ],
        "outliers": [_share_json(c, "kept") for c in report.outliers],
        "keyCards": [{"name": k.name, "reasons": list(k.reasons)} for k in report.key_cards],
        "threats": [
            {
                "name": t.name,
                "kinds": [k.label for k in t.kinds],
                "hits": list(t.hits),
                "exposure": round(t.exposure, 5),
                "playedBy": [{"commander": n, "share": round(s, 4)} for n, s in t.played_by],
            }
            for t in report.threats
        ],
        "combos": [
            {
                "id": c.id,
                "url": c.url,
                "pieces": list(c.pieces),
                "requirements": list(c.requirements),
                "results": list(c.results),
                "exposure": round(c.exposure, 5),
                "playedBy": [
                    {"commander": n, "short": short_commander_name(n), "share": round(s, 4)}
                    for n, s in c.played_by
                ],
                "answers": [
                    {"card": a.card, "how": a.how, "role": a.role.value, "catch": a.catch} for a in c.answers
                ],
            }
            for c in report.combos
        ],
        "ownCombos": [
            {
                "id": c.id,
                "url": c.url,
                "pieces": list(c.pieces),
                "results": list(c.results),
                "answers": [
                    {
                        "card": a.card,
                        "how": a.how,
                        "role": a.role.value,
                        "catch": a.catch,
                        "exposure": round(a.exposure, 5),
                        "playedBy": [
                            {"commander": n, "short": short_commander_name(n), "share": round(s, 4)}
                            for n, s in a.played_by
                        ],
                    }
                    for a in c.answers
                ],
            }
            for c in report.own_combos
        ],
        "field": [
            {
                "commander": s.name,
                "entries": s.entries,
                "topCuts": s.top_cuts,
                "conversion": round(s.conversion, 4),
                "metaShare": round(s.meta_share, 5),
            }
            for s in report.field
        ],
        "peers": [
            {
                "id": p.id,
                "name": p.name,
                "views": p.views,
                "updatedAt": p.updated_at.isoformat(),
                "url": p.url,
            }
            for p in report.peers
        ],
        "peersSkipped": report.peers_skipped,
        "sources": [
            {
                "name": s.name,
                "source": s.source.value,
                "fetchedAt": s.fetched_at.isoformat() if s.fetched_at else None,
                "error": s.error,
            }
            for s in report.sources
        ],
    }


def _share_json(card: CardShare, flag: str) -> dict[str, object]:
    return {
        "name": card.name,
        "lists": card.lists,
        "share": round(card.share, 4),
        "peerLists": card.peer_lists,
        "peerShare": round(card.peer_share, 4),
        "spice": card.spice,
        flag: card.flagged,
    }


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
        "spice": check.spice,
        "metaLists": check.meta_lists,
        "metaListsWith": check.meta_lists_with,
    }
