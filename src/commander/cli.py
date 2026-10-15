"""`cmdr` entry point: argument parsing, output formatting and exit codes."""

import argparse
import json
import logging
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TextIO

import httpx

from commander import __version__
from commander.analysis import ANALYZED_ROLES, CURVE_TOP, DeckAnalysis, analyze
from commander.archidekt import ArchidektClient, ArchidektError
from commander.carddb import CARD_DB_FILE, CardDataError, CardDataMeta, CardDb, Role
from commander.config import (
    ENV_CACHE,
    ENV_CONFIG,
    ENV_USER,
    ENV_WORKSPACE,
    ConfigError,
    cache_dir,
    config_path,
    environment,
    read_config,
    resolve_user,
    resolve_workspace,
    write_config,
)
from commander.diff import DeckDiff
from commander.net import Fetcher, NetworkError, make_client
from commander.pricing import DeckPrice, PriceSource, format_eur, price_deck
from commander.scryfall import CardRefresh, RefreshStatus, ScryfallClient, ScryfallError, refresh_card_db
from commander.store import StoredDeck, Workspace
from commander.sync import SyncResult, SyncStatus, sync_decks

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

STALE_CARD_DATA = timedelta(days=3)
DEFAULT_TOP = 15

ClientFactory = Callable[[], httpx.Client]
Clock = Callable[[], datetime]

_DESCRIPTION = """\
Track your Archidekt Commander decks and research changes to them.

examples:
  cmdr init --user MAKKIS --workspace decks   save your username and deck folder
  cmdr sync                                   pull every public Commander deck, show what changed
  cmdr sync --dry-run                         show what would change without writing anything
  cmdr list --json                            local decks as JSON
  cmdr analyze                                curve, lands and roles of every deck side by side
  cmdr analyze magda --details                one deck in full, with role differences card by card
  cmdr price                                  value of every deck in EUR
  cmdr price 10004639 --top 0                 every card of one deck, most valuable first
"""

_EPILOG = f"""\
settings come from flags, then environment ({ENV_USER}, {ENV_WORKSPACE}, {ENV_CONFIG}),
then the config file (default %APPDATA%\\Commander\\config.toml).
card data from Scryfall is kept in %LOCALAPPDATA%\\Commander (or {ENV_CACHE}); `cmdr sync` refreshes it.
exit codes: 0 success, 1 failure, 2 usage error, 130 interrupted.
"""


def main(  # noqa: PLR0911 — one exit code per failure class, mapped in one place
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    client_factory: ClientFactory = make_client,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    now: Clock = lambda: datetime.now(UTC),
) -> int:
    out = stdout or _utf8(sys.stdout)
    err = stderr or _utf8(sys.stderr)
    parser = _build_parser()
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list:
        parser.print_usage(err)
        return EXIT_USAGE
    try:
        args = parser.parse_args(args_list)
    except SystemExit as exit_:
        return exit_.code if isinstance(exit_.code, int) else EXIT_USAGE
    logging.basicConfig(
        stream=err,
        level=logging.DEBUG if args.verbose > 1 else logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
        force=True,
    )
    context = _Context(env or environment(), client_factory, out, err, now)
    command: Callable[[argparse.Namespace, _Context], int] | None = getattr(args, "handler", None)
    if command is None:
        parser.print_usage(err)
        return EXIT_USAGE
    try:
        return command(args, context)
    except (ConfigError, UsageError) as error:
        print(f"cmdr: {error}", file=err)
        return EXIT_USAGE
    except (NetworkError, ArchidektError, ScryfallError, CardDataError, OSError) as error:
        print(f"cmdr: {error}", file=err)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        print("cmdr: interrupted", file=err)
        return EXIT_INTERRUPTED


class UsageError(Exception):
    """The command line names something that does not exist or is ambiguous."""


class _Context:
    def __init__(
        self, env: Mapping[str, str], client_factory: ClientFactory, out: TextIO, err: TextIO, now: Clock
    ) -> None:
        self.env = env
        self.client_factory = client_factory
        self.out = out
        self.err = err
        self.now = now


class _CardState(Enum):
    SKIPPED = "skipped"
    CURRENT = "current"
    UPDATED = "updated"
    FAILED = "failed"


@dataclass(slots=True, frozen=True)
class _CardStatus:
    state: _CardState
    detail: str


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cmdr",
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"cmdr {__version__}")
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="more detail on stderr (-vv for debug)"
    )
    parser.add_argument("--config", help="config file to use instead of the default")
    commands = parser.add_subparsers(title="commands", metavar="<command>")

    init = commands.add_parser("init", help="save your username and deck folder to the config file")
    init.add_argument("--user", required=True, help="Archidekt username")
    init.add_argument("--workspace", required=True, help="folder that holds the deck folders")
    init.add_argument("-f", "--force", action="store_true", help="replace an existing config file")
    init.set_defaults(handler=_cmd_init)

    sync = commands.add_parser("sync", help="pull your Commander decks from Archidekt")
    sync.add_argument("--user", help="Archidekt username")
    sync.add_argument("--workspace", help="folder that holds the deck folders")
    sync.add_argument(
        "-f", "--force", action="store_true", help="refetch every deck and rebuild the card data"
    )
    sync.add_argument("-n", "--dry-run", action="store_true", help="report changes without writing files")
    sync.add_argument("--no-cards", action="store_true", help="skip the Scryfall card data refresh")
    sync.add_argument("--json", action="store_true", help="print results as JSON")
    sync.set_defaults(handler=_cmd_sync)

    list_ = commands.add_parser("list", help="show the decks saved locally")
    list_.add_argument("--workspace", help="folder that holds the deck folders")
    list_.add_argument("--json", action="store_true", help="print the decks as JSON")
    list_.set_defaults(handler=_cmd_list)

    analyze_ = commands.add_parser("analyze", help="mana curve, card types, colours and roles")
    analyze_.add_argument("deck", nargs="?", help="deck id, or part of its name or commander; omit for all")
    analyze_.add_argument("--workspace", help="folder that holds the deck folders")
    analyze_.add_argument(
        "-d", "--details", action="store_true", help="list cards where your categories and the tags differ"
    )
    analyze_.add_argument("--json", action="store_true", help="print the analysis as JSON")
    analyze_.set_defaults(handler=_cmd_analyze)

    price = commands.add_parser("price", help="deck value in EUR (Cardmarket, via Scryfall)")
    price.add_argument("deck", nargs="?", help="deck id, or part of its name or commander; omit for all")
    price.add_argument("--workspace", help="folder that holds the deck folders")
    price.add_argument(
        "-t", "--top", type=_non_negative, default=DEFAULT_TOP, help="cards to list for one deck (0 = all)"
    )
    price.add_argument("--json", action="store_true", help="print prices as JSON (amounts in euro cents)")
    price.set_defaults(handler=_cmd_price)
    return parser


def _cmd_init(args: argparse.Namespace, ctx: _Context) -> int:
    path = config_path(args.config, ctx.env)
    if path.exists() and not args.force:
        print(f"cmdr: {path} already exists; pass --force to replace it", file=ctx.err)
        return EXIT_FAILURE
    workspace = Path(args.workspace).resolve()
    write_config(path, args.user, workspace)
    print(f"saved {path}", file=ctx.out)
    return EXIT_OK


def _cmd_sync(args: argparse.Namespace, ctx: _Context) -> int:
    file = read_config(config_path(args.config, ctx.env))
    user = resolve_user(args.user, ctx.env, file)
    workspace = Workspace(resolve_workspace(args.workspace, ctx.env, file))
    progress = _progress_printer(ctx.err)
    with ctx.client_factory() as http:
        fetcher = Fetcher(http)
        # The card download is independent of the decks, so it runs while they sync.
        cards_job: Future[CardRefresh] | None = None
        if not args.dry_run and not args.no_cards:
            scryfall, folder = ScryfallClient(fetcher), cache_dir(ctx.env)
            cards_job = _in_background(
                lambda: refresh_card_db(scryfall, folder, now=ctx.now, force=args.force, on_progress=progress)
            )
        results = sync_decks(
            ArchidektClient(fetcher),
            workspace,
            user,
            force=args.force,
            dry_run=args.dry_run,
            on_progress=progress,
        )
        cards = _card_status(cards_job, dry_run=args.dry_run)
    if args.json:
        document = {
            "decks": [_result_json(r) for r in results],
            "cardData": {"status": cards.state.value, "detail": cards.detail},
        }
        json.dump(document, ctx.out, ensure_ascii=False, indent=2)
        ctx.out.write("\n")
    else:
        _print_sync_report(results, ctx.out, dry_run=args.dry_run)
        print(f"card data: {cards.state.value}, {cards.detail}", file=ctx.out)
    failed = any(r.status is SyncStatus.FAILED for r in results) or cards.state is _CardState.FAILED
    return EXIT_FAILURE if failed else EXIT_OK


def _in_background[T](work: Callable[[], T]) -> Future[T]:
    """Run work on a daemon thread, so Ctrl-C ends the process without waiting for it to finish."""
    future: Future[T] = Future()

    def run() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(work())
        except BaseException as error:  # noqa: BLE001 — handed to the caller through the future
            future.set_exception(error)

    threading.Thread(target=run, name="card-data", daemon=True).start()
    return future


def _card_status(job: Future[CardRefresh] | None, *, dry_run: bool) -> _CardStatus:
    if job is None:
        return _CardStatus(_CardState.SKIPPED, "dry run" if dry_run else "--no-cards")
    try:
        refresh = job.result()
    except (NetworkError, ScryfallError, CardDataError, OSError) as error:
        logging.getLogger(__name__).info("card data refresh failed: %s", error)
        return _CardStatus(_CardState.FAILED, f"{error}; the previous card data is kept")
    source = f"Scryfall data of {_day(refresh.meta.cards_updated_at)}"
    if refresh.status is RefreshStatus.CURRENT:
        return _CardStatus(_CardState.CURRENT, source)
    return _CardStatus(_CardState.UPDATED, f"{source}, {refresh.printings or 0:,} printings")


def _cmd_analyze(args: argparse.Namespace, ctx: _Context) -> int:
    decks = _selected_decks(args, ctx)
    with _open_card_db(ctx) as db:
        analyses = [analyze(s.deck, db.resolve(s.deck.main_cards), db.available_roles) for s in decks]
    if args.json:
        json.dump([_analysis_json(a) for a in analyses], ctx.out, ensure_ascii=False, indent=2)
        ctx.out.write("\n")
    elif args.deck:
        for analysis in analyses:
            _print_analysis(analysis, ctx.out, details=args.details)
    else:
        _print_analysis_table(analyses, ctx.out)
    return EXIT_OK


def _cmd_price(args: argparse.Namespace, ctx: _Context) -> int:
    decks = _selected_decks(args, ctx)
    with _open_card_db(ctx) as db:
        prices = [price_deck(s.deck, db.resolve(s.deck.main_cards)) for s in decks]
        meta = db.meta()
    if args.json:
        json.dump([_price_json(p) for p in prices], ctx.out, ensure_ascii=False, indent=2)
        ctx.out.write("\n")
        return EXIT_OK
    print(f"Cardmarket prices in EUR, from Scryfall data of {_day(meta.cards_updated_at)}", file=ctx.out)
    if args.deck:
        for price in prices:
            _print_deck_price(price, ctx.out, top=args.top)
    else:
        _print_price_table(prices, ctx.out)
    return EXIT_OK


def _selected_decks(args: argparse.Namespace, ctx: _Context) -> list[StoredDeck]:
    file = read_config(config_path(args.config, ctx.env))
    workspace = Workspace(resolve_workspace(args.workspace, ctx.env, file))
    if args.deck is None:
        decks = workspace.load_all()
        if not decks:
            raise UsageError(f"no decks in {workspace.root}; run `cmdr sync` first")
        return decks
    if not args.deck.strip():
        raise UsageError("the deck to look up is empty")
    matches = workspace.find(args.deck)
    if not matches:
        raise UsageError(f"no deck matches {args.deck!r}; `cmdr list` shows them all")
    if len(matches) > 1:
        names = "; ".join(f"{s.deck.id} {s.deck.name}" for s in matches)
        raise UsageError(f"{args.deck!r} matches {len(matches)} decks ({names}); use the deck id")
    return matches


def _open_card_db(ctx: _Context) -> CardDb:
    db = CardDb(cache_dir(ctx.env) / CARD_DB_FILE)
    _warn_if_stale(db.meta(), ctx)
    return db


def _warn_if_stale(meta: CardDataMeta, ctx: _Context) -> None:
    try:
        updated = datetime.fromisoformat(meta.cards_updated_at)
    except ValueError:
        return
    if updated.tzinfo is not None and ctx.now() - updated > STALE_CARD_DATA:
        print(
            f"cmdr: card data is from {_day(meta.cards_updated_at)}; run `cmdr sync` for fresh prices",
            file=ctx.err,
        )


def _day(timestamp: str) -> str:
    # Scryfall timestamps are ISO 8601 in UTC; the date part is all a reader needs.
    return timestamp[:10]


def _cmd_list(args: argparse.Namespace, ctx: _Context) -> int:
    file = read_config(config_path(args.config, ctx.env))
    workspace = Workspace(resolve_workspace(args.workspace, ctx.env, file))
    stored = workspace.load_all()
    if args.json:
        rows = [
            {
                "id": s.deck.id,
                "name": s.deck.name,
                "commanders": list(s.deck.commanders),
                "bracket": s.deck.bracket,
                "cards": s.deck.main_count,
                "updatedAt": s.deck.updated_at,
                "folder": str(s.folder),
            }
            for s in stored
        ]
        json.dump(rows, ctx.out, ensure_ascii=False, indent=2)
        ctx.out.write("\n")
        return EXIT_OK
    for s in stored:
        bracket = s.deck.bracket if s.deck.bracket is not None else "-"
        commanders = " + ".join(s.deck.commanders) or "(no commander)"
        print(f"{s.deck.id}\tB{bracket}\t{s.deck.name}\t{commanders}", file=ctx.out)
    if not stored:
        print(f"no decks in {workspace.root}; run `cmdr sync`", file=ctx.err)
    return EXIT_OK


_ROLE_HEADINGS: Mapping[Role, str] = {
    Role.RAMP: "ramp",
    Role.DRAW: "draw",
    Role.REMOVAL: "removal",
    Role.BOARD_WIPE: "wipes",
    Role.TUTOR: "tutors",
    Role.PROTECTION: "protect",
    Role.RECURSION: "recursion",
    Role.COUNTERSPELL: "counters",
}


def _print_analysis(analysis: DeckAnalysis, out: TextIO, *, details: bool) -> None:
    deck = analysis.deck
    commanders = " + ".join(deck.commanders) or "no commander"
    bracket = f"bracket {deck.bracket} on Archidekt" if deck.bracket is not None else "no bracket set"
    average = f"{analysis.average_mana_value:.2f}" if analysis.average_mana_value is not None else "-"
    print(f"{deck.name} ({deck.id})", file=out)
    print(f"{commanders}; {bracket}", file=out)
    print(f"{analysis.cards} cards, {analysis.lands} lands, average mana value {average} (nonland)", file=out)
    print("\nmana curve (nonland)", file=out)
    for value, count in enumerate(analysis.curve):
        label = f"{value}+" if value == CURVE_TOP else str(value)
        print(f"  {label:>2}  {count:>3}  {'#' * count}".rstrip(), file=out)
    types = ", ".join(f"{name} {count}" for name, count in analysis.types.items() if count)
    pips = ", ".join(f"{color} {count}" for color, count in analysis.pips.items() if count)
    print(f"\ntypes: {types or '-'}", file=out)
    print(f"pips: {pips or '-'}", file=out)
    print(f"\n  {'role':<12} {'yours':>5} {'tags':>5}", file=out)
    for role in analysis.roles:
        print(f"  {role.role.value:<12} {_count(role.yours):>5} {_count(role.tagged):>5}", file=out)
        if details and role.only_yours:
            print(f"      only yours: {', '.join(role.only_yours)}", file=out)
        if details and role.only_tagged:
            print(f"      only tags:  {', '.join(role.only_tagged)}", file=out)
    print(f"\ngame changers: {', '.join(analysis.game_changers) or 'none'}", file=out)
    if analysis.not_legal:
        print(f"not legal in Commander: {', '.join(analysis.not_legal)}", file=out)
    if analysis.unknown:
        print(f"not in the card data: {', '.join(analysis.unknown)}", file=out)
    print(file=out)


def _print_analysis_table(analyses: Sequence[DeckAnalysis], out: TextIO) -> None:
    roles = "".join(f" {_ROLE_HEADINGS[role]}" for role in ANALYZED_ROLES)
    print(f"{'id':>9} {'lands':>5} {'avg mv':>6}{roles} {'gc':>3}  name", file=out)
    for a in analyses:
        average = f"{a.average_mana_value:.2f}" if a.average_mana_value is not None else "-"
        counts = "".join(f" {_count(r.tagged):>{len(_ROLE_HEADINGS[r.role])}}" for r in a.roles)
        print(
            f"{a.deck.id:>9} {a.lands:>5} {average:>6}{counts} {len(a.game_changers):>3}  {a.deck.name}",
            file=out,
        )
    print(
        "role counts come from Scryfall tags; `cmdr analyze <deck>` sets them against your categories",
        file=out,
    )


def _analysis_json(analysis: DeckAnalysis) -> dict[str, object]:
    return {
        "id": analysis.deck.id,
        "name": analysis.deck.name,
        "commanders": list(analysis.deck.commanders),
        "bracket": analysis.deck.bracket,
        "cards": analysis.cards,
        "lands": analysis.lands,
        "averageManaValue": analysis.average_mana_value,
        "curve": list(analysis.curve),
        "types": dict(analysis.types),
        "pips": dict(analysis.pips),
        "roles": [
            {
                "role": r.role.value,
                "yours": r.yours,
                "tagged": r.tagged,
                "onlyYours": list(r.only_yours),
                "onlyTagged": list(r.only_tagged),
            }
            for r in analysis.roles
        ],
        "gameChangers": list(analysis.game_changers),
        "notLegal": list(analysis.not_legal),
        "unknown": list(analysis.unknown),
    }


def _print_deck_price(price: DeckPrice, out: TextIO, *, top: int) -> None:
    deck = price.deck
    print(f"\n{deck.name} ({deck.id})", file=out)
    print(
        f"value {format_eur(price.total_cents)}; with the cheapest printings "
        f"{format_eur(price.cheapest_total_cents)}",
        file=out,
    )
    shown = price.cards if top == 0 else price.cards[:top]
    for card in shown:
        if card.source is PriceSource.NONE:
            continue
        mark = " ~" if card.source is PriceSource.CHEAPEST else ""
        print(f"  {format_eur(card.total_cents):>10}  {card.quantity:>2}x {card.name}{mark}", file=out)
    if len(shown) < len(price.cards):
        print(f"  ... {len(price.cards) - len(shown)} more; --top 0 lists them all", file=out)
    if price.estimated:
        print("~ this printing has no price, so the card's cheapest printing is used", file=out)
    if price.unpriced:
        print(f"no price at all: {', '.join(price.unpriced)}", file=out)


def _print_price_table(prices: Sequence[DeckPrice], out: TextIO) -> None:
    print(f"{'value':>11} {'cheapest':>11}  deck", file=out)
    for price in sorted(prices, key=lambda p: -p.total_cents):
        print(
            f"{format_eur(price.total_cents):>11} {format_eur(price.cheapest_total_cents):>11}  "
            f"{price.deck.name} ({price.deck.id})",
            file=out,
        )
    total = sum(p.total_cents for p in prices)
    cheapest = sum(p.cheapest_total_cents for p in prices)
    print(f"{format_eur(total):>11} {format_eur(cheapest):>11}  total", file=out)


def _price_json(price: DeckPrice) -> dict[str, object]:
    return {
        "id": price.deck.id,
        "name": price.deck.name,
        "totalCents": price.total_cents,
        "cheapestTotalCents": price.cheapest_total_cents,
        "cards": [
            {
                "name": c.name,
                "quantity": c.quantity,
                "unitCents": c.unit_cents,
                "totalCents": c.total_cents,
                "cheapestCents": c.cheapest_cents,
                "source": c.source.value,
            }
            for c in price.cards
        ],
    }


def _count(value: int | None) -> str:
    return "-" if value is None else str(value)


def _non_negative(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def _print_sync_report(results: list[SyncResult], out: TextIO, *, dry_run: bool) -> None:
    order = {status: index for index, status in enumerate(SyncStatus)}
    for result in sorted(results, key=lambda r: (order[r.status], r.name.casefold())):
        if result.status is SyncStatus.UNCHANGED:
            continue
        print(f"{result.status.value:<9} {result.deck_id:>9}  {result.name}", file=out)
        if result.error:
            print(f"    {result.error}", file=out)
        if result.diff is not None and result.status is not SyncStatus.NEW:
            for line in _diff_lines(result.diff):
                print(f"    {line}", file=out)
    counts = {status: sum(1 for r in results if r.status is status) for status in SyncStatus}
    summary = ", ".join(f"{count} {status.value}" for status, count in counts.items() if count)
    print(f"{summary or 'no decks'}{' (dry run, nothing written)' if dry_run else ''}", file=out)


def _diff_lines(diff: DeckDiff) -> list[str]:
    lines: list[str] = []
    if diff.old_name:
        lines.append(f"renamed from {diff.old_name}")
    lines += [f"+ {c.new_quantity} {c.name}" for c in diff.added]
    lines += [f"- {c.old_quantity} {c.name}" for c in diff.removed]
    lines += [f"~ {c.name}: {c.old_quantity} -> {c.new_quantity}" for c in diff.changed]
    return lines


def _result_json(result: SyncResult) -> dict[str, object]:
    row: dict[str, object] = {"id": result.deck_id, "name": result.name, "status": result.status.value}
    if result.error:
        row["error"] = result.error
    if result.diff is not None:
        row["renamedFrom"] = result.diff.old_name
        row["added"] = [{"name": c.name, "quantity": c.new_quantity} for c in result.diff.added]
        row["removed"] = [{"name": c.name, "quantity": c.old_quantity} for c in result.diff.removed]
        row["changed"] = [
            {"name": c.name, "from": c.old_quantity, "to": c.new_quantity} for c in result.diff.changed
        ]
    return row


def _progress_printer(err: TextIO) -> Callable[[str], None]:
    if not err.isatty():
        return lambda _: None
    return lambda message: print(message, file=err)


def _utf8(stream: TextIO) -> TextIO:
    # Deck names carry emoji; a redirected Windows stream would otherwise use the ANSI codepage and crash.
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")
    return stream


if __name__ == "__main__":
    raise SystemExit(main())
