"""`cmdr` entry point: argument parsing, output formatting and exit codes."""

import argparse
import json
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO

import httpx

from commander import __version__
from commander.archidekt import ArchidektClient, ArchidektError
from commander.config import (
    ENV_CONFIG,
    ENV_USER,
    ENV_WORKSPACE,
    ConfigError,
    config_path,
    environment,
    read_config,
    resolve_user,
    resolve_workspace,
    write_config,
)
from commander.diff import DeckDiff
from commander.net import JsonFetcher, NetworkError, make_client
from commander.store import Workspace
from commander.sync import SyncResult, SyncStatus, sync_decks

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

ClientFactory = Callable[[], httpx.Client]

_DESCRIPTION = """\
Track your Archidekt Commander decks and research changes to them.

examples:
  cmdr init --user MAKKIS --workspace decks   save your username and deck folder
  cmdr sync                                   pull every public Commander deck, show what changed
  cmdr sync --dry-run                         show what would change without writing anything
  cmdr list --json                            local decks as JSON
"""

_EPILOG = f"""\
settings come from flags, then environment ({ENV_USER}, {ENV_WORKSPACE}, {ENV_CONFIG}),
then the config file (default %APPDATA%\\Commander\\config.toml).
exit codes: 0 success, 1 failure, 2 usage error, 130 interrupted.
"""


def main(  # noqa: PLR0911 — one exit code per failure class, mapped in one place
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    client_factory: ClientFactory = make_client,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
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
    context = _Context(env or environment(), client_factory, out, err)
    command: Callable[[argparse.Namespace, _Context], int] | None = getattr(args, "handler", None)
    if command is None:
        parser.print_usage(err)
        return EXIT_USAGE
    try:
        return command(args, context)
    except ConfigError as error:
        print(f"cmdr: {error}", file=err)
        return EXIT_USAGE
    except (NetworkError, ArchidektError, OSError) as error:
        print(f"cmdr: {error}", file=err)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        print("cmdr: interrupted", file=err)
        return EXIT_INTERRUPTED


class _Context:
    def __init__(
        self, env: Mapping[str, str], client_factory: ClientFactory, out: TextIO, err: TextIO
    ) -> None:
        self.env = env
        self.client_factory = client_factory
        self.out = out
        self.err = err


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
    sync.add_argument("-f", "--force", action="store_true", help="refetch every deck, even unchanged ones")
    sync.add_argument("-n", "--dry-run", action="store_true", help="report changes without writing files")
    sync.add_argument("--json", action="store_true", help="print results as JSON")
    sync.set_defaults(handler=_cmd_sync)

    list_ = commands.add_parser("list", help="show the decks saved locally")
    list_.add_argument("--workspace", help="folder that holds the deck folders")
    list_.add_argument("--json", action="store_true", help="print the decks as JSON")
    list_.set_defaults(handler=_cmd_list)
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
        client = ArchidektClient(JsonFetcher(http))
        results = sync_decks(
            client, workspace, user, force=args.force, dry_run=args.dry_run, on_progress=progress
        )
    if args.json:
        json.dump([_result_json(r) for r in results], ctx.out, ensure_ascii=False, indent=2)
        ctx.out.write("\n")
    else:
        _print_sync_report(results, ctx.out, dry_run=args.dry_run)
    return EXIT_FAILURE if any(r.status is SyncStatus.FAILED for r in results) else EXIT_OK


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
