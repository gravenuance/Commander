"""`cmdr` entry point: argument parsing, exit codes and the commands that need no module of their own."""

import argparse
import getpass
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, TextIO

from commander import __version__
from commander.apply import (
    NotYourDeckError,
    Resolution,
    SaveError,
    SaveResult,
    apply_plan,
)
from commander.archidekt import ArchidektError
from commander.archidekt_edit import (
    ArchidektAuth,
    ArchidektEditor,
    AuthError,
    ReadBackError,
    SignInRequiredError,
)
from commander.board import (
    BoardError,
    BoardIOError,
    NothingQueuedError,
)
from commander.bracketpreview import preview_json, spellbook_preview
from commander.carddb import CardDataError
from commander.cli_analysis import add_analysis_commands
from commander.cli_app import add_app_command
from commander.cli_board import add_board_commands
from commander.cli_cards import add_card_commands
from commander.cli_meta import add_meta_command
from commander.cli_output import (
    print_apply_outcome,
    print_bracket_preview,
    print_resolution,
    progress_printer,
    raise_json,
    write_json,
)
from commander.cli_profile import add_profile_commands
from commander.cli_suggest import add_suggest_command
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
    one_deck,
    open_card_db,
)
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
from commander.credentials import CredentialError, KeyringTokenStore, TokenStore
from commander.deckprofile import read_profile
from commander.diff import DeckDiff
from commander.edhtop16 import Edhtop16Error
from commander.ideas import NO_LONGER_MAKES_THE_CUT, ideas_summary
from commander.labels import LabelError
from commander.net import Fetcher, NetworkError, Pacer, make_client
from commander.plan import EXAMPLE as PLAN_EXAMPLE
from commander.plan import PlanError, read_plan
from commander.scryfall import ScryfallError
from commander.spellbook import SpellbookError
from commander.store import Workspace, WorkspaceError
from commander.suggest import (
    DEFAULT_TARGETS,
)
from commander.sync import SyncResult, SyncStatus
from commander.syncrun import SyncOptions, SyncServices, run_sync

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
    except (BoardIOError, NothingQueuedError) as error:
        print(f"cmdr: {error}", file=err)
        return EXIT_FAILURE
    except (ConfigError, UsageError, PlanError, NotYourDeckError, BoardError) as error:
        print(f"cmdr: {error}", file=err)
        return EXIT_USAGE
    except (SignInRequiredError, ReadBackError) as error:
        print(f"cmdr: {_with_remedy(error)}", file=err)
        return EXIT_FAILURE
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
    except Exception as error:
        # A bug, not a failure the user can act on: one line, and the traceback under -vv.
        logging.getLogger(__name__).debug("unexpected error", exc_info=True)
        print(f"cmdr: unexpected error: {type(error).__name__}: {error}", file=err)
        return EXIT_FAILURE


def _with_remedy(error: Exception) -> str:
    """The error's message plus the cmdr command that fixes it; shared modules leave that to the front end."""
    if isinstance(error, SignInRequiredError):
        return f"{error}; run `cmdr login`"
    if isinstance(error, ReadBackError):
        return f"{error}; run `cmdr sync`"
    return str(error)


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

    add_analysis_commands(commands.add_parser)
    add_suggest_command(commands.add_parser)
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


def _cmd_init(args: argparse.Namespace, ctx: Context) -> int:
    path = config_path(args.config, ctx.env)
    if path.exists() and not args.force:
        print(f"cmdr: {path} already exists; pass --force to replace it", file=ctx.err)
        return EXIT_USAGE
    workspace = Path(args.workspace).resolve()
    write_config(path, args.user, workspace)
    print(f"saved {path}", file=ctx.out)
    return EXIT_OK


def _cmd_sync(args: argparse.Namespace, ctx: Context) -> int:
    file = read_config(config_path(args.config, ctx.env))
    user = resolve_user(args.user, ctx.env, file)
    workspace = Workspace(resolve_workspace(args.workspace, ctx.env, file))
    services = SyncServices(
        ctx.client_factory, ctx.token_store, cache_dir(ctx.env), ctx.now, ctx.pacer_factory
    )
    options = SyncOptions(
        force=args.force, dry_run=args.dry_run, cards=not args.no_cards, ideas=not args.no_ideas
    )
    outcome = run_sync(services, workspace, user, file.meta, options, on_progress=progress_printer(ctx.err))
    for warning in outcome.warnings:
        print(f"cmdr: {warning}", file=ctx.err)
    if args.json:
        document = {
            "decks": [_result_json(r) for r in outcome.results],
            "cardData": {"status": outcome.cards.state.value, "detail": outcome.cards.detail},
            "labels": None
            if outcome.labels is None
            else [{"name": n, "colour": c} for n, c in outcome.labels],
            "ideas": {
                short: {origin.value: [card.name for card in cards] for origin, cards in added.items()}
                for short, added in outcome.ideas.items()
            },
        }
        write_json(document, ctx.out)
    else:
        _print_sync_report(outcome.results, ctx.out, dry_run=args.dry_run)
        print(f"card data: {outcome.cards.state.value}, {outcome.cards.detail}", file=ctx.out)
        if outcome.labels is not None:
            names = ", ".join(n for n, _ in outcome.labels) or "none"
            print(f"card labels: {names} (from your Archidekt account)", file=ctx.out)
        for short, names in outcome.turned_down.items():
            print(f"{short} board: turned down {', '.join(names)} ({NO_LONGER_MAKES_THE_CUT})", file=ctx.out)
        for short, added in outcome.ideas.items():
            if any(added.values()):
                print(f"{short} ideas: {ideas_summary(added)}", file=ctx.out)
    return EXIT_FAILURE if outcome.failed else EXIT_OK


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
        preview = None
        if not args.save:
            ceiling = _plan_ceiling(workspace, deck_id)
            preview = spellbook_preview(resolution, fetcher, cache_dir(ctx.env), ceiling=ceiling, now=ctx.now)
    folder = None
    if result is not None and workspace is not None and workspace.folder_for(deck_id) is not None:
        folder = workspace.save(result.deck)
    if args.json:
        answer = _apply_json(resolution, result) | {"bracketAfter": preview_json(preview)}
        write_json(answer, ctx.out)
    else:
        print_resolution(resolution, ctx.out)
        print_bracket_preview(preview, ctx.out)
        print_apply_outcome(resolution, result, folder, ctx.out, save=args.save)
    if resolution.problems:
        return EXIT_USAGE
    return EXIT_FAILURE if result is not None and result.mismatches else EXIT_OK


def _plan_ceiling(workspace: Workspace | None, deck_id: int) -> int | None:
    """The profile's bracket ceiling, when the deck has a folder in the workspace."""
    folder = workspace.folder_for(deck_id) if workspace is not None else None
    return read_profile(folder).bracket_ceiling if folder is not None else None


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


def _access_token(auth: ArchidektAuth, user: str, args: argparse.Namespace, ctx: Context) -> str | None:
    """A token for saving, or for previewing a private deck; a preview goes on without one."""
    if args.save:
        return auth.access_token(user)
    if not auth.has_login(user):
        return None
    try:
        return auth.access_token(user)
    except AuthError as error:
        print(f"cmdr: {_with_remedy(error)}; previewing without signing in", file=ctx.err)
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
        write_json(rows, ctx.out)
        return EXIT_OK
    for s in stored:
        bracket = s.deck.bracket if s.deck.bracket is not None else "-"
        commanders = " + ".join(s.deck.commanders) or "(no commander)"
        print(f"{s.deck.id}\tB{bracket}\t{s.deck.name}\t{commanders}", file=ctx.out)
    if not stored:
        print(f"no decks in {workspace.root}; run `cmdr sync`", file=ctx.err)
    return EXIT_OK


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
