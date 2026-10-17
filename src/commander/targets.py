"""The deck cards a board card could replace: picked by hand, else the weakest like it (ADR 0016)."""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum

from commander.analysis import ANALYZED_ROLES
from commander.board import Actor, BoardCard
from commander.carddb import CardDb, CardInfo, ResolvedCard, Role, Theme
from commander.deckcache import Clock
from commander.deckprofile import PROFILE_THEME, Profile, ThemeSpec
from commander.deckthemes import merge_themes
from commander.leanings import Leaning, find_leanings
from commander.metadata import MetaData
from commander.models import COMMANDER_CATEGORY, Deck
from commander.suggest import fills_role, protected_cards
from commander.weightedshare import ShareTable, share_reason, weighted_shares

MAX_TARGETS = 3
_PICKERS = frozenset({Actor.YOU, Actor.CLAUDE})


class TargetSource(Enum):
    # Named on the board card by whoever proposed it (Claude's research, meta outliers).
    PICKED = "picked"
    # Worked out here from the card's themes, roles, the meta and popularity.
    ROLE = "role"


@dataclass(slots=True, frozen=True)
class Target:
    name: str
    reason: str
    source: TargetSource


@dataclass(slots=True, frozen=True)
class Matcher:
    """A theme or leaning cards are matched against, by the name reasons show."""

    name: str
    theme: Theme
    # Leanings are counted on nonland cards, so they hold only nonland ones.
    nonland_only: bool = False


@dataclass(slots=True, frozen=True)
class CutRules:
    """What decides which deck cards an idea may replace: worked out once per deck (`deck_cut_rules`),
    then given the ideas' themes (`with_ideas`)."""

    # Oracle ids never offered: the cards the profile keeps.
    protected: Collection[str] = frozenset()
    # Each card's weighted share of the meta's lists and recent decks with the commander; None without any.
    shares: ShareTable | None = None
    # The themes and leanings each card serves, by oracle id, for deck cards and ideas alike.
    themes: Mapping[str, frozenset[str]] = field(default_factory=dict[str, frozenset[str]])
    leanings: tuple[Leaning, ...] = ()
    matchers: tuple[Matcher, ...] = ()


def deck_cut_rules(
    deck: Deck,
    main: Sequence[ResolvedCard],
    profile: Profile,
    db: CardDb,
    data: MetaData | None,
    *,
    now: Clock,
) -> CutRules:
    """The deck's kept cards, the meta's and recent decks' shares, its leanings, and the themes of its cards.

    The profile's own themes go by their words ("Dinosaur", "Discover"). data is what other players do
    with the commander (`gather_for`), None when nothing was read.
    """
    peers = data.peers.data.decks if data is not None and data.peers.data else ()
    shares = weighted_shares(data, now=now)
    ignored = {name.strip().casefold() for name in profile.ignore_themes}
    leanings = find_leanings(main, peers, db, ignore=ignored)
    tag_ids = db.tag_ids([*profile.themes.tags, *(leaning.tag for leaning in leanings)])
    merged = merge_themes(deck, profile, db)
    matchers = (
        *(
            Matcher(theme_name, theme)
            for name, labelled in merged.resolved.labelled
            for theme_name, theme in (
                _by_word(merged.profile_own, tag_ids) if name == PROFILE_THEME else [(name, labelled)]
            )
        ),
        *(
            Matcher(leaning.tag, Theme(tag_ids=(tag_ids[leaning.tag],)), nonland_only=True)
            for leaning in leanings
            if leaning.tag in tag_ids
        ),
    )
    rules = CutRules(protected_cards(main, profile), shares or None, {}, leanings, matchers)
    return with_ideas(rules, [r.info for r in main if r.info is not None], db)


def with_ideas(rules: CutRules, cards: Iterable[CardInfo], db: CardDb) -> CutRules:
    """The rules with the themes and leanings of these cards added: the ideas to match against the deck."""
    infos = {info.oracle_id: info for info in cards if info.oracle_id not in rules.themes}
    if not infos:
        return rules
    nonland = [key for key, info in infos.items() if not info.is_land]
    themes = {key: set(names) for key, names in rules.themes.items()}
    for matcher in rules.matchers:
        for oracle_id in db.matching(matcher.theme, nonland if matcher.nonland_only else list(infos)):
            themes.setdefault(oracle_id, set()).add(matcher.name)
    return replace(rules, themes={key: frozenset(names) for key, names in themes.items()})


def targets_for(
    card: BoardCard, info: CardInfo | None, main: Sequence[ResolvedCard], rules: CutRules
) -> tuple[Target, ...]:
    """The replace options you or Claude gave the card, still in the deck; else the weakest like it.

    See `role_targets` for the second.
    """
    in_deck = {r.card.name.casefold() for r in main}
    # Tools attach one generic cut list to all their ideas; only a person's or Claude's options are picks.
    options = card.replace if card.source in _PICKERS else ()
    picked = tuple(
        Target(o.name, o.reason, TargetSource.PICKED) for o in options if o.name.casefold() in in_deck
    )
    if picked or info is None:
        return picked
    return role_targets(info, main, rules)


def role_targets(info: CardInfo, main: Sequence[ResolvedCard], rules: CutRules) -> tuple[Target, ...]:
    """The weakest deck cards like the card, at most MAX_TARGETS.

    Like for like: a deck card serving a theme or leaning is offered only for an idea serving one of the
    same, and those come first. A card with roles is matched to deck cards sharing one, a card without any
    to deck cards filling none; lands only to lands. Weakest means the smallest weighted share of the meta's
    lists and recent decks (when known), then the least played on EDHREC, unranked cards last. Commanders and
    protected oracle ids are never offered.
    """
    roles = [role for role in ANALYZED_ROLES if role in info.roles]
    own = rules.themes.get(info.oracle_id, frozenset())
    pool = [
        r
        for r in main
        if _eligible(r, info, rules.protected) and _same_role(r, roles) and _swappable(r, own, rules)
    ]
    ranked = sorted(pool, key=lambda r: (not _shared(r, own, rules), *_weakness(r, rules.shares)))
    return tuple(
        Target(r.card.name, _reason(r, roles, own, rules), TargetSource.ROLE) for r in ranked[:MAX_TARGETS]
    )


def _by_word(spec: ThemeSpec, tag_ids: Mapping[str, str]) -> list[tuple[str, Theme]]:
    """The profile's own themes one word at a time, each named by itself."""
    return [
        *((word, Theme(types=(word,))) for word in spec.types),
        *((keyword, Theme(keywords=(keyword,))) for keyword in spec.keywords),
        *((slug, Theme(tag_ids=(tag_ids[slug],))) for slug in spec.tags if slug in tag_ids),
    ]


def _eligible(r: ResolvedCard, info: CardInfo, protected: Collection[str]) -> bool:
    other = r.info
    return (
        other is not None
        and COMMANDER_CATEGORY not in r.card.categories
        and other.oracle_id not in protected
        and other.oracle_id != info.oracle_id
        and other.is_land == info.is_land
    )


def _same_role(r: ResolvedCard, roles: Sequence[Role]) -> bool:
    if roles:
        return any(fills_role(r, role) for role in roles)
    return not any(fills_role(r, role) for role in ANALYZED_ROLES)


def _themes_of(r: ResolvedCard, rules: CutRules) -> frozenset[str]:
    return rules.themes.get(r.info.oracle_id, frozenset()) if r.info is not None else frozenset()


def _swappable(r: ResolvedCard, own: frozenset[str], rules: CutRules) -> bool:
    theirs = _themes_of(r, rules)
    return not theirs or bool(theirs & own)


def _shared(r: ResolvedCard, own: frozenset[str], rules: CutRules) -> frozenset[str]:
    return _themes_of(r, rules) & own


def _weakness(r: ResolvedCard, shares: ShareTable | None) -> tuple[float, bool, int, str]:
    found = shares.of(r.card.name) if shares is not None else None
    share = found.share if found is not None else 0.0
    rank = r.info.edhrec_rank if r.info else None
    return (share, rank is None, -(rank or 0), r.card.name.casefold())


def _reason(r: ResolvedCard, roles: Sequence[Role], own: frozenset[str], rules: CutRules) -> str:
    parts: list[str] = []
    shared_themes = _shared(r, own, rules)
    if shared_themes:
        parts.append(f"same theme ({', '.join(sorted(shared_themes, key=str.casefold))})")
    shared = [role.value for role in roles if fills_role(r, role)]
    parts.append(f"same role ({', '.join(shared)})" if shared else "fills no role either")
    share = rules.shares.of(r.card.name) if rules.shares is not None else None
    if share is not None:
        parts.append(share_reason(share))
    rank = r.info.edhrec_rank if r.info else None
    parts.append(f"EDHREC rank {rank:,}" if rank is not None else "unranked on EDHREC")
    return "; ".join(parts)
