"""`cmdr` entry point: argument parsing, output formatting and exit codes."""

import argparse
import getpass
import json
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import BinaryIO, TextIO

from commander import __version__
from commander.analysis import ANALYZED_ROLES, CURVE_TOP, DeckAnalysis, analyze
from commander.apply import (
    NotYourDeckError,
    Resolution,
    SaveError,
    SaveResult,
    apply_plan,
)
from commander.archidekt import ArchidektClient, ArchidektError
from commander.archidekt_edit import ArchidektAuth, ArchidektEditor, AuthError
from commander.background import in_background
from commander.board import BoardCard, BoardError, BoardService, Origin, feed_suggestions, read_board
from commander.boardsave import pick_up_aside
from commander.bracket import CACHE_FOLDER as SPELLBOOK_CACHE_FOLDER
from commander.bracket import BracketReport, EstimateResult, bracket_cards, estimate_cache, estimate_decks
from commander.bracketcheck import BracketCheck
from commander.carddb import CARD_DB_FILE, CardDataError, CardDb, Role
from commander.cli_app import add_app_command
from commander.cli_board import add_board_commands
from commander.cli_cards import add_card_commands
from commander.cli_meta import add_meta_command, meta_sources
from commander.cli_output import (
    price_text,
    print_apply_outcome,
    print_resolution,
    progress_printer,
    raise_json,
    raise_text,
)
from commander.cli_profile import add_profile_commands
from commander.cli_toget import add_toget_commands
from commander.clibase import (
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    ClientFactory,
    Clock,
    Context,
    PacerFactory,
    PasswordPrompt,
    UsageError,
    day,
    one_deck,
    open_card_db,
    selected_decks,
)
from commander.config import (
    ENV_CACHE,
    ENV_CONFIG,
    ENV_USER,
    ENV_WORKSPACE,
    ConfigError,
    MetaSettings,
    cache_dir,
    config_path,
    environment,
    read_config,
    resolve_user,
    resolve_workspace,
    write_config,
)
from commander.credentials import CredentialError, KeyringTokenStore, TokenStore
from commander.deckcache import Answer, AnswerSource
from commander.deckprofile import ProfileError, read_profile
from commander.deckthemes import merge_themes
from commander.diff import DeckDiff
from commander.edhtop16 import Edhtop16Error
from commander.ideas import compile_ideas, ideas_summary
from commander.labels import LabelError
from commander.metadata import Progress, gather_adjacent, gather_for
from commander.models import Deck
from commander.net import Fetcher, NetworkError, Pacer, make_client
from commander.plan import EXAMPLE as PLAN_EXAMPLE
from commander.plan import PlanError, read_plan
from commander.pricing import DeckPrice, PriceSource, format_eur, price_deck
from commander.scryfall import CardRefresh, RefreshStatus, ScryfallClient, ScryfallError, refresh_card_db
from commander.spellbook import BracketTag, Combo, SpellbookClient, SpellbookError, TwoCard, Variant
from commander.store import Workspace, WorkspaceError
from commander.suggest import CACHE_FOLDER as COMBOS_CACHE_FOLDER
from commander.suggest import (
    DEFAULT_COUNT,
    DEFAULT_TARGETS,
    RoleIdea,
    RoleTarget,
    Suggestions,
    combos_cache,
    find_combos,
    options_for,
    suggest,
)
from commander.sync import SyncResult, SyncStatus, sync_decks

DEFAULT_TOP = 15
MAX_PASSWORD_BYTES = 1024


_DEFAULT_TARGETS_TEXT = ", ".join(
    f"{role.value.replace(' ', '_')} {n}" for role, n in DEFAULT_TARGETS.items()
)

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
  cmdr bracket                                estimated bracket of every deck beside the one on Archidekt
  cmdr bracket magda --details                game changers, tutors and combos of one deck
  cmdr suggest                                combo pieces and roles short of target, every deck
  cmdr suggest magda --max-price 5            ideas and cut candidates for one deck, cards up to EUR 5
  cmdr login                                  sign in to Archidekt once, so `apply --save` can edit decks
  cmdr apply plan.toml                        preview a plan's changes against the deck on Archidekt
  cmdr apply plan.toml --save                 make them, after saving a snapshot of the deck there
"""

_EPILOG = f"""\
settings come from flags, then environment ({ENV_USER}, {ENV_WORKSPACE}, {ENV_CONFIG}),
then the config file (default %APPDATA%\\Commander\\config.toml).
card data from Scryfall is kept in %LOCALAPPDATA%\\Commander (or {ENV_CACHE}); `cmdr sync` refreshes it.
`cmdr bracket` and `cmdr suggest` ask Commander Spellbook about each deck and keep each answer there
for 7 days.
role targets for `cmdr suggest` (default {_DEFAULT_TARGETS_TEXT}) can be changed
in a [targets] table in the config file.
`cmdr login` keeps an Archidekt login token in Windows Credential Manager, never the password.
exit codes: 0 success, 1 failure, 2 usage error, 130 interrupted.
"""

_APPLY_DESCRIPTION = f"""\
Preview a plan's changes to one deck, and with --save make them on Archidekt.

a plan file looks like this:

{PLAN_EXAMPLE}

add: the deck ends with at least that many copies (default 1), in the cheapest EUR printing
unless set says which, filed under its card type unless category says otherwise.
remove: the deck ends with none of the card, or with keep copies.
a plan the deck already meets changes nothing, so applying it twice is safe.
--save first saves a snapshot of the deck on Archidekt, then reads the deck back to check it.
"""


def main(  # noqa: PLR0911 — one exit code per failure class, mapped in one place
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    client_factory: ClientFactory = make_client,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    now: Clock = lambda: datetime.now(UTC),
    token_store: TokenStore | None = None,
    prompt_password: PasswordPrompt = getpass.getpass,
    stdin: BinaryIO | None = None,
    pacer_factory: PacerFactory = Pacer,
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
    context = Context(
        env or environment(),
        client_factory,
        out,
        err,
        now,
        token_store or KeyringTokenStore(),
        prompt_password,
        stdin or sys.stdin.buffer,
        pacer_factory,
    )
    command: Callable[[argparse.Namespace, Context], int] | None = getattr(args, "handler", None)
    if command is None:
        parser.print_usage(err)
        return EXIT_USAGE
    try:
        return command(args, context)
    except (ConfigError, UsageError, PlanError, NotYourDeckError, BoardError) as error:
        print(f"cmdr: {error}", file=err)
        return EXIT_USAGE
    except (
        NetworkError,
        ArchidektError,
        AuthError,
        CredentialError,
        SaveError,
        ScryfallError,
        SpellbookError,
        Edhtop16Error,
        CardDataError,
        WorkspaceError,
        LabelError,
        OSError,
    ) as error:
        print(f"cmdr: {error}", file=err)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        print("cmdr: interrupted", file=err)
        return EXIT_INTERRUPTED


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
    sync.add_argument(
        "--no-ideas",
        action="store_true",
        help="skip compiling each deck's ideas from the meta and recent Archidekt decks",
    )
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

    bracket = commands.add_parser("bracket", help="bracket estimate, game changers and combos")
    bracket.add_argument("deck", nargs="?", help="deck id, or part of its name or commander; omit for all")
    bracket.add_argument("--workspace", help="folder that holds the deck folders")
    bracket.add_argument(
        "-d", "--details", action="store_true", help="show what each combo does, with a link to it"
    )
    bracket.add_argument(
        "-r",
        "--refresh",
        action="store_true",
        help="ask Commander Spellbook again even if a recent answer is cached",
    )
    bracket.add_argument("--json", action="store_true", help="print the reports as JSON")
    bracket.set_defaults(handler=_cmd_bracket)
    _add_suggest_arguments(
        commands.add_parser("suggest", help="cards to add for combos and short roles, cuts to weigh")
    )
    add_card_commands(commands.add_parser)
    add_profile_commands(commands.add_parser)
    add_toget_commands(commands.add_parser)
    add_board_commands(commands.add_parser)
    add_meta_command(commands.add_parser)
    add_app_command(commands.add_parser)
    _add_edit_commands(commands.add_parser)
    return parser


def _add_edit_commands(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    login = add_parser("login", help="sign in to Archidekt so `apply --save` can edit your decks")
    login.add_argument("--user", help="Archidekt username cmdr is set up for")
    login.add_argument("--as", dest="login_name", metavar="NAME", help="sign in with this name or email")
    login.add_argument(
        "--password-stdin", action="store_true", help="read the password from stdin instead of asking"
    )
    login.set_defaults(handler=_cmd_login)
    logout = add_parser("logout", help="forget the saved Archidekt login")
    logout.add_argument("--user", help="Archidekt username cmdr is set up for")
    logout.set_defaults(handler=_cmd_logout)
    apply_ = add_parser(
        "apply",
        help="preview a plan of cards to add and remove; --save makes the changes on Archidekt",
        description=_APPLY_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    apply_.add_argument("plan", help="plan file (TOML), or - to read it from stdin")
    apply_.add_argument("--user", help="Archidekt username cmdr is set up for")
    apply_.add_argument("--workspace", help="folder that holds the deck folders")
    apply_.add_argument(
        "--save", action="store_true", help="make the changes on Archidekt (without it, only a preview)"
    )
    apply_.add_argument("--json", action="store_true", help="print the changes as JSON")
    apply_.set_defaults(handler=_cmd_apply)


def _add_suggest_arguments(suggest_: argparse.ArgumentParser) -> None:
    suggest_.add_argument("deck", nargs="?", help="deck id, or part of its name or commander; omit for all")
    suggest_.add_argument("--workspace", help="folder that holds the deck folders")
    suggest_.add_argument(
        "-n",
        "--count",
        type=_positive,
        default=DEFAULT_COUNT,
        help=f"ideas per list (default {DEFAULT_COUNT})",
    )
    suggest_.add_argument(
        "--max-price",
        type=_euro_cents,
        metavar="EUR",
        help="leave out cards dearer than this, and cards with no price",
    )
    suggest_.add_argument(
        "--within-bracket",
        action="store_true",
        help="leave out ideas that would lift a deck above its bracket on Archidekt",
    )
    suggest_.add_argument(
        "-d", "--details", action="store_true", help="show each combo's cards and results, with a link to it"
    )
    suggest_.add_argument(
        "-r",
        "--refresh",
        action="store_true",
        help="ask Commander Spellbook again even if a recent answer is cached",
    )
    suggest_.add_argument(
        "--json", action="store_true", help="print the ideas as JSON (prices in euro cents)"
    )
    suggest_.add_argument(
        "--to-board", action="store_true", help="put the ideas on the deck's board as suggested (one deck)"
    )
    suggest_.set_defaults(handler=_cmd_suggest)


def _cmd_init(args: argparse.Namespace, ctx: Context) -> int:
    path = config_path(args.config, ctx.env)
    if path.exists() and not args.force:
        print(f"cmdr: {path} already exists; pass --force to replace it", file=ctx.err)
        return EXIT_FAILURE
    workspace = Path(args.workspace).resolve()
    write_config(path, args.user, workspace)
    print(f"saved {path}", file=ctx.out)
    return EXIT_OK


def _cmd_sync(args: argparse.Namespace, ctx: Context) -> int:
    file = read_config(config_path(args.config, ctx.env))
    user = resolve_user(args.user, ctx.env, file)
    workspace = Workspace(resolve_workspace(args.workspace, ctx.env, file))
    progress = progress_printer(ctx.err)
    with ctx.client_factory() as http:
        fetcher = Fetcher(http)
        # The card download is independent of the decks, so it runs while they sync.
        cards_job: Future[CardRefresh] | None = None
        if not args.dry_run and not args.no_cards:
            scryfall, folder = ScryfallClient(fetcher), cache_dir(ctx.env)
            cards_job = in_background(
                lambda: refresh_card_db(
                    scryfall, folder, now=ctx.now, force=args.force, on_progress=progress
                ),
                name="card-data",
            )
        results = sync_decks(
            ArchidektClient(fetcher),
            workspace,
            user,
            force=args.force,
            dry_run=args.dry_run,
            on_progress=progress,
        )
        labels = None if args.dry_run else _sync_labels(fetcher, workspace, user, ctx)
        cards = _card_status(cards_job, dry_run=args.dry_run)
    picked = {} if args.dry_run else _pick_up_aside(workspace, ctx)
    ideas = {} if args.dry_run or args.no_ideas else _compile_ideas(workspace, user, file.meta, ctx, progress)
    if args.json:
        document = {
            "decks": [_result_json(r) for r in results],
            "cardData": {"status": cards.state.value, "detail": cards.detail},
            "labels": None if labels is None else [{"name": n, "colour": c} for n, c in labels],
            "ideas": {
                short: {origin.value: [card.name for card in cards] for origin, cards in added.items()}
                for short, added in ideas.items()
            },
        }
        json.dump(document, ctx.out, ensure_ascii=False, indent=2)
        ctx.out.write("\n")
    else:
        _print_sync_report(results, ctx.out, dry_run=args.dry_run)
        print(f"card data: {cards.state.value}, {cards.detail}", file=ctx.out)
        if labels is not None:
            print(
                f"card labels: {', '.join(n for n, _ in labels) or 'none'} (from your Archidekt account)",
                file=ctx.out,
            )
        for short, names in picked.items():
            print(
                f"{short} board: considering {', '.join(names)} (from the Sideboard or Maybeboard)",
                file=ctx.out,
            )
        for short, added in ideas.items():
            if any(added.values()):
                print(f"{short} ideas: {ideas_summary(added)}", file=ctx.out)
    failed = any(r.status is SyncStatus.FAILED for r in results) or cards.state is _CardState.FAILED
    return EXIT_FAILURE if failed else EXIT_OK


def _compile_ideas(
    workspace: Workspace, user: str, settings: MetaSettings, ctx: Context, progress: Progress
) -> dict[str, dict[Origin, list[BoardCard]]]:
    """Each deck's new ideas from the meta and recent Archidekt decks (ADR 0014), by short name.

    Answers under a day old come from the cache. A deck whose sources fail keeps its board as it was.
    """
    try:
        db = CardDb(cache_dir(ctx.env) / CARD_DB_FILE)
    except CardDataError as error:
        print(f"cmdr: ideas not compiled: {error}", file=ctx.err)
        return {}
    found: dict[str, dict[Origin, list[BoardCard]]] = {}
    with db, ctx.client_factory() as http:
        sources = meta_sources(http, ctx, user, settings)
        for stored in workspace.load_all():
            deck = stored.deck
            if not deck.commanders:
                continue
            try:
                data = gather_for(
                    deck, sources, cache_dir(ctx.env), now=ctx.now, refresh=False, on_progress=progress
                )
                groups = read_profile(stored.folder).adjacent
                adjacent = gather_adjacent(
                    deck,
                    groups,
                    sources,
                    cache_dir(ctx.env),
                    now=ctx.now,
                    refresh=False,
                    on_progress=progress,
                )
                for name, why in adjacent.errors:
                    print(
                        f"cmdr: {deck.short_name}: no recent {name} decks to learn from ({why})", file=ctx.err
                    )
                found[deck.short_name] = compile_ideas(stored, data, db, now=ctx.now, adjacent=adjacent.decks)
            except (BoardError, ProfileError) as error:
                print(f"cmdr: {deck.short_name} ideas not compiled: {error}", file=ctx.err)
                continue
            if data.lists.data is None and data.peers.data is None:
                why = data.peers.error or data.lists.error
                print(
                    f"cmdr: {deck.short_name}: no meta or Archidekt decks to learn from ({why})", file=ctx.err
                )
    return found


def _pick_up_aside(workspace: Workspace, ctx: Context) -> dict[str, list[str]]:
    """Each deck's Sideboard and Maybeboard cards new to its board, put there as considered."""
    try:
        db = CardDb(cache_dir(ctx.env) / CARD_DB_FILE)
    except CardDataError as error:
        print(f"cmdr: boards not updated from the Sideboards and Maybeboards: {error}", file=ctx.err)
        return {}
    picked: dict[str, list[str]] = {}
    with db:
        for stored in workspace.load_all():
            try:
                added = pick_up_aside(stored, db, now=ctx.now)
            except BoardError as error:
                print(f"cmdr: {stored.deck.short_name} board not updated: {error}", file=ctx.err)
                continue
            if added:
                picked[stored.deck.short_name] = [card.name for card in added]
    return picked


def _card_status(job: Future[CardRefresh] | None, *, dry_run: bool) -> _CardStatus:
    if job is None:
        return _CardStatus(_CardState.SKIPPED, "dry run" if dry_run else "--no-cards")
    try:
        refresh = job.result()
    except (NetworkError, ScryfallError, CardDataError, OSError) as error:
        logging.getLogger(__name__).info("card data refresh failed: %s", error)
        return _CardStatus(_CardState.FAILED, f"{error}; the previous card data is kept")
    source = f"Scryfall data of {day(refresh.meta.cards_updated_at)}"
    if refresh.status is RefreshStatus.CURRENT:
        return _CardStatus(_CardState.CURRENT, source)
    return _CardStatus(_CardState.UPDATED, f"{source}, {refresh.printings or 0:,} printings")


def _cmd_analyze(args: argparse.Namespace, ctx: Context) -> int:
    decks = selected_decks(args, ctx)
    with open_card_db(ctx) as db:
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


def _cmd_price(args: argparse.Namespace, ctx: Context) -> int:
    decks = selected_decks(args, ctx)
    with open_card_db(ctx) as db:
        prices = [price_deck(s.deck, db.resolve(s.deck.main_cards)) for s in decks]
        meta = db.meta()
    if args.json:
        json.dump([_price_json(p) for p in prices], ctx.out, ensure_ascii=False, indent=2)
        ctx.out.write("\n")
        return EXIT_OK
    print(f"Cardmarket prices in EUR, from Scryfall data of {day(meta.cards_updated_at)}", file=ctx.out)
    if args.deck:
        for price in prices:
            _print_deck_price(price, ctx.out, top=args.top)
    else:
        _print_price_table(prices, ctx.out)
    return EXIT_OK


def _cmd_bracket(args: argparse.Namespace, ctx: Context) -> int:
    stored = selected_decks(args, ctx)
    decks = [s.deck for s in stored]
    ceilings = [read_profile(s.folder).bracket_ceiling for s in stored]
    with open_card_db(ctx) as db:
        cards = [bracket_cards(db.resolve(deck.main_cards), db.available_roles) for deck in decks]
    with ctx.client_factory() as http:
        results = estimate_decks(
            decks,
            SpellbookClient(Fetcher(http)),
            estimate_cache(cache_dir(ctx.env) / SPELLBOOK_CACHE_FOLDER),
            now=ctx.now,
            refresh=args.refresh,
        )
    reports = [
        BracketReport(d, c, r, ceiling)
        for d, c, r, ceiling in zip(decks, cards, results, ceilings, strict=True)
    ]
    for report in reports:
        _warn_answer(report.deck, report.result, "bracket estimate", ctx.err)
    if args.json:
        json.dump([_bracket_json(r) for r in reports], ctx.out, ensure_ascii=False, indent=2)
        ctx.out.write("\n")
    elif args.deck:
        for report in reports:
            _print_bracket(report, ctx.out, details=args.details)
        print(SPELLBOOK_CREDIT, file=ctx.out)
    else:
        _print_bracket_table(reports, ctx.out)
    unavailable = any(r.result.source is AnswerSource.UNAVAILABLE for r in reports)
    return EXIT_FAILURE if unavailable else EXIT_OK


def _cmd_suggest(args: argparse.Namespace, ctx: Context) -> int:
    if args.to_board and args.deck is None:
        raise UsageError("--to-board needs one deck")
    file = read_config(config_path(args.config, ctx.env))
    stored = selected_decks(args, ctx)
    decks = [s.deck for s in stored]
    profiles = [read_profile(s.folder) for s in stored]
    with open_card_db(ctx) as db:
        with ctx.client_factory() as http:
            combos = find_combos(
                decks,
                SpellbookClient(Fetcher(http)),
                combos_cache(cache_dir(ctx.env) / COMBOS_CACHE_FOLDER),
                now=ctx.now,
                refresh=args.refresh,
            )
        results: list[Suggestions] = []
        folders = [s.folder for s in stored]
        for deck, folder, profile, answer in zip(decks, folders, profiles, combos, strict=True):
            merged = merge_themes(deck, profile, db)
            themes = merged.resolved
            own = {*profile.themes.tags, *profile.exclude.tags}
            for slug in themes.unknown_tags:
                where = "profile tag" if slug in own else "deck theme tag"
                print(
                    f"cmdr: {deck.name}: {where} {slug!r} is not a Tagger tag; `cmdr tags` finds them",
                    file=ctx.err,
                )
            for name in merged.unknown_ignores:
                print(
                    f"cmdr: {deck.name}: the profile ignores {name!r}, which is no deck tag or category",
                    file=ctx.err,
                )
            options = options_for(
                deck,
                profile,
                themes,
                file.targets,
                count=args.count,
                max_cents=args.max_price,
                within_bracket=args.within_bracket,
                rejected=read_board(folder).rejected_names,
            )
            results.append(suggest(deck, db.resolve(deck.main_cards), db, answer, options))
            if args.to_board:
                added = feed_suggestions(
                    BoardService(folder, deck, db, now=ctx.now), results[-1], read_board(folder)
                )
                print(f"put {len(added)} idea(s) on the {deck.short_name} board", file=ctx.err)
        meta = db.meta()
    for result in results:
        _warn_answer(result.deck, result.combos, "combo list", ctx.err)
    if args.json:
        json.dump([_suggestions_json(r) for r in results], ctx.out, ensure_ascii=False, indent=2)
        ctx.out.write("\n")
    elif args.deck:
        print(
            f"prices: each card's cheapest printing in EUR, Scryfall data of {day(meta.cards_updated_at)}",
            file=ctx.out,
        )
        for result in results:
            _print_suggestions(result, ctx.out, details=args.details)
        print(SUGGEST_CREDIT, file=ctx.out)
    else:
        _print_suggestions_table(results, ctx.out)
    unavailable = any(r.combos.source is AnswerSource.UNAVAILABLE for r in results)
    return EXIT_FAILURE if unavailable else EXIT_OK


def _cmd_login(args: argparse.Namespace, ctx: Context) -> int:
    user = resolve_user(args.user, ctx.env, read_config(config_path(args.config, ctx.env)))
    name = args.login_name or user
    if args.password_stdin:
        try:
            password = ctx.stdin.readline(MAX_PASSWORD_BYTES).decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError:
            raise UsageError("the password on stdin must be UTF-8 text") from None
    elif ctx.stdin.isatty():
        password = ctx.prompt_password(f"Archidekt password for {name}: ")
    else:
        raise UsageError("no terminal to ask for the password on; pass --password-stdin")
    with ctx.client_factory() as http:
        ArchidektAuth(Fetcher(http), ctx.token_store).login(user, name, password)
    print(f"signed in to Archidekt as {user}; the login is kept in Windows Credential Manager", file=ctx.out)
    return EXIT_OK


def _cmd_logout(args: argparse.Namespace, ctx: Context) -> int:
    user = resolve_user(args.user, ctx.env, read_config(config_path(args.config, ctx.env)))
    with ctx.client_factory() as http:
        removed = ArchidektAuth(Fetcher(http), ctx.token_store).logout(user)
    print(f"removed the saved login for {user}" if removed else f"no saved login for {user}", file=ctx.out)
    return EXIT_OK


def _cmd_apply(args: argparse.Namespace, ctx: Context) -> int:
    plan = read_plan(ctx.stdin if args.plan == "-" else Path(args.plan))
    file = read_config(config_path(args.config, ctx.env))
    user = resolve_user(args.user, ctx.env, file)
    deck_id, workspace = _plan_deck(plan.deck, args, ctx)
    result: SaveResult | None = None
    with open_card_db(ctx) as db, ctx.client_factory() as http:
        fetcher = Fetcher(http)
        token = _access_token(ArchidektAuth(fetcher, ctx.token_store), user, args, ctx)
        outcome = apply_plan(
            ArchidektEditor(fetcher, token),
            deck_id,
            plan,
            db,
            user=user,
            save_changes=args.save,
            now=ctx.now,
            on_progress=progress_printer(ctx.err),
        )
        resolution, result = outcome.resolution, outcome.result
    folder = None
    if result is not None and workspace is not None and workspace.folder_for(deck_id) is not None:
        folder = workspace.save(result.deck)
    if args.json:
        json.dump(_apply_json(resolution, result), ctx.out, ensure_ascii=False, indent=2)
        ctx.out.write("\n")
    else:
        print_resolution(resolution, ctx.out)
        print_apply_outcome(resolution, result, folder, ctx.out, save=args.save)
    if resolution.problems:
        return EXIT_USAGE
    return EXIT_FAILURE if result is not None and result.mismatches else EXIT_OK


def _plan_deck(query: str, args: argparse.Namespace, ctx: Context) -> tuple[int, Workspace | None]:
    """The plan's deck id, and the workspace when there is one (a deck id works without it)."""
    file = read_config(config_path(args.config, ctx.env))
    if not query.isdigit():
        workspace = Workspace(resolve_workspace(args.workspace, ctx.env, file))
        return one_deck(workspace, query).deck.id, workspace
    try:
        return int(query), Workspace(resolve_workspace(args.workspace, ctx.env, file))
    except ConfigError:
        return int(query), None


def _sync_labels(
    fetcher: Fetcher, workspace: Workspace, user: str, ctx: Context
) -> list[tuple[str, str]] | None:
    """Keep the account's card labels for the app (ADR 0012); None when not signed in or the read fails."""
    auth = ArchidektAuth(fetcher, ctx.token_store)
    if not auth.has_login(user):
        return None
    try:
        labels = ArchidektEditor(fetcher, auth.access_token(user)).account_labels()
    except (AuthError, CredentialError, NetworkError, ArchidektError) as error:
        print(f"cmdr: card labels not refreshed: {error}", file=ctx.err)
        return None
    workspace.save_account_labels(labels)
    return labels


def _access_token(auth: ArchidektAuth, user: str, args: argparse.Namespace, ctx: Context) -> str | None:
    """A token for saving, or for previewing a private deck; a preview goes on without one."""
    if args.save:
        return auth.access_token(user)
    if not auth.has_login(user):
        return None
    try:
        return auth.access_token(user)
    except AuthError as error:
        print(f"cmdr: {error}; previewing without signing in", file=ctx.err)
        return None


def _cmd_list(args: argparse.Namespace, ctx: Context) -> int:
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


SPELLBOOK_CREDIT = "bracket estimates and combos from Commander Spellbook, https://commanderspellbook.com"
_TWO_CARD_ORDER = {TwoCard.YES: 0, TwoCard.ARGUABLY: 1, TwoCard.NO: 2}
_TAG_ORDER = {tag: index for index, tag in enumerate(BracketTag)}


def _warn_answer[T](deck: Deck, answer: Answer[T], what: str, err: TextIO) -> None:
    if answer.source is AnswerSource.UNAVAILABLE:
        print(f"cmdr: {deck.name}: no {what}: {answer.error}", file=err)
    elif answer.source is AnswerSource.STALE and answer.fetched_at is not None:
        print(
            f"cmdr: {deck.name}: showing the {what} from {answer.fetched_at.date().isoformat()}; "
            f"refreshing it failed: {answer.error}",
            file=err,
        )


def _estimate_text(result: EstimateResult) -> str:
    if result.data is None or result.fetched_at is None:
        return "unavailable"
    tag = result.data.tag
    day = result.fetched_at.date().isoformat()
    if tag.minimum is None:
        return f"not legal in Commander ({tag.label}), Commander Spellbook {day}"
    return f"bracket {tag.brackets} ({tag.label}), Commander Spellbook {day}"


def _sorted_combos(combos: Sequence[Combo]) -> list[Combo]:
    return sorted(
        combos, key=lambda c: (_TAG_ORDER[c.tag], _TWO_CARD_ORDER[c.two_card], len(c.cards), c.cards)
    )


def _card_list(label: str, names: Sequence[str] | None) -> str:
    if names is None:
        return f"{label}: unknown (not in the tag data)"
    if not names:
        return f"{label}: none"
    return f"{label} ({len(names)}): {', '.join(names)}"


def _print_bracket(report: BracketReport, out: TextIO, *, details: bool) -> None:
    deck, cards, result = report.deck, report.cards, report.result
    commanders = " + ".join(deck.commanders) or "no commander"
    bracket = f"bracket {deck.bracket} on Archidekt" if deck.bracket is not None else "no bracket set"
    print(f"{deck.name} ({deck.id})", file=out)
    print(f"{commanders}; {bracket}", file=out)
    print(f"estimate: {_estimate_text(result)}", file=out)
    check = report.check
    for note in check.notes:
        print(f"  ! {note}", file=out)
    if check.causes:
        print("  because of:", file=out)
        for cause in check.causes:
            print(f"    {cause.tag.label:<10} {cause.reason.value}: {' + '.join(cause.cards)}", file=out)
    print(_card_list("game changers", cards.game_changers), file=out)
    print(_card_list("tutors", cards.tutors), file=out)
    print(_card_list("mass land denial", cards.mass_land_denial), file=out)
    print(_card_list("extra turns", cards.extra_turns), file=out)
    print("(tutors, mass land denial and extra turns come from Scryfall tags)", file=out)
    if cards.not_legal:
        print(f"not legal in Commander: {', '.join(cards.not_legal)}", file=out)
    if result.data is not None:
        _print_combos(result.data.combos, out, details=details)
    print(file=out)


def _print_combos(combos: Sequence[Combo], out: TextIO, *, details: bool) -> None:
    if not combos:
        print("combos: none", file=out)
        return
    print(f"\ncombos ({len(combos)})", file=out)
    for combo in _sorted_combos(combos):
        size = {TwoCard.YES: "2-card", TwoCard.ARGUABLY: "2-card?"}.get(
            combo.two_card, f"{len(combo.cards)}-card"
        )
        print(f"  {combo.tag.label:<10} {size:<8}  {' + '.join(combo.cards)}", file=out)
        if details:
            print(f"  {'':<10} {'':<8}  -> {', '.join(combo.results) or 'no results listed'}", file=out)
            print(f"  {'':<10} {'':<8}  {combo.url}", file=out)
    if any(c.two_card is TwoCard.ARGUABLY for c in combos):
        print(
            "? Commander Spellbook counts it as arguably two cards",
            file=out,
        )


def _print_bracket_table(reports: Sequence[BracketReport], out: TextIO) -> None:
    print(
        f"{'id':>9} {'set':>3}  {'estimate':<16} {'gc':>3} {'tutors':>6} {'mld':>3} {'turns':>5} "
        f"{'2-card':>6} {'combos':>6}  name",
        file=out,
    )
    for r in reports:
        estimate = r.result.data
        tag = f"{estimate.tag.brackets} {estimate.tag.label}" if estimate is not None else "-"
        two_card = sum(1 for c in estimate.combos if c.two_card is TwoCard.YES) if estimate else None
        combos = len(estimate.combos) if estimate else None
        bracket = r.deck.bracket if r.deck.bracket is not None else "-"
        mark = "! " if r.check.notes else ""
        print(
            f"{r.deck.id:>9} {bracket:>3}  {tag:<16} {len(r.cards.game_changers):>3} "
            f"{_size(r.cards.tutors):>6} {_size(r.cards.mass_land_denial):>3} "
            f"{_size(r.cards.extra_turns):>5} {_count(two_card):>6} "
            f"{_count(combos):>6}  {mark}{r.deck.name}",
            file=out,
        )
    if any(r.check.notes for r in reports):
        print(
            "! the Archidekt bracket, the profile's ceiling and the estimate disagree; "
            "`cmdr bracket <deck>` says why",
            file=out,
        )
    print("set = bracket on Archidekt; tutors, mld and turns come from Scryfall tags", file=out)
    print(SPELLBOOK_CREDIT, file=out)


def _bracket_json(report: BracketReport) -> dict[str, object]:
    deck, cards, result = report.deck, report.cards, report.result
    estimate = result.data
    return {
        "id": deck.id,
        "name": deck.name,
        "commanders": list(deck.commanders),
        "bracket": deck.bracket,
        "estimate": {
            "source": result.source.value,
            "fetchedAt": result.fetched_at.isoformat() if result.fetched_at else None,
            "error": result.error,
            "tag": estimate.tag.label if estimate else None,
            "minimumBracket": estimate.tag.minimum if estimate else None,
            "brackets": estimate.tag.brackets if estimate else None,
        },
        "setBelowEstimate": report.set_below_estimate,
        "ceiling": report.ceiling,
        "check": _check_json(report.check),
        "gameChangers": list(cards.game_changers),
        "tutors": _optional_list(cards.tutors),
        "massLandDenial": _optional_list(cards.mass_land_denial),
        "extraTurns": _optional_list(cards.extra_turns),
        "notLegal": list(cards.not_legal),
        "combos": [
            {
                "id": c.id,
                "url": c.url,
                "tag": c.tag.label,
                "cards": list(c.cards),
                "results": list(c.results),
                "twoCard": c.two_card.value,
            }
            for c in (_sorted_combos(estimate.combos) if estimate else [])
        ],
    }


def _check_json(check: BracketCheck) -> dict[str, object]:
    return {
        "notes": list(check.notes),
        "causes": [
            {
                "tag": cause.tag.label,
                "reason": cause.reason.value,
                "cards": list(cause.cards),
                "comboUrl": cause.combo.url if cause.combo else None,
            }
            for cause in check.causes
        ],
    }


SUGGEST_CREDIT = (
    "combos from Commander Spellbook, https://commanderspellbook.com; "
    "popularity ranks from EDHREC, https://edhrec.com, via Scryfall"
)


def _print_suggestions(result: Suggestions, out: TextIO, *, details: bool) -> None:
    deck = result.deck
    commanders = " + ".join(deck.commanders) or "no commander"
    bracket = f"bracket {deck.bracket} on Archidekt" if deck.bracket is not None else "no bracket set"
    colours = "colours unknown" if result.identity is None else f"colours {result.identity or 'colourless'}"
    print(f"\n{deck.name} ({deck.id})", file=out)
    print(f"{commanders}; {bracket}; {colours}", file=out)
    _print_pieces(result, out, details=details)
    _print_roles(result, out)
    _print_themed(result, out)
    _print_cuts(result, out)
    ideas = [i for r in result.roles for i in r.ideas] + list(result.themed)
    raised = any(p.raises for p in result.pieces) or any(i.raises for i in ideas)
    if raised:
        print(
            "! adding it lifts the deck to at least that bracket; --within-bracket leaves these out", file=out
        )
    if result.hidden:
        print(
            f"{result.hidden} left out because they would lift the deck above bracket {result.ceiling}",
            file=out,
        )


def _print_pieces(result: Suggestions, out: TextIO, *, details: bool) -> None:
    answer = result.combos
    if answer.data is None or answer.fetched_at is None:
        print("\ncards that complete a combo: unknown, Commander Spellbook could not be reached", file=out)
        return
    source = f"Commander Spellbook {answer.fetched_at.date().isoformat()}"
    if not result.pieces:
        print(f"\ncards that complete a combo: none ({source})", file=out)
        return
    print(f"\ncards that complete a combo ({source})", file=out)
    for piece in result.pieces:
        tags = sorted({v.tag for v in piece.combos}, key=lambda t: _TAG_ORDER[t])
        combos = f"{len(piece.combos)} combo{'' if len(piece.combos) == 1 else 's'}"
        print(
            f"  {price_text(piece.price_cents):>9}  {piece.name} ({combos}: "
            f"{', '.join(t.label for t in tags)}){raise_text(piece.raises)}",
            file=out,
        )
        needs = sorted({need for v in piece.combos for need in v.requirements})
        if needs:
            print(f"  {'':>9}  also needs: {'; '.join(needs)}", file=out)
        if details:
            for variant in piece.combos:
                _print_variant(variant, piece.name, out)
    if result.more_pieces:
        print(f"  ... {result.more_pieces} more; --count lists more", file=out)


def _print_variant(variant: Variant, piece: str, out: TextIO) -> None:
    others = " + ".join(c.name for c in variant.cards if c.name != piece)
    results = ", ".join(variant.results) or "no results listed"
    print(f"  {'':>9}    {variant.tag.label}: with {others} -> {results}", file=out)
    print(f"  {'':>9}    {variant.url}", file=out)


def _print_roles(result: Suggestions, out: TextIO) -> None:
    if result.identity is None:
        print("\nroles: a commander is not in the card data, so its colours and ideas are unknown", file=out)
        return
    short = [r for r in result.roles if r.short]
    met = ", ".join(f"{r.role.value} {r.have}/{r.target}" for r in result.roles if not r.short)
    if not short:
        print(f"\nroles: {f'all at target ({met})' if met else 'no targets set'}", file=out)
        return
    print(
        "\nroles below target (nonland cards by Scryfall tags or your categories; most played ideas first)",
        file=out,
    )
    for target in short:
        print(f"  {target.role.value} {target.have} of {target.target}", file=out)
        if not target.ideas:
            print(f"  {'':>9}  no card fits", file=out)
        for idea in target.ideas:
            _print_idea(idea, out)
    if met:
        print(f"  at target: {met}", file=out)


def _print_themed(result: Suggestions, out: TextIO) -> None:
    if not result.themed:
        return
    print("\ncards that fit the deck's themes (most played first)", file=out)
    for idea in result.themed:
        _print_idea(idea, out, mark_theme=False)


def _print_idea(idea: RoleIdea, out: TextIO, *, mark_theme: bool = True) -> None:
    theme = (
        f" (fits {', '.join(idea.fits)})" if idea.fits else " (theme)" if mark_theme and idea.themed else ""
    )
    price = price_text(idea.info.cheapest_cents)
    print(f"  {price:>9}  {idea.info.name}{theme}{raise_text(idea.raises)}", file=out)


def _print_cuts(result: Suggestions, out: TextIO) -> None:
    if not result.cuts:
        print("\ncards to consider cutting: none, every nonland card has a role or is in a combo", file=out)
        return
    print("\ncards to consider cutting (no role, in no combo; least played first, by EDHREC rank)", file=out)
    for info in result.cuts:
        rank = f"#{info.edhrec_rank:,}" if info.edhrec_rank is not None else "unranked"
        print(f"  {rank:>9}  {info.name}", file=out)
    if result.combos.data is None:
        print("  (without Commander Spellbook, combo pieces may be among these)", file=out)


def _print_suggestions_table(results: Sequence[Suggestions], out: TextIO) -> None:
    roles = [role for role in ANALYZED_ROLES if any(t.role is role for r in results for t in r.roles)]
    widths = {role: max(len(_ROLE_HEADINGS[role]), 5) for role in roles}
    headings = "".join(f" {_ROLE_HEADINGS[role]:>{widths[role]}}" for role in roles)
    print(f"{'id':>9} {'set':>3} {'combo':>5}{headings} {'cuts':>4}  name", file=out)
    for r in results:
        pieces = len(r.pieces) + r.more_pieces if r.combos.data is not None else None
        targets = {t.role: t for t in r.roles}
        cells = "".join(f" {_target_text(targets.get(role)):>{widths[role]}}" for role in roles)
        bracket = r.deck.bracket if r.deck.bracket is not None else "-"
        print(
            f"{r.deck.id:>9} {bracket:>3} {_count(pieces):>5}{cells} {len(r.cuts):>4}  {r.deck.name}",
            file=out,
        )
    print("combo = cards that would complete a combo; roles = nonland cards tagged / target", file=out)
    print("`cmdr suggest <deck>` lists the cards", file=out)
    print(SUGGEST_CREDIT, file=out)


def _target_text(target: RoleTarget | None) -> str:
    return "-" if target is None else f"{target.have}/{target.target}"


def _suggestions_json(result: Suggestions) -> dict[str, object]:
    deck, answer = result.deck, result.combos
    return {
        "id": deck.id,
        "name": deck.name,
        "commanders": list(deck.commanders),
        "bracket": deck.bracket,
        "colorIdentity": result.identity,
        "combos": {
            "source": answer.source.value,
            "fetchedAt": answer.fetched_at.isoformat() if answer.fetched_at else None,
            "error": answer.error,
        },
        "comboPieces": [
            {
                "name": p.name,
                "priceCents": p.price_cents,
                "raises": raise_json(p.raises),
                "combos": [
                    {
                        "id": v.id,
                        "url": v.url,
                        "tag": v.tag.label,
                        "cards": [c.name for c in v.cards],
                        "requirements": list(v.requirements),
                        "results": list(v.results),
                        "popularity": v.popularity,
                    }
                    for v in p.combos
                ],
            }
            for p in result.pieces
        ],
        "moreComboPieces": result.more_pieces,
        "roles": [
            {
                "role": t.role.value,
                "have": t.have,
                "target": t.target,
                "short": t.short,
                "ideas": [_idea_json(i) for i in t.ideas],
            }
            for t in result.roles
        ],
        "themed": [_idea_json(i) for i in result.themed],
        "cuts": [
            {"name": c.name, "priceCents": c.cheapest_cents, "edhrecRank": c.edhrec_rank} for c in result.cuts
        ],
        "hidden": result.hidden,
        "ceiling": result.ceiling,
    }


def _idea_json(idea: RoleIdea) -> dict[str, object]:
    return {
        "name": idea.info.name,
        "priceCents": idea.info.cheapest_cents,
        "edhrecRank": idea.info.edhrec_rank,
        "raises": raise_json(idea.raises),
        "themed": idea.themed,
        "fits": list(idea.fits),
    }


def _apply_json(resolution: Resolution, result: SaveResult | None) -> dict[str, object]:
    deck = resolution.deck
    return {
        "deck": {"id": deck.id, "name": deck.name, "url": deck.url, "bracket": deck.bracket},
        "cardsBefore": resolution.main_before,
        "cardsAfter": resolution.main_after,
        "changes": [
            {
                "kind": c.kind.value,
                "name": c.name,
                "before": c.before,
                "after": c.after,
                "category": c.category,
                "inDeck": c.in_deck,
                "set": c.set_code,
                "collectorNumber": c.collector_number,
                "scryfallId": c.scryfall_id,
                "priceCents": c.price_cents,
                "raises": raise_json(c.raises),
            }
            for c in resolution.changes
        ],
        "skipped": list(resolution.skipped),
        "warnings": list(resolution.warnings),
        "problems": list(resolution.problems),
        "saved": result is not None,
        "snapshot": result.snapshot if result else None,
        "mismatches": list(result.mismatches) if result else [],
    }


def _size(names: Sequence[str] | None) -> str:
    return "-" if names is None else str(len(names))


def _optional_list(names: Sequence[str] | None) -> list[str] | None:
    return None if names is None else list(names)


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


def _positive(text: str) -> int:
    value = _non_negative(text)
    if value == 0:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def _euro_cents(text: str) -> int:
    # A decimal comma is accepted too, as euro amounts are often written that way.
    try:
        amount = Decimal(text.strip().replace(",", "."))
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not an amount in euros: {text!r}") from None
    if not amount.is_finite() or amount < 0:
        raise argparse.ArgumentTypeError(f"not an amount in euros: {text!r}")
    return int((amount * 100).to_integral_value(rounding=ROUND_FLOOR))


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


def _utf8(stream: TextIO) -> TextIO:
    # Deck names carry emoji; a redirected Windows stream would otherwise use the ANSI codepage and crash.
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")
    return stream


if __name__ == "__main__":
    raise SystemExit(main())
