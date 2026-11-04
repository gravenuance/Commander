"""The deck cards a board card could replace: picked by hand, else the best scoring cuts (ADRs 0029, 0036)."""

import logging
import math
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum

from commander.analysis import ANALYZED_ROLES
from commander.answers import card_key
from commander.board import Actor, BoardCard
from commander.carddb import CardDb, CardInfo, ResolvedCard, Role, Theme, back_is_land
from commander.deckcache import Clock
from commander.deckprofile import PROFILE_THEME, Profile, ThemeSpec
from commander.deckthemes import merge_themes
from commander.keycards import key_cards
from commander.landswap import ManaGuard, mana_guard
from commander.leanings import Leaning, find_leanings
from commander.metadata import MetaData
from commander.models import COMMANDER_CATEGORY, Deck
from commander.peercounts import PeerCounts, peer_counts
from commander.spellbook import BracketEstimate, ComboDetail
from commander.suggest import fills_role, protected_cards, role_count
from commander.weightedshare import ShareTable, share_reason, weighted_shares

log = logging.getLogger(__name__)

MAX_TARGETS = 4
# A computed cut must be this much less played than the idea, or half the idea's share when that is smaller:
# a staple never pushes out a near-staple, and a barely played idea still finds cuts.
MIN_GAP = 0.30
# A card of another theme is cut only for one at least this many points more played (a share of 0.2).
MIN_THEME_GAP = 0.20
# Every candidate cut scores the idea's share less its own, plus a bonus for what makes it allowed (ADR 0036).
# A cut in the idea's role or themes adds this.
LIKE_FOR_LIKE_BONUS = 0.10
# A surplus cut adds this per card the deck holds over the peer decks' median of the role or theme, or per
# land it could lose: how much less played each extra card of a role is among peer decks (median slope,
# 95 commander x role cases, 2026-10-18). Provisional, to retune with more data.
SURPLUS_BONUS = 0.03
# The bonus counts at most this many cards over, so it stays under LIKE_FOR_LIKE_BONUS: a deck far over a
# median does not push the same few cards to the top of every idea's list.
MAX_SURPLUS_CARDS = 3
# The deck's own largest themes (by cards held) are what it is built on, never a surplus.
CORE_THEMES = 3
# A role or theme surplus needs the deck to hold this many cards over the peers' median, and still the median
# after the cut: one over is within the noise of a median taken over a few decks.
SURPLUS_MARGIN = 2
# The roles whose over-run lets a card of another role go; the other roles keep the peers' floor.
SURPLUS_ROLES = frozenset({Role.RAMP, Role.DRAW, Role.REMOVAL, Role.TUTOR})
# Weighted shares are fractions; this keeps float noise from deciding the gap.
_SHARE_NOISE = 1e-9
# Scores equal up to this many digits tie, and go on to the tie-break.
_SCORE_DIGITS = 9
# The group of the spare-land candidates, which fill one place among an idea's targets (ADR 0063).
_SPARE_LAND = "spare land"
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
    # The names of the themes that hold a word of the profile's own themes: the owner's chosen identity.
    profile_themes: frozenset[str] = frozenset()
    # The role counts the profile's [targets] set: no computed cut takes the deck below one of them.
    profile_targets: Mapping[Role, int] = field(default_factory=dict[Role, int])


def deck_cut_rules(
    deck: Deck,
    main: Sequence[ResolvedCard],
    profile: Profile,
    db: CardDb,
    data: MetaData | None,
    *,
    now: Clock,
    estimate: BracketEstimate | None = None,
) -> CutRules:
    """The deck's protected cards, the meta's and recent decks' shares, its leanings, the themes of its cards,
    and its mana base for judging swaps of lands.

    The profile's own themes go by their words ("Dinosaur", "Discover"). data is what other players do
    with the commander (`gather_for`), None when nothing was read. estimate is the deck's cached Spellbook
    bracket estimate, whose combos stand in for `data.own_combos` when that was not read.
    """
    peers = data.peers.data.decks if data is not None and data.peers.data else ()
    shares = weighted_shares(data, now=now, grace=profile.grace_by_key())
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
    protected = _protected(deck, main, profile, db, data=data, estimate=estimate)
    profile_themes = frozenset(m.name for m in named if _holds_word(m.theme, profile.themes, tag_ids))
    rules = CutRules(
        protected, shares or None, {}, leanings, matchers, mana, counts, profile_themes, dict(profile.targets)
    )
    return with_ideas(rules, [r.info for r in main if r.info is not None], db)


def _protected(
    deck: Deck,
    main: Sequence[ResolvedCard],
    profile: Profile,
    db: CardDb,
    *,
    data: MetaData | None,
    estimate: BracketEstimate | None,
) -> frozenset[str]:
    """Oracle ids of the deck cards no computed cut takes (ADR 0017, 0036): the cards the profile keeps or
    gives grace, the pieces of the deck's combos and its key cards."""
    graced = profile.grace_by_key()
    named = {r.card.name for r in main if card_key(r.card.name) in graced}
    peers = data.peers.data.decks if data is not None and data.peers.data else ()
    own_combos = data.own_combos.data if data is not None else None
    named |= {key.name for key in key_cards(deck, profile, own_combos or (), peers, db)}
    protected = frozenset(
        r.info.oracle_id
        for r in main
        if r.info is not None and r.card.name in named and COMMANDER_CATEGORY not in r.card.categories
    )
    return protected | protected_cards(main, profile) | _combo_pieces(deck, main, own_combos, estimate)


def _holds_word(theme: Theme, words: ThemeSpec, tag_ids: Mapping[str, str]) -> bool:
    """Whether the theme matches by a type, keyword or tag the profile's own themes name."""

    def shared(mine: Iterable[str], theirs: Iterable[str]) -> bool:
        return bool({word.casefold() for word in mine} & {word.casefold() for word in theirs})

    ids = (tag_ids[slug] for slug in words.tags if slug in tag_ids)
    return (
        shared(words.types, theme.types)
        or shared(words.keywords, theme.keywords)
        or shared(ids, theme.tag_ids)
    )


def _combo_pieces(
    deck: Deck,
    main: Sequence[ResolvedCard],
    own_combos: Sequence[ComboDetail] | None,
    estimate: BracketEstimate | None,
) -> frozenset[str]:
    """Oracle ids of the deck cards in a combo Spellbook finds inside the deck: its own answer for the deck,
    else the combos of the deck's cached bracket estimate; none without either."""
    names: set[str] = set()
    if own_combos is not None:
        names = {card_key(piece.name) for combo in own_combos for piece in combo.pieces}
    elif estimate is not None:
        names = {card_key(name) for combo in estimate.combos for name in combo.cards}
    else:
        log.info("No Spellbook combos for deck %s: its combo pieces are not protected from cuts", deck.name)
    return frozenset(r.info.oracle_id for r in main if r.info is not None and card_key(r.card.name) in names)


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
    card: BoardCard,
    info: CardInfo | None,
    main: Sequence[ResolvedCard],
    rules: CutRules,
    counts: "DeckCounts | None" = None,
) -> tuple[Target, ...]:
    """The replace options you or Claude gave the card, still in the deck; else the best scoring cuts.

    Every cut but the ones you picked must be less played than the card (`_less_played`). See `role_targets`
    for the second, and for `counts`.
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
    return role_targets(info, main, rules, counts)


def role_targets(
    info: CardInfo, main: Sequence[ResolvedCard], rules: CutRules, counts: "DeckCounts | None" = None
) -> tuple[Target, ...]:
    """The best scoring deck cards for the card to replace, at most MAX_TARGETS.

    `counts` are the deck's (`deck_counts`), which a caller scoring many ideas against one deck works out
    once; without them each call does.

    Every candidate scores the idea's weighted share (ADR 0026) less its own, plus a bonus for what makes it
    allowed. Equal scores go to the bigger surplus, then the less popular card on EDHREC (the higher rank
    number first, unranked last), then the name. Commanders and protected oracle ids are never offered, nor
    is a card within MIN_GAP of the idea's share (`_clearly_less_played`; a card no list plays counts as 0,
    and a basic land always does), unless no list was read.

    - Like for like (+LIKE_FOR_LIKE_BONUS): a card of the idea's role (an idea with none matches cards with
      none, a land only lands), and a card serving a theme or leaning only for an idea serving the same. A
      land idea takes the place of a basic land of a colour it makes, or of a land that only makes mana
      unless the idea does more (`_fits_land`).
    - With the recent decks' counts read (`_wider_candidates`): a card of another role the deck holds more
      of than the peers' median, or of another theme it over-runs, scores SURPLUS_BONUS per extra card, up
      to MAX_SURPLUS_CARDS. A card of a surplus role and another theme needs both surpluses; the deck's
      CORE_THEMES largest themes and the profile's own are never a theme surplus.
    - A nonland idea may also replace a land the mana base can spare (`_land_candidates`).
    """
    roles = [role for role in ANALYZED_ROLES if role in info.roles]
    own = rules.themes.get(info.oracle_id, frozenset())
    if counts is None and rules.profile_targets:
        counts = deck_counts(main, rules)
    eligible = [
        r
        for r in main
        if _eligible(r, info, rules.protected)
        and _fits_land(info, r, rules)
        and (counts is None or _keeps_targets(r, info, counts, rules))
        and _clearly_less_played(info.name, r, rules.shares)
    ]
    like = [
        _Candidate(
            r,
            LIKE_FOR_LIKE_BONUS + _gap(info.name, r, rules.shares),
            _reason(r, roles, own, rules),
            _order(r, 0.0),
        )
        for r in eligible
        if _same_role(r, roles) and _swappable(r, own, rules)
    ]
    candidates = [
        *like,
        *_wider_candidates(info, eligible, main, rules, counts),
        *_land_candidates(info, main, rules),
    ]
    taken: list[Target] = []
    groups: set[str] = set()
    for c in sorted(candidates, key=lambda c: (-round(c.score, _SCORE_DIGITS), c.order)):
        if len(taken) == MAX_TARGETS:
            break
        if c.group in groups or not _sound(info, c.cut, rules):
            continue
        # A group fills one place: the first of it the guard allows, the rest give way.
        if c.group is not None:
            groups.add(c.group)
        taken.append(Target(c.cut.card.name, c.reason, TargetSource.ROLE))
    return tuple(taken)


@dataclass(slots=True, frozen=True)
class _Candidate:
    """A deck card the idea may replace, with what it scores and how it reads."""

    cut: ResolvedCard
    score: float
    reason: str
    # Breaks ties: the bigger surplus first, then (for spare lands) a basic before any other land and the
    # colour with more sources to spare, then the lower EDHREC popularity (the higher rank number first,
    # unranked last), then the name.
    order: tuple[float, float, bool, int, str]
    # At most one candidate of a group is offered (one spare land).
    group: str | None = None


@dataclass(slots=True, frozen=True)
class DeckCounts:
    """How many of the deck's cards fill each role and serve each theme."""

    roles: Counter[Role]
    themes: Counter[str]
    # The cards filling each role the profile sets a target for, counted as the suggestions count them (tag
    # or category).
    targeted: Mapping[Role, int] = field(default_factory=dict[Role, int])


def deck_counts(main: Sequence[ResolvedCard], rules: CutRules) -> DeckCounts:
    targeted = {role: role_count(main, role) for role in rules.profile_targets}
    return DeckCounts(_role_counts(main), _theme_counts(main, rules), targeted)


def _keeps_targets(r: ResolvedCard, idea: CardInfo, counts: DeckCounts, rules: CutRules) -> bool:
    """Whether cutting the card keeps every role the profile sets a target for at it: a card filling such a
    role the idea does not is cut only while the deck holds more cards of it than the target."""
    return all(
        counts.targeted[role] > target
        for role, target in rules.profile_targets.items()
        if role not in idea.roles and fills_role(r, role)
    )


@dataclass(slots=True, frozen=True)
class _Held:
    """What the deck holds of each role and theme, against what the peer decks hold."""

    peers: PeerCounts
    roles: Mapping[Role, int]
    themes: Mapping[str, int]
    # The names of the deck's leanings, which stay protected.
    leanings: frozenset[str]
    # The names of the themes no cut counts as a surplus: the profile's own and the deck's largest.
    identity: frozenset[str]


@dataclass(slots=True, frozen=True)
class _Idea:
    """The roles and themes of the card a cut is sought for."""

    roles: frozenset[Role]
    themes: frozenset[str]


@dataclass(slots=True, frozen=True)
class _Surplus:
    """What lets a wider cut go: how many cards the deck holds over the peers' median, and how the reason
    reads; no note for a plain cut of another role."""

    over: float
    note: str | None

    @property
    def bonus(self) -> float:
        return _surplus_bonus(self.over)


def _surplus_bonus(over: float) -> float:
    return SURPLUS_BONUS * min(over, MAX_SURPLUS_CARDS)


def _wider_candidates(
    info: CardInfo,
    eligible: Sequence[ResolvedCard],
    main: Sequence[ResolvedCard],
    rules: CutRules,
    counts: DeckCounts | None,
) -> list[_Candidate]:
    """Deck cards of another role or theme than the idea's that may go for it.

    None without the recent decks' counts or the shares. See `_wider_cut` for what lets one go.
    """
    peers, shares = rules.peers, rules.shares
    if peers is None or not shares:
        return []
    roles = [role for role in ANALYZED_ROLES if role in info.roles]
    own = rules.themes.get(info.oracle_id, frozenset())
    if counts is None:
        counts = deck_counts(main, rules)
    leanings = frozenset(leaning.tag for leaning in rules.leanings)
    identity = rules.profile_themes | _core_themes(counts, leanings)
    held = _Held(peers, counts.roles, counts.themes, leanings, identity)
    found: list[_Candidate] = []
    for r in eligible:
        if _same_role(r, roles) and _swappable(r, own, rules):
            continue
        gap = _gap(info.name, r, shares)
        surplus = _wider_cut(r, _Idea(info.roles, own), gap, held, rules)
        if surplus is not None:
            reason = _reason(r, roles, own, rules, surplus.note)
            found.append(_Candidate(r, gap + surplus.bonus, reason, _order(r, surplus.over)))
    return found


def _wider_cut(r: ResolvedCard, idea: _Idea, gap: float, held: _Held, rules: CutRules) -> _Surplus | None:
    """Whether a deck card that is not like the idea may go for it, and why.

    A card of a leaning the idea does not serve never goes. A card that would empty one of SURPLUS_ROLES needs
    the role surplus, and the theme surplus too when its themes are not the idea's; the larger one sets the
    bonus, so nothing counts twice. Any other card of another theme goes by theme surplus, and one of another
    role only by the peers' floor of the roles it fills.
    """
    themes = _themes_of(r, rules)
    if (themes & held.leanings) - idea.themes:
        return None
    tagged = _tagged_roles(r)
    lost = [role for role in tagged if role not in idea.roles]
    if any(role in SURPLUS_ROLES for role in lost):
        by_role = _role_surplus(lost, held)
        if by_role is None or _swappable(r, idea.themes, rules):
            return by_role
        by_theme = _theme_surplus(themes, gap, held)
        return None if by_theme is None else max(by_role, by_theme, key=lambda s: s.over)
    if any(role in held.peers.roles and held.roles[role] - 1 < held.peers.roles[role].floor for role in lost):
        return None
    if not _swappable(r, idea.themes, rules):
        return _theme_surplus(themes, gap, held)
    return _Surplus(0.0, None)


def _role_surplus(lost: Sequence[Role], held: _Held) -> _Surplus | None:
    """Every role of SURPLUS_ROLES the cut empties must run SURPLUS_MARGIN over the peers' median and keep
    the median after the cut; any other keeps the peers' floor. The role over-run most sets the bonus."""
    over: list[tuple[Role, int, float]] = []
    for role in lost:
        band, count = held.peers.roles.get(role), held.roles[role]
        if role not in SURPLUS_ROLES:
            if band is not None and count - 1 < band.floor:
                return None
        elif band is None or count < band.median + SURPLUS_MARGIN or count - 1 < band.median:
            return None
        else:
            over.append((role, count, band.median))
    if not over:
        return None
    role, count, middle = max(over, key=lambda o: o[1] - o[2])
    return _Surplus(count - middle, f"surplus {role.value}: {_against(count, middle)}")


def _core_themes(counts: DeckCounts, leanings: frozenset[str]) -> frozenset[str]:
    """The CORE_THEMES themes the deck holds most cards of (ties by name), its leanings left out."""
    held = sorted((-count, theme) for theme, count in counts.themes.items() if theme not in leanings)
    return frozenset(theme for _, theme in held[:CORE_THEMES])


def _theme_surplus(themes: frozenset[str], gap: float, held: _Held) -> _Surplus | None:
    """The idea must be MIN_THEME_GAP more played, and every theme the cut empties must be none of the deck's
    identity, have a peer median, run SURPLUS_MARGIN over it and keep it after the cut. The theme over-run
    most sets the bonus."""
    if gap < MIN_THEME_GAP - _SHARE_NOISE or themes & held.identity:
        return None
    over: list[tuple[str, int, float]] = []
    for theme in themes:
        middle, count = held.peers.themes.get(theme), held.themes[theme]
        if middle is None or count < middle + SURPLUS_MARGIN or count - 1 < middle:
            return None
        over.append((theme, count, middle))
    theme, count, middle = max(over, key=lambda o: (o[1] - o[2], o[0]))
    return _Surplus(
        count - middle,
        f"surplus theme ({theme}): {_against(count, middle)}; idea {gap * 100:.0f} points more played",
    )


def _against(count: int, middle: float) -> str:
    return f"{count} against a median of {middle:g} in recent decks"


def _land_candidates(info: CardInfo, main: Sequence[ResolvedCard], rules: CutRules) -> list[_Candidate]:
    """Lands a nonland idea may replace while the mana base has lands to spare.

    None without the shares, the peers' counts or the mana data, or while the guard counts no spare land
    (`ManaGuard.spare_lands`). The candidates fill one place among the targets: the first the guard allows
    (ADR 0063). Each scores SURPLUS_BONUS per spare land, up to MAX_SURPLUS_CARDS, plus its share gap to the
    idea. A basic counts as played by no list (`_played`), so it comes first, the colour with most sources
    to spare before the others: the cheapest land to lose, unless the guard keeps it for the deck's fetches.
    A nonbasic land is a candidate only when it does nothing but make mana (`ManaGuard.mana_only`), carries
    no role, serves no theme or leaning (a utility or theme land is never spare) and is clearly less played
    than the idea (`_clearly_less_played`).
    """
    mana, shares = rules.mana, rules.shares
    if info.is_land or mana is None or not shares or rules.peers is None:
        return []
    spare = mana.spare_lands()
    if spare <= 0:
        return []
    note = f"spare land: the mana base holds with {spare} fewer {'land' if spare == 1 else 'lands'}"
    found: list[_Candidate] = []
    for r in main:
        cut = r.info
        if not _cuttable(r, info, rules.protected) or cut is None or not cut.is_land:
            continue
        reason = _reason(r, (), frozenset(), rules, note)
        score = _surplus_bonus(spare) + _gap(info.name, r, shares)
        if _is_basic(r):
            sources = mana.spare_sources(r.card.name)
            if sources is not None:
                order = (-float(spare), -sources, True, 0, r.card.name.casefold())
                found.append(_Candidate(r, score, reason, order, group=_SPARE_LAND))
        elif (
            mana.mana_only(r.card.name)
            and not cut.roles
            and not _roles_of(r)
            and not _themes_of(r, rules)
            and _clearly_less_played(info.name, r, shares)
        ):
            # Infinite: a nonbasic that ties with a basic gives way to it.
            _, _, unranked, rank, name = _order(r, float(spare))
            order = (-float(spare), math.inf, unranked, rank, name)
            found.append(_Candidate(r, score, reason, order, group=_SPARE_LAND))
    return found


def _order(r: ResolvedCard, over: float) -> tuple[float, float, bool, int, str]:
    """The tie-break of a candidate that scores like another: see `_Candidate.order`."""
    rank = r.info.edhrec_rank if r.info else None
    return (-over, 0.0, rank is None, -(rank or 0), r.card.name.casefold())


def _role_counts(main: Sequence[ResolvedCard]) -> Counter[Role]:
    """How many deck cards the Tagger tags put in each role, as `peercounts` counts the peers'; commanders
    left out."""
    counts: Counter[Role] = Counter()
    for r in _distinct_cards(main):
        counts.update(_tagged_roles(r))
    return counts


def _theme_counts(main: Sequence[ResolvedCard], rules: CutRules) -> Counter[str]:
    counts: Counter[str] = Counter()
    for r in _distinct_cards(main):
        counts.update(_themes_of(r, rules))
    return counts


def _distinct_cards(main: Sequence[ResolvedCard]) -> list[ResolvedCard]:
    """The deck's cards but the commanders, each once however many entries or copies it has: the peers'
    counts see a card once per deck."""
    return list({r.card.name: r for r in main if COMMANDER_CATEGORY not in r.card.categories}.values())


def _tagged_roles(r: ResolvedCard) -> list[Role]:
    return [role for role in ANALYZED_ROLES if r.info is not None and role in r.info.roles]


def _roles_of(r: ResolvedCard) -> list[Role]:
    return [role for role in ANALYZED_ROLES if fills_role(r, role)]


def _sound(info: CardInfo | None, cut: ResolvedCard, rules: CutRules) -> bool:
    """Whether the deck card may be cut for the idea: a swap of a land, or of a spell with a land back that
    counts as part of one, is judged by the mana base. A fetcher spell for another spell is judged by the
    deck's fetches alone (ADR 0064)."""
    if rules.mana is None or info is None or cut.info is None:
        return True
    if any(_counts_as_land(card) for card in (info, cut.info)):
        return rules.mana.allows(info.name, cut.card.name)
    return not rules.mana.is_fetcher(info.name) or rules.mana.allows_fetcher(info.name, cut.card.name)


def _counts_as_land(info: CardInfo) -> bool:
    return info.is_land or back_is_land(info.type_line)


def _by_word(spec: ThemeSpec, tag_ids: Mapping[str, str]) -> list[tuple[str, Theme]]:
    """The profile's own themes one word at a time, each named by itself."""
    return [
        *((word, Theme(types=(word,))) for word in spec.types),
        *((keyword, Theme(keywords=(keyword,))) for keyword in spec.keywords),
        *((slug, Theme(tag_ids=(tag_ids[slug],))) for slug in spec.tags if slug in tag_ids),
    ]


def _cuttable(r: ResolvedCard, info: CardInfo, protected: Collection[str]) -> bool:
    other = r.info
    return (
        other is not None
        and COMMANDER_CATEGORY not in r.card.categories
        and other.oracle_id not in protected
        and other.oracle_id != info.oracle_id
    )


def _eligible(r: ResolvedCard, info: CardInfo, protected: Collection[str]) -> bool:
    return _cuttable(r, info, protected) and r.info is not None and r.info.is_land == info.is_land


def _fits_land(idea: CardInfo, r: ResolvedCard, rules: CutRules) -> bool:
    """Whether a land idea may take the deck land's place (ADR 0036): a basic only when the idea makes one of
    its colours, any other land only when it does nothing but make mana, unless the idea does more too. A
    land that does only make mana may also take a fetch land's place when another fetcher the deck keeps
    finds it (ADR 0064). The mana base's data is needed to tell; without it every land fits."""
    mana = rules.mana
    if not idea.is_land or mana is None:
        return True
    if _is_basic(r):
        return mana.makes_colour_of(idea.name, r.card.name)
    return (
        not mana.mana_only(idea.name)
        or mana.mana_only(r.card.name)
        or mana.found_by_other_fetch(idea.name, r.card.name)
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
    return not shares or _share(idea, shares) - _played(cut, shares) > _SHARE_NOISE


def _clearly_less_played(idea: str, cut: ResolvedCard, shares: ShareTable | None) -> bool:
    """Whether the deck card is at least MIN_GAP less played than the idea, or half the idea's share when
    that is smaller; without any shares, every card is."""
    if not shares:
        return True
    needed = min(MIN_GAP, _share(idea, shares) / 2)
    return _less_played(idea, cut, shares) and _gap(idea, cut, shares) >= needed - _SHARE_NOISE


def _gap(idea: str, cut: ResolvedCard, shares: ShareTable | None) -> float:
    """How far the idea is more played than the deck card; 0 without any shares."""
    return _share(idea, shares) - _played(cut, shares) if shares else 0.0


def _played(cut: ResolvedCard, shares: ShareTable) -> float:
    """The deck card's share. A basic land counts as played by no list: nearly every list runs basics, so its
    share says nothing about whether a land that does more should take its place."""
    return 0.0 if _is_basic(cut) else _share(cut.card.name, shares)


def _is_basic(r: ResolvedCard) -> bool:
    return r.info is not None and "Basic" in r.info.front_type_line.split()


def _share(name: str, shares: ShareTable) -> float:
    found = shares.of(name)
    return found.share if found is not None else 0.0


def _reason(
    r: ResolvedCard, roles: Sequence[Role], own: frozenset[str], rules: CutRules, surplus: str | None = None
) -> str:
    """Why the card may go: what makes a surplus cut allowed, else what it shares with the idea."""
    parts: list[str] = []
    if surplus is not None:
        parts.append(surplus)
    else:
        shared_themes = _shared(r, own, rules)
        if shared_themes:
            parts.append(f"same theme ({', '.join(sorted(shared_themes, key=str.casefold))})")
        shared = [role.value for role in roles if fills_role(r, role)]
        filled = [role.value for role in _roles_of(r)]
        if shared:
            parts.append(f"same role ({', '.join(shared)})")
        elif filled:
            parts.append(f"other role ({', '.join(filled)})")
        else:
            parts.append("fills no role" if roles else "fills no role either")
    share = rules.shares.of(r.card.name) if rules.shares is not None else None
    if share is not None:
        parts.append(share_reason(share))
    rank = r.info.edhrec_rank if r.info else None
    parts.append(f"EDHREC rank {rank:,}" if rank is not None else "unranked on EDHREC")
    return "; ".join(parts)
