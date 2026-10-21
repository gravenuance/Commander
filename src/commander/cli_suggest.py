"""`cmdr suggest`: cards to add for combos and short roles, and cuts to weigh."""

import argparse
from collections.abc import Callable, Sequence
from dataclasses import replace
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import TextIO

from commander.analysis import ANALYZED_ROLES
from commander.board import BoardService, feed_suggestions, read_board
from commander.changetext import raise_text
from commander.cli_output import (
    ROLE_HEADINGS,
    TAG_ORDER,
    count_text,
    price_text,
    raise_json,
    warn_answer,
    write_json,
)
from commander.clibase import (
    EXIT_FAILURE,
    EXIT_OK,
    Context,
    UsageError,
    day,
    estimate_floor,
    open_card_db,
    positive,
    selected_decks,
)
from commander.config import cache_dir, config_path, read_config
from commander.deckcache import AnswerSource
from commander.deckprofile import read_profile
from commander.deckthemes import merge_themes
from commander.net import Fetcher
from commander.spellbook import SpellbookClient, Variant
from commander.suggest import CACHE_FOLDER as COMBOS_CACHE_FOLDER
from commander.suggest import (
    DEFAULT_COUNT,
    RoleIdea,
    RoleTarget,
    Suggestions,
    combos_cache,
    find_combos,
    options_for,
    suggest,
)


def add_suggest_command(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    suggest_ = add_parser("suggest", help="cards to add for combos and short roles, cuts to weigh")
    suggest_.add_argument("deck", nargs="?", help="deck id, or part of its name or commander; omit for all")
    suggest_.add_argument("--workspace", help="folder that holds the deck folders")
    suggest_.add_argument(
        "-n",
        "--count",
        type=positive,
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
        "--any-price",
        action="store_true",
        help="ignore the deck's own budget below bracket 5 (its average and dearest card)",
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
        "--to-board",
        action="store_true",
        help="put the ideas on the deck's board under considering (one deck)",
    )
    suggest_.set_defaults(handler=_cmd_suggest)


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
                deck_budget=not args.any_price,
                rejected=read_board(folder).rejected_names,
            )
            # An unset deck's marks and budget go by its estimate's lowest bracket (ADR 0051); its
            # --within-bracket ceiling stays on the bracket set, so an estimate alone hides no idea.
            floor = estimate_floor(deck, ctx) if deck.bracket is None else None
            playing = replace(deck, bracket=floor) if floor is not None else deck
            results.append(suggest(playing, db.resolve(deck.main_cards), db, answer, options))
            if args.to_board:
                added = feed_suggestions(
                    BoardService(folder, deck, db, now=ctx.now), results[-1], read_board(folder)
                )
                print(f"put {len(added)} idea(s) on the {deck.short_name} board", file=ctx.err)
        meta = db.meta()
    for result in results:
        warn_answer(result.deck, result.combos, "combo list", ctx.err)
    if args.json:
        write_json([_suggestions_json(r, d.bracket) for r, d in zip(results, decks, strict=True)], ctx.out)
    elif args.deck:
        print(
            f"prices: each card's cheapest printing in EUR, Scryfall data of {day(meta.cards_updated_at)}",
            file=ctx.out,
        )
        for result, deck in zip(results, decks, strict=True):
            _print_suggestions(result, ctx.out, details=args.details, set_bracket=deck.bracket)
        print(SUGGEST_CREDIT, file=ctx.out)
    else:
        _print_suggestions_table(results, [deck.bracket for deck in decks], ctx.out)
    unavailable = any(r.combos.source is AnswerSource.UNAVAILABLE for r in results)
    return EXIT_FAILURE if unavailable else EXIT_OK


SUGGEST_CREDIT = (
    "combos from Commander Spellbook, https://commanderspellbook.com; "
    "popularity ranks from EDHREC, https://edhrec.com, via Scryfall"
)


def _print_suggestions(result: Suggestions, out: TextIO, *, details: bool, set_bracket: int | None) -> None:
    """One deck's ideas; set_bracket is the bracket on Archidekt, where result.deck has the one marked by."""
    deck = result.deck
    commanders = " + ".join(deck.commanders) or "no commander"
    if set_bracket is not None:
        bracket = f"bracket {set_bracket} on Archidekt"
    elif deck.bracket is not None:
        bracket = f"no bracket set, at least {deck.bracket} by its estimate"
    else:
        bracket = "no bracket set"
    colours = "colours unknown" if result.identity is None else f"colours {result.identity or 'colourless'}"
    print(f"\n{deck.name} ({deck.id})", file=out)
    print(f"{commanders}; {bracket}; {colours}", file=out)
    budget = result.budget
    if budget.working is not None:
        ceiling = f", nothing over {price_text(budget.ceiling)}" if budget.ceiling is not None else ""
        within = f", within budget up to {price_text(budget.line)}" if budget.line is not None else ""
        print(f"budget: average card {price_text(budget.working)}{within}{ceiling}", file=out)
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
        tags = sorted({v.tag for v in piece.combos}, key=lambda t: TAG_ORDER[t])
        combos = f"{len(piece.combos)} combo{'' if len(piece.combos) == 1 else 's'}"
        over = "" if result.budget.within(piece.price_cents) else " (over budget)"
        print(
            f"  {price_text(piece.price_cents):>9}  {piece.name} ({combos}: "
            f"{', '.join(t.label for t in tags)}){over}{raise_text(piece.raises)}",
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
    over = "" if idea.within_budget else " (over budget)"
    print(f"  {price:>9}  {idea.info.name}{theme}{over}{raise_text(idea.raises)}", file=out)


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


def _print_suggestions_table(
    results: Sequence[Suggestions], set_brackets: Sequence[int | None], out: TextIO
) -> None:
    roles = [role for role in ANALYZED_ROLES if any(t.role is role for r in results for t in r.roles)]
    widths = {role: max(len(ROLE_HEADINGS[role]), 5) for role in roles}
    headings = "".join(f" {ROLE_HEADINGS[role]:>{widths[role]}}" for role in roles)
    print(f"{'id':>9} {'set':>3} {'combo':>5}{headings} {'cuts':>4}  name", file=out)
    for r, set_bracket in zip(results, set_brackets, strict=True):
        pieces = len(r.pieces) + r.more_pieces if r.combos.data is not None else None
        targets = {t.role: t for t in r.roles}
        cells = "".join(f" {_target_text(targets.get(role)):>{widths[role]}}" for role in roles)
        bracket = set_bracket if set_bracket is not None else "-"
        print(
            f"{r.deck.id:>9} {bracket:>3} {count_text(pieces):>5}{cells} {len(r.cuts):>4}  {r.deck.name}",
            file=out,
        )
    print("combo = cards that would complete a combo; roles = nonland cards tagged / target", file=out)
    print("`cmdr suggest <deck>` lists the cards", file=out)
    print(SUGGEST_CREDIT, file=out)


def _target_text(target: RoleTarget | None) -> str:
    return "-" if target is None else f"{target.have}/{target.target}"


def _suggestions_json(result: Suggestions, set_bracket: int | None) -> dict[str, object]:
    deck, answer = result.deck, result.combos
    return {
        "id": deck.id,
        "name": deck.name,
        "commanders": list(deck.commanders),
        "bracket": set_bracket,
        # The bracket the marks and budget go by: the one set, else the estimate's lowest (ADR 0051).
        "effectiveBracket": deck.bracket,
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
                "withinBudget": result.budget.within(p.price_cents),
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
        "budget": result.budget.json(),
    }


def _idea_json(idea: RoleIdea) -> dict[str, object]:
    return {
        "name": idea.info.name,
        "priceCents": idea.info.cheapest_cents,
        "edhrecRank": idea.info.edhrec_rank,
        "raises": raise_json(idea.raises),
        "themed": idea.themed,
        "fits": list(idea.fits),
        "withinBudget": idea.within_budget,
    }


def _euro_cents(text: str) -> int:
    # A decimal comma is accepted too, as euro amounts are often written that way.
    try:
        amount = Decimal(text.strip().replace(",", "."))
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not an amount in euros: {text!r}") from None
    if not amount.is_finite() or amount < 0:
        raise argparse.ArgumentTypeError(f"not an amount in euros: {text!r}")
    return int((amount * 100).to_integral_value(rounding=ROUND_FLOOR))
