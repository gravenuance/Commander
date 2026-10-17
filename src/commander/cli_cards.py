"""`cmdr card` and `cmdr tags`: look up a card's rules text and tags, and find Tagger tags by name."""

import argparse
from collections.abc import Callable

from commander.carddb import CardDb, CardInfo, CardText
from commander.cli_output import write_json
from commander.clibase import EXIT_OK, Context, UsageError, open_card_db
from commander.pricing import format_eur

TAGGER_CREDIT = "tags from Scryfall Tagger, https://tagger.scryfall.com"


def add_card_commands(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    card = add_parser("card", help="a card's rules text, roles, price and Tagger tags")
    card.add_argument("name", nargs="+", help="card name (any case; the front face is enough)")
    card.add_argument("--json", action="store_true", help="print the cards as JSON (prices in euro cents)")
    card.set_defaults(handler=_cmd_card)
    tags = add_parser("tags", help="Tagger tags whose name contains the text, for deck profiles")
    tags.add_argument("text", help="part of a tag name, such as stax or blink")
    tags.add_argument("--json", action="store_true", help="print the tags as JSON")
    tags.set_defaults(handler=_cmd_tags)


def look_up(db: CardDb, names: list[str]) -> list[tuple[CardInfo, CardText]]:
    """Each named card with its text; an unknown name stops the lookup with the name in the message."""
    found: list[tuple[CardInfo, CardText]] = []
    for name in names:
        info = db.card_named(name)
        text = db.card_text(info.oracle_id) if info else None
        if info is None or text is None:
            raise UsageError(f"no card named {name!r} in the card data (check the spelling)")
        found.append((info, text))
    return found


def card_json(info: CardInfo, text: CardText) -> dict[str, object]:
    return {
        "name": info.name,
        "oracleId": info.oracle_id,
        "manaCost": info.mana_cost,
        "manaValue": info.cmc,
        "typeLine": info.type_line,
        "colorIdentity": info.color_identity,
        "power": text.power,
        "toughness": text.toughness,
        "text": text.text,
        "keywords": list(text.keywords),
        "roles": sorted(role.value for role in info.roles),
        "tags": list(text.tags),
        "gameChanger": info.game_changer,
        "commanderLegal": info.commander_legal,
        "edhrecRank": info.edhrec_rank,
        "priceCents": info.cheapest_cents,
    }


def _cmd_card(args: argparse.Namespace, ctx: Context) -> int:
    with open_card_db(ctx) as db:
        cards = look_up(db, args.name)
    if args.json:
        write_json([card_json(info, text) for info, text in cards], ctx.out)
        return EXIT_OK
    for index, (info, text) in enumerate(cards):
        if index:
            print(file=ctx.out)
        _print_card(info, text, ctx)
    print(TAGGER_CREDIT, file=ctx.out)
    return EXIT_OK


def _print_card(info: CardInfo, text: CardText, ctx: Context) -> None:
    stats = f"  {text.power}/{text.toughness}" if text.power is not None else ""
    print(f"{info.name}  {info.mana_cost}".rstrip(), file=ctx.out)
    print(f"{info.type_line}{stats}", file=ctx.out)
    for line in text.text.splitlines():
        print(f"  {line}", file=ctx.out)
    facts = [
        f"EDHREC #{info.edhrec_rank:,}" if info.edhrec_rank else "unranked",
        format_eur(info.cheapest_cents) if info.cheapest_cents is not None else "no EUR price",
    ]
    if info.game_changer:
        facts.append("game changer")
    if not info.commander_legal:
        facts.append("not legal in Commander")
    print("; ".join(facts), file=ctx.out)
    if info.roles:
        print(f"roles: {', '.join(sorted(role.value for role in info.roles))}", file=ctx.out)
    if text.tags:
        print(f"tags: {', '.join(text.tags)}", file=ctx.out)


def _cmd_tags(args: argparse.Namespace, ctx: Context) -> int:
    with open_card_db(ctx) as db:
        found = db.find_tags(args.text)
    if args.json:
        write_json([{"slug": t.slug, "cards": t.cards} for t in found], ctx.out)
        return EXIT_OK
    if not found:
        print(f"no tag contains {args.text.strip()!r}", file=ctx.out)
        return EXIT_OK
    width = max(len(t.slug) for t in found)
    for tag in found:
        print(f"{tag.slug:<{width}}  {tag.cards:>6,} cards", file=ctx.out)
    print(TAGGER_CREDIT, file=ctx.out)
    return EXIT_OK
