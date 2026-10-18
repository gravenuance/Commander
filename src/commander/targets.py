"""The deck cards a board card could replace: picked by hand, else the weakest like it (ADR 0029)."""

from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from itertools import islice

from commander.analysis import ANALYZED_ROLES
from commander.answers import card_key
from commander.board import Actor, BoardCard
from commander.carddb import CardDb, CardInfo, ResolvedCard, Role, Theme
from commander.deckcache import Clock
from commander.deckprofile import PROFILE_THEME, Profile, ThemeSpec
from commander.deckthemes import merge_themes
from commander.landswap import ManaGuard, mana_guard
from commander.leanings import Leaning, find_leanings
from commander.metadata import MetaData
from commander.models import COMMANDER_CATEGORY, Deck
from commander.peercounts import PeerCounts, peer_counts
from commander.suggest import fills_role, protected_cards
from commander.weightedshare import ShareTable, share_reason, weighted_shares

MAX_TARGETS = 3
# A card of another theme is cut only for one at least this many points more played (a share of 0.2).
MIN_THEME_GAP = 0.20
# Cutting from a role the deck over-runs scores this much per card over the peer decks' median: how much less
# played each extra card of a role is among peer decks (median slope, 95 commander x role cases, 2026-10-18).
OVERRUN_BONUS = 0.03
# Weighted shares are fractions; this keeps float noise from deciding the gap.
_SHARE_NOISE = 1e-9
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
    # Judges a swap that takes a land out or brings one in; None skips the check.
    mana: ManaGuard | None = None
    # What the peer decks hold of each role and theme; None without enough decks, and then a cut stays
    # within the idea's own role and themes.
    peers: PeerCounts | None = None


def deck_cut_rules(
    deck: Deck,
    main: Sequence[ResolvedCard],
    profile: Profile,
    db: CardDb,
    data: MetaData | None,
    *,
    now: Clock,
) -> CutRules:
    """The deck's kept cards, the meta's and recent decks' shares, its leanings, the themes of its cards, and
    its mana base for judging swaps of lands.

    The profile's own themes go by their words ("Dinosaur", "Discover"). data is what other players do
    with the commander (`gather_for`), None when nothing was read.
    """
    peers = data.peers.data.decks if data is not None and data.peers.data else ()
    shares = weighted_shares(data, now=now)
    ignored = {name.strip().casefold() for name in profile.ignore_themes}
    leanings = find_leanings(main, peers, db, ignore=ignored)
    tag_ids = db.tag_ids([*profile.themes.tags, *(leaning.tag for leaning in leanings)])
    merged = merge_themes(deck, profile, db)
    words = [Matcher(word, theme) for word, theme in _by_word(merged.profile_own, tag_ids)]
    named = [Matcher(name, theme) for name, theme in merged.resolved.labelled if name != PROFILE_THEME]
    leaning_matchers = [
        Matcher(leaning.tag, Theme(tag_ids=(tag_ids[leaning.tag],)), nonland_only=True)
        for leaning in leanings
        if leaning.tag in tag_ids
    ]
    matchers = (*words, *named, *leaning_matchers)
    commanders = {card_key(r.card.name) for r in main if COMMANDER_CATEGORY in r.card.categories}
    counts = peer_counts(
        peers,
        commanders,
        db,
        {m.name: m.theme for m in (*named, *leaning_matchers)},
        nonland_only={m.name for m in leaning_matchers},
    )
    mana = mana_guard(deck, db, data)
    rules = CutRules(protected_cards(main, profile), shares or None, {}, leanings, matchers, mana, counts)
    return with_ideas(rules, [r.info for r in main if r.info is not None], db)


def with_ideas(rules: CutRules, cards: Iterable[CardInfo], db: CardDb) -> CutRules:
    """The rules with the themes, leanings and mana data of these cards added: the ideas to match against the
    deck."""
    infos = {info.oracle_id: info for info in cards if info.oracle_id not in rules.themes}
    if not infos:
        return rules
    nonland = [key for key, info in infos.items() if not info.is_land]
    themes = {key: set(names) for key, names in rules.themes.items()}
    # A card matching no theme is kept with none, so a later call does not match it again.
    themes.update((key, set()) for key in infos)
    for matcher in rules.matchers:
        for oracle_id in db.matching(matcher.theme, nonland if matcher.nonland_only else list(infos)):
            themes.setdefault(oracle_id, set()).add(matcher.name)
    mana = rules.mana.with_cards(db.card_mana(info.name for info in infos.values())) if rules.mana else None
    return replace(rules, themes={key: frozenset(names) for key, names in themes.items()}, mana=mana)


def targets_for(
    card: BoardCard, info: CardInfo | None, main: Sequence[ResolvedCard], rules: CutRules
) -> tuple[Target, ...]:
    """The replace options you or Claude gave the card, still in the deck; else the weakest like it.

    Every cut but the ones you picked must be less played than the card (`_less_played`). See `role_targets`
    for the second.
    """
    in_deck = {r.card.name.casefold(): r for r in main}
    # Tools attach one generic cut list to all their ideas; only a person's or Claude's options are picks.
    options = card.replace if card.source in _PICKERS else ()
    picked = tuple(
        Target(o.name, o.reason, TargetSource.PICKED)
        for o in options
        if (r := in_deck.get(o.name.casefold())) is not None
        and _sound(info, r, rules)
        and (card.source is Actor.YOU or _less_played(card.name, r, rules.shares))
    )
    if picked or info is None:
        return picked
    return role_targets(info, main, rules)


def role_targets(info: CardInfo, main: Sequence[ResolvedCard], rules: CutRules) -> tuple[Target, ...]:
    """The weakest deck cards like the card, at most MAX_TARGETS; the slots left go to cards unlike it.

    Like for like: a deck card serving a theme or leaning is offered only for an idea serving one of the
    same, and those come first. A card with roles is matched to deck cards sharing one, a card without any
    to deck cards filling none; lands only to lands. Weakest means the smallest weighted share of the meta's
    lists and recent decks (when known), then the least played on EDHREC, unranked cards last. Commanders and
    protected oracle ids are never offered, nor is a card at least as played as the idea (a card no list
    plays counts as 0), unless no list was read.

    With the recent decks' counts read, cards of another role or theme, less played than the idea, fill the
    slots like for like leaves (`_wider_cuts`).
    """
    roles = [role for role in ANALYZED_ROLES if role in info.roles]
    own = rules.themes.get(info.oracle_id, frozenset())
    eligible = [
        r for r in main if _eligible(r, info, rules.protected) and _less_played(info.name, r, rules.shares)
    ]
    pool = [r for r in eligible if _same_role(r, roles) and _swappable(r, own, rules)]
    ranked = sorted(pool, key=lambda r: (not _shared(r, own, rules), *_weakness(r, rules.shares)))
    like = [
        Target(r.card.name, _reason(r, roles, own, rules), TargetSource.ROLE)
        for r in islice((r for r in ranked if _sound(info, r, rules)), MAX_TARGETS)
    ]
    if len(like) == MAX_TARGETS:
        return tuple(like)
    wider = (
        Target(w.cut.card.name, _reason(w.cut, roles, own, rules, w), TargetSource.ROLE)
        for w in _wider_cuts(info, eligible, main, rules)
        if _sound(info, w.cut, rules)
    )
    return (*like, *islice(wider, MAX_TARGETS - len(like)))


@dataclass(slots=True, frozen=True)
class _Wider:
    """A cut of another role or theme than the idea's, and what makes it allowed."""

    cut: ResolvedCard
    # The idea's share less the card's, plus the bonus for a role the deck over-runs.
    score: float
    # The role the deck over-runs most that the card fills, with the deck's count and the peers' median.
    over: tuple[Role, int, float] | None
    # How far the idea is more played than the card, when the card is of another theme.
    theme_gap: float | None


def _wider_cuts(
    info: CardInfo, eligible: Sequence[ResolvedCard], main: Sequence[ResolvedCard], rules: CutRules
) -> list[_Wider]:
    """Deck cards of another role or theme that may go for the idea, the best first.

    None without the recent decks' counts or the shares. A card of another role may go only while each role
    it fills and the idea does not keeps the peers' 25th percentile of cards (roles counted by Tagger tag
    alone, as the peers' are). A card of another theme may go
    only while the idea is MIN_THEME_GAP more played and each of its themes keeps the peers' median count (a
    theme with no peer count, the profile's own, may lose one card).
    Cards of the idea's themes come first, then by score: the idea's share less the card's, plus
    OVERRUN_BONUS for each card a role it fills runs over the peers' median (a cut of another role only).
    """
    peers, shares = rules.peers, rules.shares
    if peers is None or not shares:
        return []
    roles = [role for role in ANALYZED_ROLES if role in info.roles]
    own = rules.themes.get(info.oracle_id, frozenset())
    gain = _share(info.name, shares)
    held_roles, held_themes = _role_counts(main), _theme_counts(main, rules)
    found: list[_Wider] = []
    for r in eligible:
        other_role = not _same_role(r, roles)
        other_theme = not _swappable(r, own, rules)
        if not (other_role or other_theme):
            continue
        lost = [role for role in _tagged_roles(r) if role not in info.roles]
        if any(role in peers.roles and held_roles[role] - 1 < peers.roles[role].floor for role in lost):
            continue
        gap = gain - _share(r.card.name, shares)
        if other_theme and (
            gap < MIN_THEME_GAP - _SHARE_NOISE or not _themes_hold(r, held_themes, rules, peers)
        ):
            continue
        over = _over_run(lost, held_roles, peers) if other_role else None
        bonus = OVERRUN_BONUS * (over[1] - over[2]) if over is not None else 0.0
        found.append(_Wider(r, gap + bonus, over, gap if other_theme else None))
    found.sort(
        key=lambda w: (
            w.theme_gap is not None,
            not _shared(w.cut, own, rules),
            -w.score,
            *_weakness(w.cut, shares),
        )
    )
    return found


def _over_run(
    lost: Sequence[Role], held: Mapping[Role, int], peers: PeerCounts
) -> tuple[Role, int, float] | None:
    over = [(role, held[role], peers.roles[role].median) for role in lost if role in peers.roles]
    return max((o for o in over if o[1] > o[2]), key=lambda o: o[1] - o[2], default=None)


def _themes_hold(r: ResolvedCard, held: Mapping[str, int], rules: CutRules, peers: PeerCounts) -> bool:
    """Whether each theme of the deck card keeps the peers' count without it."""
    return all(held[theme] - 1 >= peers.themes.get(theme, held[theme] - 1) for theme in _themes_of(r, rules))


def _role_counts(main: Sequence[ResolvedCard]) -> Counter[Role]:
    """How many deck cards the Tagger tags put in each role, as `peercounts` counts the peers'; commanders
    left out."""
    counts: Counter[Role] = Counter()
    for r in main:
        if COMMANDER_CATEGORY not in r.card.categories:
            for role in _tagged_roles(r):
                counts[role] += r.card.quantity
    return counts


def _theme_counts(main: Sequence[ResolvedCard], rules: CutRules) -> Counter[str]:
    counts: Counter[str] = Counter()
    for r in main:
        if COMMANDER_CATEGORY not in r.card.categories:
            for theme in _themes_of(r, rules):
                counts[theme] += r.card.quantity
    return counts


def _tagged_roles(r: ResolvedCard) -> list[Role]:
    return [role for role in ANALYZED_ROLES if r.info is not None and role in r.info.roles]


def _roles_of(r: ResolvedCard) -> list[Role]:
    return [role for role in ANALYZED_ROLES if fills_role(r, role)]


def _sound(info: CardInfo | None, cut: ResolvedCard, rules: CutRules) -> bool:
    """Whether the deck card may be cut for the idea: a swap of a land is judged by the mana base."""
    if rules.mana is None or info is None or cut.info is None or not (info.is_land or cut.info.is_land):
        return True
    return rules.mana.allows(info.name, cut.card.name)


def _over_run_note(wider: _Wider | None) -> str:
    if wider is None or wider.over is None:
        return ""
    _, count, middle = wider.over
    return f", over-run: {count} against a median of {middle:g} in recent decks"


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


def _less_played(idea: str, cut: ResolvedCard, shares: ShareTable | None) -> bool:
    """Whether the deck card is less played than the idea; without any shares, every card is."""
    return not shares or _share(idea, shares) - _share(cut.card.name, shares) > _SHARE_NOISE


def _share(name: str, shares: ShareTable) -> float:
    found = shares.of(name)
    return found.share if found is not None else 0.0


def _weakness(r: ResolvedCard, shares: ShareTable | None) -> tuple[float, bool, int, str]:
    share = _share(r.card.name, shares) if shares is not None else 0.0
    rank = r.info.edhrec_rank if r.info else None
    return (share, rank is None, -(rank or 0), r.card.name.casefold())


def _reason(
    r: ResolvedCard, roles: Sequence[Role], own: frozenset[str], rules: CutRules, wider: _Wider | None = None
) -> str:
    parts: list[str] = []
    shared_themes = _shared(r, own, rules)
    if shared_themes:
        parts.append(f"same theme ({', '.join(sorted(shared_themes, key=str.casefold))})")
    elif wider is not None and wider.theme_gap is not None:
        theirs = ", ".join(sorted(_themes_of(r, rules), key=str.casefold))
        parts.append(f"other theme ({theirs}; idea {wider.theme_gap * 100:.0f} points more played)")
    shared = [role.value for role in roles if fills_role(r, role)]
    filled = [role.value for role in _roles_of(r)]
    if shared:
        parts.append(f"same role ({', '.join(shared)})")
    elif filled:
        parts.append(f"other role ({', '.join(filled)}{_over_run_note(wider)})")
    else:
        parts.append("fills no role" if roles else "fills no role either")
    share = rules.shares.of(r.card.name) if rules.shares is not None else None
    if share is not None:
        parts.append(share_reason(share))
    rank = r.info.edhrec_rank if r.info else None
    parts.append(f"EDHREC rank {rank:,}" if rank is not None else "unranked on EDHREC")
    return "; ".join(parts)
