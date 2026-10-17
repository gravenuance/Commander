"""`cmdr analyze`, `cmdr price` and `cmdr bracket`: what a deck holds, what it costs, how strong it is."""

import argparse
from collections.abc import Callable, Sequence
from typing import TextIO

from commander.analysis import ANALYZED_ROLES, CURVE_TOP, DeckAnalysis, analyze
from commander.bracket import CACHE_FOLDER as SPELLBOOK_CACHE_FOLDER
from commander.bracket import BracketReport, EstimateResult, bracket_cards, estimate_cache, estimate_decks
from commander.bracketcheck import check_json
from commander.cli_output import ROLE_HEADINGS, TAG_ORDER, count_text, print_check, warn_answer, write_json
from commander.clibase import (
    EXIT_FAILURE,
    EXIT_OK,
    Context,
    day,
    non_negative,
    open_card_db,
    selected_decks,
)
from commander.config import cache_dir
from commander.deckcache import AnswerSource
from commander.deckprofile import read_profile
from commander.net import Fetcher
from commander.pricing import DeckPrice, PriceSource, format_eur, price_deck
from commander.spellbook import Combo, SpellbookClient, TwoCard

DEFAULT_TOP = 15
SPELLBOOK_CREDIT = "bracket estimates and combos from Commander Spellbook, https://commanderspellbook.com"
_TWO_CARD_ORDER = {TwoCard.YES: 0, TwoCard.ARGUABLY: 1, TwoCard.NO: 2}


def add_analysis_commands(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    analyze = add_parser("analyze", help="mana curve, card types, colours and roles")
    analyze.add_argument("deck", nargs="?", help="deck id, or part of its name or commander; omit for all")
    analyze.add_argument("--workspace", help="folder that holds the deck folders")
    analyze.add_argument(
        "-d", "--details", action="store_true", help="list cards where your categories and the tags differ"
    )
    analyze.add_argument("--json", action="store_true", help="print the analysis as JSON")
    analyze.set_defaults(handler=_cmd_analyze)

    price = add_parser("price", help="deck value in EUR (Cardmarket, via Scryfall)")
    price.add_argument("deck", nargs="?", help="deck id, or part of its name or commander; omit for all")
    price.add_argument("--workspace", help="folder that holds the deck folders")
    price.add_argument(
        "-t", "--top", type=non_negative, default=DEFAULT_TOP, help="cards to list for one deck (0 = all)"
    )
    price.add_argument("--json", action="store_true", help="print prices as JSON (amounts in euro cents)")
    price.set_defaults(handler=_cmd_price)

    bracket = add_parser("bracket", help="bracket estimate, game changers and combos")
    bracket.add_argument("deck", nargs="?", help="deck id, or part of its name or commander; omit for all")
    bracket.add_argument("--workspace", help="folder that holds the deck folders")
    bracket.add_argument(
        "-d", "--details", action="store_true", help="show what each combo does, with a link to it"
    )
    bracket.add_argument(
        "-r",
        "--refresh",
        action="store_true",
        help="ask Commander Spellbook again even if a recent answer is cached",
    )
    bracket.add_argument("--json", action="store_true", help="print the reports as JSON")
    bracket.set_defaults(handler=_cmd_bracket)


def _cmd_analyze(args: argparse.Namespace, ctx: Context) -> int:
    decks = selected_decks(args, ctx)
    with open_card_db(ctx) as db:
        analyses = [analyze(s.deck, db.resolve(s.deck.main_cards), db.available_roles) for s in decks]
    if args.json:
        write_json([_analysis_json(a) for a in analyses], ctx.out)
    elif args.deck:
        for analysis in analyses:
            _print_analysis(analysis, ctx.out, details=args.details)
    else:
        _print_analysis_table(analyses, ctx.out)
    return EXIT_OK


def _cmd_price(args: argparse.Namespace, ctx: Context) -> int:
    decks = selected_decks(args, ctx)
    with open_card_db(ctx) as db:
        prices = [price_deck(s.deck, db.resolve(s.deck.main_cards)) for s in decks]
        meta = db.meta()
    if args.json:
        write_json([_price_json(p) for p in prices], ctx.out)
        return EXIT_OK
    print(f"Cardmarket prices in EUR, from Scryfall data of {day(meta.cards_updated_at)}", file=ctx.out)
    if args.deck:
        for price in prices:
            _print_deck_price(price, ctx.out, top=args.top)
    else:
        _print_price_table(prices, ctx.out)
    return EXIT_OK


def _cmd_bracket(args: argparse.Namespace, ctx: Context) -> int:
    stored = selected_decks(args, ctx)
    decks = [s.deck for s in stored]
    ceilings = [read_profile(s.folder).bracket_ceiling for s in stored]
    with open_card_db(ctx) as db:
        cards = [bracket_cards(db.resolve(deck.main_cards), db.available_roles) for deck in decks]
    with ctx.client_factory() as http:
        results = estimate_decks(
            decks,
            SpellbookClient(Fetcher(http)),
            estimate_cache(cache_dir(ctx.env) / SPELLBOOK_CACHE_FOLDER),
            now=ctx.now,
            refresh=args.refresh,
        )
    reports = [
        BracketReport(d, c, r, ceiling)
        for d, c, r, ceiling in zip(decks, cards, results, ceilings, strict=True)
    ]
    for report in reports:
        warn_answer(report.deck, report.result, "bracket estimate", ctx.err)
    if args.json:
        write_json([_bracket_json(r) for r in reports], ctx.out)
    elif args.deck:
        for report in reports:
            _print_bracket(report, ctx.out, details=args.details)
        print(SPELLBOOK_CREDIT, file=ctx.out)
    else:
        _print_bracket_table(reports, ctx.out)
    unavailable = any(r.result.source is AnswerSource.UNAVAILABLE for r in reports)
    return EXIT_FAILURE if unavailable else EXIT_OK


def _print_analysis(analysis: DeckAnalysis, out: TextIO, *, details: bool) -> None:
    deck = analysis.deck
    commanders = " + ".join(deck.commanders) or "no commander"
    bracket = f"bracket {deck.bracket} on Archidekt" if deck.bracket is not None else "no bracket set"
    average = f"{analysis.average_mana_value:.2f}" if analysis.average_mana_value is not None else "-"
    print(f"{deck.name} ({deck.id})", file=out)
    print(f"{commanders}; {bracket}", file=out)
    print(f"{analysis.cards} cards, {analysis.lands} lands, average mana value {average} (nonland)", file=out)
    print("\nmana curve (nonland)", file=out)
    for value, count in enumerate(analysis.curve):
        label = f"{value}+" if value == CURVE_TOP else str(value)
        print(f"  {label:>2}  {count:>3}  {'#' * count}".rstrip(), file=out)
    types = ", ".join(f"{name} {count}" for name, count in analysis.types.items() if count)
    pips = ", ".join(f"{color} {count}" for color, count in analysis.pips.items() if count)
    print(f"\ntypes: {types or '-'}", file=out)
    print(f"pips: {pips or '-'}", file=out)
    print(f"\n  {'role':<12} {'yours':>5} {'tags':>5}", file=out)
    for role in analysis.roles:
        print(f"  {role.role.value:<12} {count_text(role.yours):>5} {count_text(role.tagged):>5}", file=out)
        if details and role.only_yours:
            print(f"      only yours: {', '.join(role.only_yours)}", file=out)
        if details and role.only_tagged:
            print(f"      only tags:  {', '.join(role.only_tagged)}", file=out)
    print(f"\ngame changers: {', '.join(analysis.game_changers) or 'none'}", file=out)
    if analysis.not_legal:
        print(f"not legal in Commander: {', '.join(analysis.not_legal)}", file=out)
    if analysis.unknown:
        print(f"not in the card data: {', '.join(analysis.unknown)}", file=out)
    print(file=out)


def _print_analysis_table(analyses: Sequence[DeckAnalysis], out: TextIO) -> None:
    roles = "".join(f" {ROLE_HEADINGS[role]}" for role in ANALYZED_ROLES)
    print(f"{'id':>9} {'lands':>5} {'avg mv':>6}{roles} {'gc':>3}  name", file=out)
    for a in analyses:
        average = f"{a.average_mana_value:.2f}" if a.average_mana_value is not None else "-"
        counts = "".join(f" {count_text(r.tagged):>{len(ROLE_HEADINGS[r.role])}}" for r in a.roles)
        print(
            f"{a.deck.id:>9} {a.lands:>5} {average:>6}{counts} {len(a.game_changers):>3}  {a.deck.name}",
            file=out,
        )
    print(
        "role counts come from Scryfall tags; `cmdr analyze <deck>` sets them against your categories",
        file=out,
    )


def _analysis_json(analysis: DeckAnalysis) -> dict[str, object]:
    return {
        "id": analysis.deck.id,
        "name": analysis.deck.name,
        "commanders": list(analysis.deck.commanders),
        "bracket": analysis.deck.bracket,
        "cards": analysis.cards,
        "lands": analysis.lands,
        "averageManaValue": analysis.average_mana_value,
        "curve": list(analysis.curve),
        "types": dict(analysis.types),
        "pips": dict(analysis.pips),
        "roles": [
            {
                "role": r.role.value,
                "yours": r.yours,
                "tagged": r.tagged,
                "onlyYours": list(r.only_yours),
                "onlyTagged": list(r.only_tagged),
            }
            for r in analysis.roles
        ],
        "gameChangers": list(analysis.game_changers),
        "notLegal": list(analysis.not_legal),
        "unknown": list(analysis.unknown),
    }


def _print_deck_price(price: DeckPrice, out: TextIO, *, top: int) -> None:
    deck = price.deck
    print(f"\n{deck.name} ({deck.id})", file=out)
    print(
        f"value {format_eur(price.total_cents)}; with the cheapest printings "
        f"{format_eur(price.cheapest_total_cents)}",
        file=out,
    )
    shown = price.cards if top == 0 else price.cards[:top]
    for card in shown:
        if card.source is PriceSource.NONE:
            continue
        mark = " ~" if card.source is PriceSource.CHEAPEST else ""
        print(f"  {format_eur(card.total_cents):>10}  {card.quantity:>2}x {card.name}{mark}", file=out)
    if len(shown) < len(price.cards):
        print(f"  ... {len(price.cards) - len(shown)} more; --top 0 lists them all", file=out)
    if price.estimated:
        print("~ this printing has no price, so the card's cheapest printing is used", file=out)
    if price.unpriced:
        print(f"no price at all: {', '.join(price.unpriced)}", file=out)


def _print_price_table(prices: Sequence[DeckPrice], out: TextIO) -> None:
    print(f"{'value':>11} {'cheapest':>11}  deck", file=out)
    for price in sorted(prices, key=lambda p: -p.total_cents):
        print(
            f"{format_eur(price.total_cents):>11} {format_eur(price.cheapest_total_cents):>11}  "
            f"{price.deck.name} ({price.deck.id})",
            file=out,
        )
    total = sum(p.total_cents for p in prices)
    cheapest = sum(p.cheapest_total_cents for p in prices)
    print(f"{format_eur(total):>11} {format_eur(cheapest):>11}  total", file=out)


def _price_json(price: DeckPrice) -> dict[str, object]:
    return {
        "id": price.deck.id,
        "name": price.deck.name,
        "totalCents": price.total_cents,
        "cheapestTotalCents": price.cheapest_total_cents,
        "cards": [
            {
                "name": c.name,
                "quantity": c.quantity,
                "unitCents": c.unit_cents,
                "totalCents": c.total_cents,
                "cheapestCents": c.cheapest_cents,
                "source": c.source.value,
            }
            for c in price.cards
        ],
    }


def _estimate_text(result: EstimateResult) -> str:
    if result.data is None or result.fetched_at is None:
        return "unavailable"
    tag = result.data.tag
    day = result.fetched_at.date().isoformat()
    if tag.minimum is None:
        return f"not legal in Commander ({tag.label}), Commander Spellbook {day}"
    return f"bracket {tag.brackets} ({tag.label}), Commander Spellbook {day}"


def _sorted_combos(combos: Sequence[Combo]) -> list[Combo]:
    return sorted(
        combos, key=lambda c: (TAG_ORDER[c.tag], _TWO_CARD_ORDER[c.two_card], len(c.cards), c.cards)
    )


def _card_list(label: str, names: Sequence[str] | None) -> str:
    if names is None:
        return f"{label}: unknown (not in the tag data)"
    if not names:
        return f"{label}: none"
    return f"{label} ({len(names)}): {', '.join(names)}"


def _print_bracket(report: BracketReport, out: TextIO, *, details: bool) -> None:
    deck, cards, result = report.deck, report.cards, report.result
    commanders = " + ".join(deck.commanders) or "no commander"
    bracket = f"bracket {deck.bracket} on Archidekt" if deck.bracket is not None else "no bracket set"
    print(f"{deck.name} ({deck.id})", file=out)
    print(f"{commanders}; {bracket}", file=out)
    print(f"estimate: {_estimate_text(result)}", file=out)
    print_check(report.check, out)
    print(_card_list("game changers", cards.game_changers), file=out)
    print(_card_list("tutors", cards.tutors), file=out)
    print(_card_list("mass land denial", cards.mass_land_denial), file=out)
    print(_card_list("extra turns", cards.extra_turns), file=out)
    print("(tutors, mass land denial and extra turns come from Scryfall tags)", file=out)
    if cards.not_legal:
        print(f"not legal in Commander: {', '.join(cards.not_legal)}", file=out)
    if result.data is not None:
        _print_combos(result.data.combos, out, details=details)
    print(file=out)


def _print_combos(combos: Sequence[Combo], out: TextIO, *, details: bool) -> None:
    if not combos:
        print("combos: none", file=out)
        return
    print(f"\ncombos ({len(combos)})", file=out)
    for combo in _sorted_combos(combos):
        size = {TwoCard.YES: "2-card", TwoCard.ARGUABLY: "2-card?"}.get(
            combo.two_card, f"{len(combo.cards)}-card"
        )
        print(f"  {combo.tag.label:<10} {size:<8}  {' + '.join(combo.cards)}", file=out)
        if details:
            print(f"  {'':<10} {'':<8}  -> {', '.join(combo.results) or 'no results listed'}", file=out)
            print(f"  {'':<10} {'':<8}  {combo.url}", file=out)
    if any(c.two_card is TwoCard.ARGUABLY for c in combos):
        print(
            "? Commander Spellbook counts it as arguably two cards",
            file=out,
        )


def _print_bracket_table(reports: Sequence[BracketReport], out: TextIO) -> None:
    print(
        f"{'id':>9} {'set':>3}  {'estimate':<16} {'gc':>3} {'tutors':>6} {'mld':>3} {'turns':>5} "
        f"{'2-card':>6} {'combos':>6}  name",
        file=out,
    )
    for r in reports:
        estimate = r.result.data
        tag = f"{estimate.tag.brackets} {estimate.tag.label}" if estimate is not None else "-"
        two_card = sum(1 for c in estimate.combos if c.two_card is TwoCard.YES) if estimate else None
        combos = len(estimate.combos) if estimate else None
        bracket = r.deck.bracket if r.deck.bracket is not None else "-"
        mark = "! " if r.check.notes else ""
        print(
            f"{r.deck.id:>9} {bracket:>3}  {tag:<16} {len(r.cards.game_changers):>3} "
            f"{_size(r.cards.tutors):>6} {_size(r.cards.mass_land_denial):>3} "
            f"{_size(r.cards.extra_turns):>5} {count_text(two_card):>6} "
            f"{count_text(combos):>6}  {mark}{r.deck.name}",
            file=out,
        )
    if any(r.check.notes for r in reports):
        print(
            "! the Archidekt bracket, the profile's ceiling and the estimate disagree; "
            "`cmdr bracket <deck>` says why",
            file=out,
        )
    print("set = bracket on Archidekt; tutors, mld and turns come from Scryfall tags", file=out)
    print(SPELLBOOK_CREDIT, file=out)


def _bracket_json(report: BracketReport) -> dict[str, object]:
    deck, cards, result = report.deck, report.cards, report.result
    estimate = result.data
    return {
        "id": deck.id,
        "name": deck.name,
        "commanders": list(deck.commanders),
        "bracket": deck.bracket,
        "estimate": {
            "source": result.source.value,
            "fetchedAt": result.fetched_at.isoformat() if result.fetched_at else None,
            "error": result.error,
            "tag": estimate.tag.label if estimate else None,
            "minimumBracket": estimate.tag.minimum if estimate else None,
            "brackets": estimate.tag.brackets if estimate else None,
        },
        "setBelowEstimate": report.set_below_estimate,
        "ceiling": report.ceiling,
        "check": check_json(report.check),
        "gameChangers": list(cards.game_changers),
        "tutors": _optional_list(cards.tutors),
        "massLandDenial": _optional_list(cards.mass_land_denial),
        "extraTurns": _optional_list(cards.extra_turns),
        "notLegal": list(cards.not_legal),
        "combos": [
            {
                "id": c.id,
                "url": c.url,
                "tag": c.tag.label,
                "cards": list(c.cards),
                "results": list(c.results),
                "twoCard": c.two_card.value,
            }
            for c in (_sorted_combos(estimate.combos) if estimate else [])
        ],
    }


def _size(names: Sequence[str] | None) -> str:
    return "-" if names is None else str(len(names))


def _optional_list(names: Sequence[str] | None) -> list[str] | None:
    return None if names is None else list(names)
