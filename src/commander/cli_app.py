"""`cmdr app`: open the desktop app over the deck boards."""

import argparse
from collections.abc import Callable

from commander.app import launch
from commander.clibase import EXIT_OK, Context


def add_app_command(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    app = add_parser("app", help="open the desktop app: deck boards, decisions and saving")
    app.add_argument("--workspace", help="folder that holds the deck folders")
    app.add_argument("--user", help="Archidekt username cmdr is set up for")
    app.add_argument("--debug", action="store_true", help="allow the web inspector (F12)")
    app.set_defaults(handler=_cmd_app)


def _cmd_app(args: argparse.Namespace, ctx: Context) -> int:
    launch(
        ctx.env,
        client_factory=ctx.client_factory,
        token_store=ctx.token_store,
        now=ctx.now,
        config=args.config,
        workspace=args.workspace,
        user=args.user,
        debug=args.debug,
    )
    return EXIT_OK
