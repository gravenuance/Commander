"""`cmdr board`: the cards considered and suggested for a deck, decisions on them, and saving them."""

import argparse
import json
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from typing import TextIO

from commander.apply import fetch_own_deck
from commander.archidekt_edit import ArchidektAuth, ArchidektEditor
from commander.board import (
    Activity,
    Actor,
    Board,
    BoardCard,
    BoardService,
    NothingQueuedError,
    ReplaceOption,
    Section,
    Status,
    activity_json,
    board_json,
    read_activity,
    read_board,
)
from commander.boardsave import live_plan, preview_board, save_board
from commander.bracketpreview import spellbook_preview
from commander.carddb import CardDb
from commander.cli_output import (
    print_apply_outcome,
    print_bracket_preview,
    print_resolution,
    progress_printer,
    write_json,
)
from commander.clibase import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_USAGE,
    Context,
    UsageError,
    one_deck,
    open_card_db,
    workspace,
)
from commander.config import cache_dir, config_path, read_config, resolve_user
from commander.deckprofile import read_profile
from commander.net import Fetcher
from commander.plan import Plan
from commander.store import StoredDeck

_ACTORS = {actor.value: actor for actor in Actor if actor is not Actor.CMDR}
_DECISIONS = {"add": Status.ADD, "reject": Status.REJECTED, "reopen": Status.OPEN}
_STATUS_TEXT = {Status.OPEN: "open", Status.ADD: "add", Status.REJECTED: "no", Status.APPLIED: "in deck"}
_DESCRIPTION = """\
Cards considered or suggested for one deck: research on them, decisions, and saving them to Archidekt.
The desktop app shows the same board; every change lands in the deck's activity log.

examples:
  cmdr board show ellivere
  cmdr board add ellivere "Sylvan Safekeeper" --reason "protects stax pieces" --replace "Compost=not stax"
  cmdr board decide ellivere "Sylvan Safekeeper" add --cut Compost
  cmdr board log --since 2026-10-15T18:00
  cmdr board commit ellivere          preview the queued changes against Archidekt
  cmdr board commit ellivere --save   make them, then update the local copy
"""


def add_board_commands(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    board = add_parser(
        "board",
        help="cards considered or suggested for a deck, decisions, and saving them",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    actions = board.add_subparsers(title="actions", metavar="<action>")

    show = _deck_action(actions.add_parser, "show", "the board of one deck")
    show.add_argument("--json", action="store_true", help="print the board as JSON")
    show.set_defaults(handler=_cmd_show)

    add = _deck_action(actions.add_parser, "add", "put a card on the board to research", by=True)
    add.add_argument("card", help="card name")
    add.add_argument("--suggested", action="store_true", help="list it as suggested, not considered")
    add.add_argument("--reason", default="", help="why it might fit")
    _replace_argument(add)
    add.set_defaults(handler=_cmd_add)

    research = _deck_action(actions.add_parser, "research", "record research on a card", by=True)
    research.add_argument("card", help="card name or board id")
    research.add_argument("--reason", help="why it fits or does not")
    _replace_argument(research)
    research.add_argument("--clear-replace", action="store_true", help="drop every card to replace")
    research.add_argument("--move", choices=[s.value for s in Section], help="move it to the other list")
    research.set_defaults(handler=_cmd_research)

    decide = _deck_action(actions.add_parser, "decide", "add a card, turn it down, or reopen it", by=True)
    decide.add_argument("card", help="card name or board id")
    decide.add_argument("decision", choices=list(_DECISIONS), help="what to do with it")
    decide.add_argument("--cut", metavar="CARD", help="the deck card that makes room (with add)")
    decide.add_argument("--note", default="", help="why")
    decide.set_defaults(handler=_cmd_decide)

    remove = _deck_action(actions.add_parser, "remove", "take a card off the board", by=True)
    remove.add_argument("card", help="card name or board id")
    remove.set_defaults(handler=_cmd_remove)

    log = actions.add_parser("log", help="what changed on the boards, oldest first")
    log.add_argument("deck", nargs="?", help="deck id, or part of its name or commander; omit for all")
    log.add_argument("--workspace", help="folder that holds the deck folders")
    log.add_argument("--since", metavar="TIME", help="only after this UTC time, like 2026-10-15T18:00")
    log.add_argument("--json", action="store_true", help="print the entries as JSON")
    log.set_defaults(handler=_cmd_log)

    plan = _deck_action(
        actions.add_parser,
        "plan",
        "print the queued cards, as the deck on Archidekt needs them, as a plan file for `cmdr apply`",
    )
    plan.add_argument("--user", help="Archidekt username cmdr is set up for")
    plan.set_defaults(handler=_cmd_plan)

    commit = _deck_action(
        actions.add_parser, "commit", "preview the queued cards on Archidekt; --save makes them"
    )
    commit.add_argument("--user", help="Archidekt username cmdr is set up for")
    commit.add_argument("--save", action="store_true", help="make the changes on Archidekt")
    commit.set_defaults(handler=_cmd_commit)


def _deck_action(
    add_parser: Callable[..., argparse.ArgumentParser], name: str, help_text: str, *, by: bool = False
) -> argparse.ArgumentParser:
    action = add_parser(name, help=help_text)
    action.add_argument("deck", help="deck id, or part of its name or commander")
    action.add_argument("--workspace", help="folder that holds the deck folders")
    if by:
        action.add_argument("--by", choices=list(_ACTORS), default="you", help="who is making the change")
    return action


def _replace_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--replace",
        action="append",
        default=None,
        metavar="CARD[=REASON]",
        help="a deck card it could replace, and why; repeat for more",
    )


@contextmanager
def _board(args: argparse.Namespace, ctx: Context) -> Generator[tuple[StoredDeck, BoardService, CardDb]]:
    stored = one_deck(workspace(args, ctx), args.deck)
    with open_card_db(ctx) as db:
        yield stored, BoardService(stored.folder, stored.deck, db, now=ctx.now), db


def _cmd_show(args: argparse.Namespace, ctx: Context) -> int:
    stored = one_deck(workspace(args, ctx), args.deck)
    board = read_board(stored.folder)
    if args.json:
        write_json({"deck": stored.deck.id, **board_json(board)}, ctx.out)
        return EXIT_OK
    print_board(stored, board, ctx.out)
    return EXIT_OK


def _cmd_add(args: argparse.Namespace, ctx: Context) -> int:
    section = Section.SUGGESTED if args.suggested else Section.CONSIDERED
    with _board(args, ctx) as (stored, service, _):
        card = service.propose(
            args.card,
            _ACTORS[args.by],
            section=section,
            reason=args.reason,
            replace_options=_options(args.replace),
        )
    print(
        f"{card.name} is on the {stored.deck.short_name} board ({card.section.value}, id {card.id})",
        file=ctx.out,
    )
    return EXIT_OK


def _cmd_research(args: argparse.Namespace, ctx: Context) -> int:
    options = [] if args.clear_replace else (_options(args.replace) if args.replace is not None else None)
    with _board(args, ctx) as (_, service, _):
        card = service.research(
            args.card,
            _ACTORS[args.by],
            reason=args.reason,
            replace_options=options,
            section=Section(args.move) if args.move else None,
        )
    print(f"updated {card.name}", file=ctx.out)
    return EXIT_OK


def _cmd_decide(args: argparse.Namespace, ctx: Context) -> int:
    status = _DECISIONS[args.decision]
    if args.cut and status is not Status.ADD:
        raise UsageError("--cut goes with add")
    with _board(args, ctx) as (_, service, _):
        card = service.decide(args.card, status, _ACTORS[args.by], choice=args.cut, note=args.note)
    cut = f", cutting {card.choice}" if card.choice else ""
    print(f"{card.name}: {_STATUS_TEXT[card.status]}{cut}", file=ctx.out)
    return EXIT_OK


def _cmd_remove(args: argparse.Namespace, ctx: Context) -> int:
    with _board(args, ctx) as (_, service, _):
        service.remove(args.card, _ACTORS[args.by])
    print(f"removed {args.card.strip()}", file=ctx.out)
    return EXIT_OK


def _cmd_log(args: argparse.Namespace, ctx: Context) -> int:
    space = workspace(args, ctx)
    decks = [one_deck(space, args.deck)] if args.deck else space.load_all()
    entries = sorted(
        ((entry, stored) for stored in decks for entry in read_activity(stored.folder, since=args.since)),
        key=lambda pair: pair[0].at,
    )
    if args.json:
        rows = [{"deck": s.deck.id, "deckName": s.deck.short_name, **activity_json(e)} for e, s in entries]
        write_json(rows, ctx.out)
        return EXIT_OK
    if not entries:
        print("nothing changed" + (f" since {args.since}" if args.since else ""), file=ctx.out)
    for entry, stored in entries:
        print(activity_line(entry, stored.deck.short_name), file=ctx.out)
    return EXIT_OK


def _cmd_plan(args: argparse.Namespace, ctx: Context) -> int:
    stored = one_deck(workspace(args, ctx), args.deck)
    user = resolve_user(args.user, ctx.env, read_config(config_path(args.config, ctx.env)))
    with ctx.client_factory() as http:
        fetcher = Fetcher(http)
        auth = ArchidektAuth(fetcher, ctx.token_store)
        # Reading a public deck needs no login; a private one does.
        editor = ArchidektEditor(fetcher, auth.access_token(user) if auth.has_login(user) else None)
        live = fetch_own_deck(editor, stored.deck.id, user=user)
    plan = live_plan(live, read_board(stored.folder))
    if plan is None:
        raise NothingQueuedError("every queued card is already in the deck on Archidekt")
    ctx.out.write(plan_toml(plan))
    return EXIT_OK


def _cmd_commit(args: argparse.Namespace, ctx: Context) -> int:
    user = resolve_user(args.user, ctx.env, read_config(config_path(args.config, ctx.env)))
    with _board(args, ctx) as (stored, service, db), ctx.client_factory() as http:
        fetcher = Fetcher(http)
        editor = ArchidektEditor(fetcher, ArchidektAuth(fetcher, ctx.token_store).access_token(user))
        if not args.save:
            outcome = preview_board(editor, stored, db, user=user, now=ctx.now)
            ceiling = read_profile(stored.folder).bracket_ceiling
            preview = spellbook_preview(
                outcome.resolution, fetcher, cache_dir(ctx.env), ceiling=ceiling, now=ctx.now
            )
            print_resolution(outcome.resolution, ctx.out)
            print_bracket_preview(preview, ctx.out)
            print_apply_outcome(outcome.resolution, None, None, ctx.out, save=False)
            return EXIT_USAGE if outcome.resolution.problems else EXIT_OK
        saved = save_board(
            editor,
            workspace(args, ctx),
            stored,
            db,
            service,
            user=user,
            actor=Actor.YOU,
            now=ctx.now,
            on_progress=progress_printer(ctx.err),
        )
    print_resolution(saved.resolution, ctx.out)
    print_apply_outcome(saved.resolution, saved.result, stored.folder, ctx.out, save=True)
    if saved.result is None and saved.applied:
        print(f"marked applied, already in the deck: {', '.join(saved.applied)}", file=ctx.out)
    if saved.resolution.problems:
        return EXIT_USAGE
    return EXIT_FAILURE if saved.result is not None and saved.result.mismatches else EXIT_OK


def print_board(stored: StoredDeck, board: Board, out: TextIO) -> None:
    deck = stored.deck
    print(f"{deck.short_name}: {deck.name} ({deck.id})", file=out)
    if not board.cards:
        print(f"the board is empty; `cmdr board add {deck.id} <card>` puts a card on it", file=out)
        return
    for section in Section:
        cards = [c for c in board.cards if c.section is section]
        if not cards:
            continue
        print(f"\n{section.value}", file=out)
        for card in cards:
            _print_card(card, out)
    queued = board.queued
    if queued:
        cuts = sum(1 for c in queued if c.choice)
        print(
            f"\nqueued: {len(queued)} to add, {cuts} to cut; `cmdr board commit {deck.id}` previews", file=out
        )


def _print_card(card: BoardCard, out: TextIO) -> None:
    status = _STATUS_TEXT[card.status]
    cut = f", cut {card.choice}" if card.choice else ""
    print(f"  [{status}{cut}] {card.name}  (by {card.source.value}, id {card.id})", file=out)
    if card.reason:
        print(f"      {card.reason}", file=out)
    for option in card.replace:
        why = f": {option.reason}" if option.reason else ""
        print(f"      could replace {option.name}{why}", file=out)
    if card.note:
        print(f"      note: {card.note}", file=out)


def activity_line(entry: Activity, deck: str) -> str:
    card = f" {entry.card}" if entry.card else ""
    detail = f": {entry.detail}" if entry.detail else ""
    return f"{entry.at}  {deck}  {entry.actor.value} {entry.action}{card}{detail}"


def plan_toml(plan: Plan) -> str:
    lines = [f"deck = {_toml(plan.deck)}"]
    if plan.add:
        lines.append("add = [" + ", ".join(_toml(a.name) for a in plan.add) + "]")
    if plan.remove:
        removes = (
            f"{{name = {_toml(r.name)}, maybeboard = true}}" if r.maybeboard else _toml(r.name)
            for r in plan.remove
        )
        lines.append("remove = [" + ", ".join(removes) + "]")
    if plan.drop:
        lines.append("drop = [" + ", ".join(_toml(name) for name in plan.drop) + "]")
    return "\n".join(lines) + "\n"


def _options(values: Sequence[str] | None) -> list[ReplaceOption]:
    options: list[ReplaceOption] = []
    for value in values or ():
        name, _, reason = value.partition("=")
        if not name.strip():
            raise UsageError(f"--replace {value!r} names no card; use CARD or CARD=REASON")
        options.append(ReplaceOption(name.strip(), reason.strip()))
    return options


def _toml(text: str) -> str:
    # A JSON string literal is also a valid TOML basic string, escapes included.
    return json.dumps(text, ensure_ascii=False)
