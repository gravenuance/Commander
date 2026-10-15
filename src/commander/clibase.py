"""What every `cmdr` command shares: exit codes, injected dependencies and deck and card data lookup."""

import argparse
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import BinaryIO, TextIO

import httpx

from commander.carddb import CARD_DB_FILE, CardDataMeta, CardDb
from commander.config import cache_dir, config_path, read_config, resolve_workspace
from commander.credentials import TokenStore
from commander.net import Pacer
from commander.store import StoredDeck, Workspace

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

STALE_CARD_DATA = timedelta(days=3)

ClientFactory = Callable[[], httpx.Client]
PasswordPrompt = Callable[[str], str]
# Makes the request pacer for a service, given its interval; tests swap in one that never sleeps.
PacerFactory = Callable[[float], Pacer]
Clock = Callable[[], datetime]


class UsageError(Exception):
    """The command line names something that does not exist or is ambiguous."""


class Context:
    def __init__(  # noqa: PLR0917 — every outside dependency, injected for tests
        self,
        env: Mapping[str, str],
        client_factory: ClientFactory,
        out: TextIO,
        err: TextIO,
        now: Clock,
        token_store: TokenStore,
        prompt_password: PasswordPrompt,
        stdin: BinaryIO,
        pacer_factory: PacerFactory = Pacer,
    ) -> None:
        self.env = env
        self.client_factory = client_factory
        self.out = out
        self.err = err
        self.now = now
        self.token_store = token_store
        self.prompt_password = prompt_password
        self.stdin = stdin
        self.pacer_factory = pacer_factory


def workspace(args: argparse.Namespace, ctx: Context) -> Workspace:
    file = read_config(config_path(args.config, ctx.env))
    return Workspace(resolve_workspace(args.workspace, ctx.env, file))


def selected_decks(args: argparse.Namespace, ctx: Context) -> list[StoredDeck]:
    space = workspace(args, ctx)
    if args.deck is None:
        decks = space.load_all()
        if not decks:
            raise UsageError(f"no decks in {space.root}; run `cmdr sync` first")
        return decks
    return [one_deck(space, args.deck)]


def one_deck(space: Workspace, query: str) -> StoredDeck:
    if not query.strip():
        raise UsageError("the deck to look up is empty")
    matches = space.find(query)
    if not matches:
        raise UsageError(f"no deck matches {query!r}; `cmdr list` shows them all")
    if len(matches) > 1:
        names = "; ".join(f"{s.deck.id} {s.deck.name}" for s in matches)
        raise UsageError(f"{query!r} matches {len(matches)} decks ({names}); use the deck id")
    return matches[0]


def open_card_db(ctx: Context) -> CardDb:
    db = CardDb(cache_dir(ctx.env) / CARD_DB_FILE)
    warn_if_stale(db.meta(), ctx)
    return db


def warn_if_stale(meta: CardDataMeta, ctx: Context) -> None:
    try:
        updated = datetime.fromisoformat(meta.cards_updated_at)
    except ValueError:
        return
    if updated.tzinfo is not None and ctx.now() - updated > STALE_CARD_DATA:
        print(
            f"cmdr: card data is from {day(meta.cards_updated_at)}; run `cmdr sync` for fresh prices",
            file=ctx.err,
        )


def day(timestamp: str) -> str:
    # Scryfall timestamps are ISO 8601 in UTC; the date part is all a reader needs.
    return timestamp[:10]
