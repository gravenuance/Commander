"""`cmdr tokens`: the tokens, emblems and helper cards each deck's cards make (ADR 0061)."""

import argparse
from collections.abc import Callable
from dataclasses import dataclass, field

from commander.carddb import CardToken
from commander.cli_output import write_json
from commander.clibase import EXIT_OK, Context, open_card_db, selected_decks
from commander.models import Deck
from commander.tokens import DeckTokens, describe, hint, short_text, token_json, tokens_of_deck

EXAMPLES = """\
examples:
  cmdr tokens                       every token any deck makes, once, with the cards that make it per deck
  cmdr tokens ellivere              one deck's tokens, each with the cards of the deck that make it
  cmdr tokens ellivere --json       the same as JSON, for scripts
"""
CHECK_NOTE = "Scryfall links no token to these, though their text creates one"
LEDGER_TEXT_CHARS = 70


def add_token_commands(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    tokens = add_parser(
        "tokens",
        help="tokens, emblems and helper cards that each deck's cards make",
        description=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    tokens.add_argument("deck", nargs="?", help="deck id, or part of its name or commander; omit for all")
    tokens.add_argument("--workspace", help="folder that holds the deck folders")
    tokens.add_argument("--json", action="store_true", help="print the tokens as JSON")
    tokens.set_defaults(handler=_cmd_tokens)


@dataclass(slots=True)
class _LedgerRow:
    token: CardToken
    # Per deck, the cards of it that make the token.
    decks: list[tuple[Deck, tuple[str, ...]]] = field(default_factory=list[tuple[Deck, tuple[str, ...]]])


def _cmd_tokens(args: argparse.Namespace, ctx: Context) -> int:
    stored = selected_decks(args, ctx)
    with open_card_db(ctx) as db:
        made = [(s.deck, tokens_of_deck(db, s.deck)) for s in stored]
    if args.deck:
        deck, tokens = made[0]
        if args.json:
            write_json(_deck_json(deck, tokens), ctx.out)
        else:
            _print_deck(deck, tokens, ctx)
        return EXIT_OK
    ledger = _ledger(made)
    if args.json:
        write_json(_ledger_json(ledger, made), ctx.out)
    else:
        _print_ledger(ledger, made, ctx)
    return EXIT_OK


def _ledger(made: list[tuple[Deck, DeckTokens]]) -> list[_LedgerRow]:
    """Each distinct token once, the ones more decks make first."""
    rows: dict[str, _LedgerRow] = {}
    for deck, tokens in made:
        for row in tokens.tokens:
            known = rows.setdefault(row.token.oracle_id, _LedgerRow(row.token))
            known.decks.append((deck, row.makers))
    return sorted(rows.values(), key=lambda r: (-len(r.decks), r.token.name.casefold(), r.token.oracle_id))


def _print_deck(deck: Deck, tokens: DeckTokens, ctx: Context) -> None:
    count = len(tokens.tokens)
    print(f"{deck.name} ({deck.id}): {count} token{'s' if count != 1 else ''}", file=ctx.out)
    for row in tokens.tokens:
        print(f"  {describe(row.token)}: {', '.join(row.makers)}", file=ctx.out)
    if tokens.check_by_hand:
        print(f"{CHECK_NOTE}: {', '.join(tokens.check_by_hand)}", file=ctx.out)


def _print_ledger(ledger: list[_LedgerRow], made: list[tuple[Deck, DeckTokens]], ctx: Context) -> None:
    for row in ledger:
        token = row.token
        # A creature token's text is already in its description.
        text = "" if hint(token) else short_text(token, LEDGER_TEXT_CHARS)
        print(f"{describe(token)}  [{token.type_line}]{f'  {text}' if text else ''}", file=ctx.out)
        per_deck = (f"{deck.short_name} ({', '.join(makers)})" for deck, makers in row.decks)
        print("  " + "; ".join(per_deck), file=ctx.out)
    unchecked = [(deck.short_name, tokens.check_by_hand) for deck, tokens in made if tokens.check_by_hand]
    if unchecked:
        print(f"\n{CHECK_NOTE}:", file=ctx.out)
        for name, cards in unchecked:
            print(f"  {name}: {', '.join(cards)}", file=ctx.out)
    if not ledger:
        print("no deck makes a token", file=ctx.out)


def _deck_json(deck: Deck, tokens: DeckTokens) -> dict[str, object]:
    return {
        "id": deck.id,
        "name": deck.name,
        "tokens": [token_json(row.token) | {"makers": list(row.makers)} for row in tokens.tokens],
        "checkByHand": list(tokens.check_by_hand),
    }


def _ledger_json(ledger: list[_LedgerRow], made: list[tuple[Deck, DeckTokens]]) -> dict[str, object]:
    return {
        "tokens": [
            token_json(row.token)
            | {
                "decks": [
                    {"id": deck.id, "deck": deck.short_name, "makers": list(makers)}
                    for deck, makers in row.decks
                ]
            }
            for row in ledger
        ],
        "checkByHand": [
            {"id": deck.id, "deck": deck.short_name, "cards": list(tokens.check_by_hand)}
            for deck, tokens in made
            if tokens.check_by_hand
        ],
    }
