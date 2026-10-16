"""`cmdr toget`: the cards the decks still have to get, and marking one ordered or arrived (ADR 0010)."""

import argparse
import json
from collections.abc import Callable

from commander.archidekt_edit import ArchidektAuth, ArchidektEditor
from commander.board import Actor, BoardService
from commander.clibase import EXIT_OK, Context, UsageError, one_deck, open_card_db, workspace
from commander.config import config_path, read_config, resolve_user
from commander.labels import Step, mark, to_get_json
from commander.models import Deck
from commander.net import Fetcher

EXAMPLES = """\
examples:
  cmdr toget                                     every deck's cards still to get
  cmdr toget ellivere                            one deck's
  cmdr toget ellivere "Sylvan Safekeeper" --ordered   mark it ordered (label Getting) on Archidekt
  cmdr toget ellivere "Sylvan Safekeeper" --arrived   mark it arrived (no label)
"""


def add_toget_commands(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    toget = add_parser(
        "toget",
        help="cards labelled Don't Have or Getting; mark one ordered or arrived",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    toget.add_argument("deck", nargs="?", help="deck id, or part of its name or commander")
    toget.add_argument("card", nargs="?", help="the card to mark")
    step = toget.add_mutually_exclusive_group()
    step.add_argument("--ordered", action="store_true", help="label the card Getting on Archidekt")
    step.add_argument("--arrived", action="store_true", help="clear the card's label on Archidekt")
    toget.add_argument("--workspace", help="folder that holds the deck folders")
    toget.add_argument("--user", help="Archidekt username cmdr is set up for")
    toget.add_argument("--json", action="store_true", help="print the cards as JSON")
    toget.set_defaults(handler=_cmd_toget)


def _cmd_toget(args: argparse.Namespace, ctx: Context) -> int:
    chosen = Step.ORDERED if args.ordered else Step.ARRIVED if args.arrived else None
    if args.card is None and chosen is None:
        return _show(args, ctx)
    if args.card is None or chosen is None:
        raise UsageError("name a card together with --ordered or --arrived")
    stored = one_deck(workspace(args, ctx), args.deck)
    user = resolve_user(args.user, ctx.env, read_config(config_path(args.config, ctx.env)))
    with open_card_db(ctx) as db, ctx.client_factory() as http:
        fetcher = Fetcher(http)
        editor = ArchidektEditor(fetcher, ArchidektAuth(fetcher, ctx.token_store).access_token(user))
        service = BoardService(stored.folder, stored.deck, db, now=ctx.now)
        marked = mark(
            editor, workspace(args, ctx), stored, service, name=args.card, step=chosen, actor=Actor.YOU
        )
    print(f"{marked.card}: {marked.before or 'no label'} -> {marked.after or 'no label'}", file=ctx.out)
    return EXIT_OK


def _show(args: argparse.Namespace, ctx: Context) -> int:
    space = workspace(args, ctx)
    decks = [one_deck(space, args.deck).deck] if args.deck else [s.deck for s in space.load_all()]
    decks.sort(key=lambda d: d.short_name.casefold())
    if args.json:
        document = [{"deck": d.id, "short": d.short_name, "cards": to_get_json(d)} for d in decks]
        json.dump(document, ctx.out, ensure_ascii=False, indent=2)
        ctx.out.write("\n")
        return EXIT_OK
    for line in toget_lines(decks):
        print(line, file=ctx.out)
    return EXIT_OK


def toget_lines(decks: list[Deck]) -> list[str]:
    lines: list[str] = []
    for deck in decks:
        if not deck.labels_known:
            lines.append(f"{deck.short_name}: labels unknown; run `cmdr sync`")
            continue
        lines.extend(f"{deck.short_name}: {card['name']} ({card['label']})" for card in to_get_json(deck))
    return lines or ["nothing to get"]
