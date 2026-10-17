"""`cmdr profile`: show a deck's profile as suggest reads it, or start one from a commented template."""

import argparse
from collections.abc import Callable, Sequence

from commander.cli_meta import meta_sources
from commander.cli_output import write_json
from commander.clibase import EXIT_OK, Context, one_deck, open_card_db, workspace
from commander.config import cache_dir, config_path, read_config
from commander.deckprofile import (
    Profile,
    ThemeSpec,
    profile_path,
    read_profile,
    resolve_themes,
    write_profile_template,
)
from commander.deckthemes import BroadKind, DerivedTheme, MergedThemes, ThemeSource, broad_kind, merge_themes
from commander.leanings import Leaning, leaning_json, leaning_text
from commander.metadata import gather_for
from commander.pricing import format_eur
from commander.targets import deck_cut_rules

_BROAD_TAGS = {
    BroadKind.PLAY_STYLE: "how the deck plays, not a theme",
    BroadKind.ROLE: "a role, not a theme",
    BroadKind.CARD_TYPE: "a card type, not a theme",
}


def add_profile_commands(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    profile = add_parser("profile", help="a deck's profile: themes, exclusions and targets for suggest")
    profile.add_argument("deck", help="deck id, or part of its name or commander")
    profile.add_argument("--workspace", help="folder that holds the deck folders")
    profile.add_argument("--init", action="store_true", help="write a commented profile.toml to fill in")
    profile.add_argument("--json", action="store_true", help="print the profile as JSON")
    profile.set_defaults(handler=_cmd_profile)


def _cmd_profile(args: argparse.Namespace, ctx: Context) -> int:
    stored = one_deck(workspace(args, ctx), args.deck)
    if args.init:
        path = write_profile_template(stored.folder, stored.deck)
        print(f"wrote {path}; fill it in, then `cmdr profile {stored.deck.id}` checks it", file=ctx.out)
        return EXIT_OK
    profile = read_profile(stored.folder)
    file = read_config(config_path(args.config, ctx.env))
    with open_card_db(ctx) as db, ctx.client_factory() as http:
        unknown = resolve_themes(profile, db).unknown_tags
        merged = merge_themes(stored.deck, profile, db)
        # Leaning tags can be ignored too, so only names that are no Tagger tag either are unknown.
        tags = db.tag_ids(name.strip().casefold() for name in merged.unknown_ignores)
        unknown_ignores = [name for name in merged.unknown_ignores if name.strip().casefold() not in tags]
        data = None
        if stored.deck.commanders:
            sources = meta_sources(http, ctx, file.user or "", file.meta)
            data = gather_for(
                stored.deck, sources, cache_dir(ctx.env), now=ctx.now, refresh=False, offline=True
            )
        leanings = deck_cut_rules(
            stored.deck, db.resolve(stored.deck.main_cards), profile, db, data, now=ctx.now
        ).leanings
    for slug in unknown:
        print(f"cmdr: tag {slug!r} is not a Tagger tag; `cmdr tags` finds them", file=ctx.err)
    for name in unknown_ignores:
        print(
            f"cmdr: the profile ignores {name!r}, which is no deck tag, category or Tagger tag", file=ctx.err
        )
    if args.json:
        document = {
            **profile_json(profile, unknown),
            **derived_json(merged),
            "unknownIgnores": unknown_ignores,
            "leanings": [leaning_json(leaning) for leaning in leanings],
        }
        write_json(document, ctx.out)
        return EXIT_OK
    path = profile_path(stored.folder)
    print(f"{stored.deck.name} ({stored.deck.id})", file=ctx.out)
    if path.exists():
        lines = profile_lines(profile)
    else:
        lines = [f"no profile; `cmdr profile {stored.deck.id} --init` starts one"]
    for line in [*lines, *derived_lines(merged), *leaning_lines(leanings)]:
        print(line, file=ctx.out)
    return EXIT_OK


def leaning_lines(leanings: Sequence[Leaning]) -> list[str]:
    """What the deck plays far more of than recent decks with its commander (ADR 0016)."""
    if not leanings:
        return []
    peers = leanings[0].peers
    lines = [f"leans into, against {peers} recent decks with its commander (cuts only like for like):"]
    lines += [f"  {leaning_text(leaning)}" for leaning in leanings]
    return lines


def profile_lines(profile: Profile) -> list[str]:
    targets = ", ".join(f"{role.value} {n}" for role, n in profile.targets.items())
    rows = [
        ("archetype", profile.archetype),
        ("bracket ceiling", str(profile.bracket_ceiling) if profile.bracket_ceiling else ""),
        ("price limit", format_eur(profile.max_card_cents) if profile.max_card_cents is not None else ""),
        ("targets", targets),
        (
            "themes",
            "; ".join(filter(None, [_spec_text(profile.themes), _names(profile.ignore_themes, "ignore")])),
        ),
        ("exclude", "; ".join(filter(None, [_spec_text(profile.exclude), _names(profile.exclude_cards)]))),
        (
            "keep",
            "; ".join(
                filter(None, [_names(profile.keep_cards), _names(profile.keep_categories, "categories")])
            ),
        ),
        ("ideas also from", "; ".join(" + ".join(group) for group in profile.adjacent)),
    ]
    return [f"{label}: {value or '-'}" for label, value in rows]


def profile_json(profile: Profile, unknown_tags: tuple[str, ...]) -> dict[str, object]:
    return {
        "archetype": profile.archetype,
        "bracketCeiling": profile.bracket_ceiling,
        "maxCardCents": profile.max_card_cents,
        "targets": {role.value: n for role, n in profile.targets.items()},
        "themes": {**_spec_json(profile.themes), "ignore": list(profile.ignore_themes)},
        "exclude": {**_spec_json(profile.exclude), "cards": list(profile.exclude_cards)},
        "keep": {"cards": list(profile.keep_cards), "categories": list(profile.keep_categories)},
        "ideas": {"commanders": [list(group) for group in profile.adjacent]},
        "unknownTags": list(unknown_tags),
    }


def derived_lines(merged: MergedThemes) -> list[str]:
    """What the deck's Archidekt deck tags and categories say, theme by theme, in Archidekt's order."""
    derived = merged.derived
    used = {(t.source, t.name) for t in merged.used}
    lines = [_derived_line(t, (t.source, t.name) in used) for t in derived.themes]
    lines += [f"  {name} (deck tag): matches no type, keyword or Tagger tag" for name in derived.unmatched]
    lines += [
        f"  {name} (deck tag): {_BROAD_TAGS[kind]}" for name in derived.broad if (kind := broad_kind(name))
    ]
    lines += [f"  {name} (category): its cards share nothing distinctive" for name in derived.vague]
    if not merged.profile_covered.empty:
        # Ideas are labelled by the deck tag, not "profile" as well.
        lines.append(f"  profile themes these already name: {_spec_text(merged.profile_covered)}")
    return ["from Archidekt:", *lines] if lines else ["from Archidekt: no deck tags or theme categories"]


def derived_json(merged: MergedThemes) -> dict[str, object]:
    used = {(t.source, t.name) for t in merged.used}
    return {
        "derived": [
            {
                "source": t.source.value,
                "name": t.name,
                **_spec_json(t.spec),
                "covered": t.covered,
                "size": t.size,
                "used": (t.source, t.name) in used,
            }
            for t in merged.derived.themes
        ],
        "unmatchedDeckTags": list(merged.derived.unmatched),
        "profileCovered": _spec_json(merged.profile_covered),
        "broadTags": list(merged.derived.broad),
        "vagueCategories": list(merged.derived.vague),
        "unknownIgnores": list(merged.unknown_ignores),
    }


def _derived_line(theme: DerivedTheme, used: bool) -> str:  # noqa: FBT001 — one flag, read at the call
    source = theme.source.value
    if theme.source is ThemeSource.CATEGORY and theme.size:
        source += f", {theme.covered} of {theme.size} cards"
    what = _spec_text(theme.spec) if used else "left out by the profile"
    return f"  {theme.name} ({source}): {what}"


def _spec_text(spec: ThemeSpec) -> str:
    parts = [
        _names(spec.types, "types"),
        _names(spec.keywords, "keywords"),
        _names(spec.tags, "tags"),
    ]
    return "; ".join(part for part in parts if part)


def _names(names: tuple[str, ...], label: str = "") -> str:
    if not names:
        return ""
    prefix = f"{label} " if label else ""
    return prefix + ", ".join(names)


def _spec_json(spec: ThemeSpec) -> dict[str, object]:
    return {"types": list(spec.types), "keywords": list(spec.keywords), "tags": list(spec.tags)}
