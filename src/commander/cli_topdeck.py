"""`cmdr topdeck`: save the TopDeck.gg API key the tournament lists need, or forget it."""

import argparse
from collections.abc import Callable

from commander.clibase import EXIT_OK, Context, UsageError
from commander.config import cache_dir
from commander.credentials import KeyringKeyStore, check_api_key
from commander.metadata import topdeck_client
from commander.topdeck import CREDIT, DEVELOPERS_URL, SITE_URL

MAX_KEY_BYTES = 2048
_DESCRIPTION = f"""\
The tournament lists in `cmdr meta`, `cmdr sync` and the app come from TopDeck.gg, whose API needs a free key
for each user. Get yours at {DEVELOPERS_URL}, then run this and paste it when asked.

TopDeck is asked whether it accepts the key before it is saved in Windows Credential Manager (service
"Commander (cmdr) TopDeck"). The key is never taken from the command line, where it would stay in the shell's
history.
"""


def add_topdeck_command(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    topdeck = add_parser(
        "topdeck",
        help="save the TopDeck.gg API key the tournament lists need",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    topdeck.add_argument("--key-stdin", action="store_true", help="read the key from stdin instead of asking")
    topdeck.add_argument("--no-check", action="store_true", help="save the key without asking TopDeck first")
    topdeck.add_argument("--remove", action="store_true", help="forget the saved key")
    topdeck.set_defaults(handler=_cmd_topdeck)


def _cmd_topdeck(args: argparse.Namespace, ctx: Context) -> int:
    store = KeyringKeyStore()
    if args.remove:
        message = "removed the saved TopDeck key" if store.delete() else "no TopDeck key saved"
        print(message, file=ctx.out)
        return EXIT_OK
    key = check_api_key(_read_key(args, ctx))
    if not args.no_check:
        with ctx.client_factory() as http:
            topdeck_client(http, ctx.pacer_factory, cache_dir(ctx.env), store).check_key(key)
    store.put(key)
    checked = "" if args.no_check else " (TopDeck accepted it)"
    print(f"TopDeck key saved in Windows Credential Manager{checked}", file=ctx.out)
    print(f"{CREDIT}, {SITE_URL}", file=ctx.out)
    return EXIT_OK


def _read_key(args: argparse.Namespace, ctx: Context) -> str:
    if args.key_stdin:
        try:
            return ctx.stdin.readline(MAX_KEY_BYTES).decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError:
            raise UsageError("the key on stdin must be plain text") from None
    if ctx.stdin.isatty():
        return ctx.prompt_password("TopDeck API key: ")
    raise UsageError("no terminal to ask for the key on; pass --key-stdin")
