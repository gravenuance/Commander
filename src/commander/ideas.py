"""Ideas for Considering, compiled on every sync (ADR 0028).

Decks at brackets 4 and 5 get the staples of their commander's TopDeck lists ("meta") and of its most-viewed
recent Archidekt decks ("archidekt decks"), the latter at the deck's own bracket and the one above; lower
brackets get the Archidekt ones only. A card is one of these ideas when the lower bound of its weighted
share, over every list read, reaches IDEA_LOWER_BOUND (ADR 0026); it belongs to the source that plays it
more. An idea's score is that source's weighted share times its weight for the deck's play style (ADR 0031);
adjacent and tagged ideas have no weighted share and use the pool's plain share.
Each source puts at most MAX_PER_SOURCE ideas on the board, highest score first. An idea with no deck card
to replace is still put on the board, where the app lists it as "No cut yet". A deck whose profile names
commanders that play alike also gets the cards at least half of their pooled recent Archidekt decks play
("adjacent decks"). So does one whose Archidekt theme tags find recent decks of other commanders ("tagged
decks"), each tag's decks a pool of their own. Ideas pass the profile's exclusions, bracket ceiling and
price cap; below bracket 5 also the deck's budget (ADR 0025), those within its working budget first.
EDHREC is never asked (ADR 0003): its rank, from Scryfall's data, only orders the cards an idea could
replace. Every sync also re-judges the open ideas by cmdr or from the old Maybeboard pick-up: one that no
longer passes its source's test is turned down (`rejudge_ideas`).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from commander.analysis import game_changers
from commander.answers import card_key
from commander.archidekt import MAX_SEARCH_TAGS
from commander.board import (
    Actor,
    BoardCard,
    BoardService,
    Origin,
    OriginPool,
    Relabel,
    Status,
    feed_ideas,
    read_board,
)
from commander.budget import resolved_budget
from commander.carddb import CardDb
from commander.deckcache import Clock
from commander.deckprofile import Profile, read_profile, resolve_spec, resolve_themes
from commander.deckthemes import ThemeSource, merge_themes
from commander.meta import (
    MIN_PEER_LISTS,
    Basis,
    CardShare,
    Staples,
    all_staples,
    pool_staple_keys,
    pool_staples,
)
from commander.metadata import (
    AdjacentDecks,
    MetaData,
    MetaSources,
    PeerList,
    Progress,
    TaggedDecks,
    gather_adjacent,
)
from commander.models import Deck, short_commander_name
from commander.playstyle import Fit
from commander.store import StoredDeck
from commander.suggest import IdeaFilter, name_keys_of
from commander.weightedshare import NO_SHARES, ShareTable, SourceShare, weighted_shares

MAX_PER_SOURCE = 50
# Why an idea that no longer passes the idea test is turned down on a sync (ADR 0028).
NO_LONGER_MAKES_THE_CUT = "no longer makes the cut"
# The reason of a reconsidered card that none of the lists read plays.
NO_LIST_PLAYS_IT = "reconsidered; no list plays it"


@dataclass(slots=True, frozen=True)
class Idea:
    name: str
    origin: Origin
    reason: str
    # The share that orders the source's ideas: its weighted share for meta and decks, the pool's plain share
    # for adjacent and tagged, which have no weighted share.
    share: float
    # For a tagged idea: the deck tag whose decks play it.
    tag: str | None = None
    # What the deck's play style makes of the card (`playstyle.Weight`): the share's factor in the score.
    multiplier: float = 1.0
    # For an adjacent or tagged idea: how many of the pooled decks play it.
    pool: OriginPool | None = None

    @property
    def score(self) -> float:
        """What ideas are taken in the order of: the share weighted by the deck's play style."""
        return self.share * self.multiplier


def _best_first(ideas: list[Idea]) -> list[Idea]:
    return sorted(ideas, key=lambda idea: (-idea.score, idea.name))


def _source_share(table: ShareTable, name: str, origin: Origin, plain: float) -> float:
    """The card's weighted share in the origin's source; the plain share when the table holds none."""
    found = table.of(name)
    part: SourceShare | None = None
    if found is not None:
        part = found.meta if origin is Origin.META else found.archidekt
    return part.share if part else plain


def staple_ideas(deck: Deck, staples: Staples, since: date, table: ShareTable = NO_SHARES) -> list[Idea]:
    """The missing staples as ideas, each from the source whose weighted share of it is larger (the plain
    share when `table` holds none): "meta" for the TopDeck lists (also on a tie), else "archidekt decks".
    The rows are staples already: `compile_ideas` picks them by the weighted share's lower bound, and an idea
    carries the chosen source's weighted share from `table`."""
    # What the deck's play style doesn't need (a Turbo deck's interaction) is no idea for it (ADR 0031).
    ideas = [
        _staple_idea(deck, row, staples, since, table) for row in staples.rows if row.fit is not Fit.OFF_PLAN
    ]
    return _best_first(ideas)


def _staple_idea(deck: Deck, row: CardShare, staples: Staples, since: date, table: ShareTable) -> Idea:
    lists = f"{deck.short_name} lists" if staples.basis is Basis.ALL else f"top-cut {deck.short_name} lists"
    parts: list[str] = []
    if staples.lists:
        parts.append(f"in {row.lists} of {staples.lists} {lists} on TopDeck since {since}")
    if staples.peers:
        parts.append(f"in {row.peer_lists} of {staples.peers} most-viewed recent Archidekt decks")
    reason = "; ".join(parts)
    meta = _source_share(table, row.name, Origin.META, row.share)
    decks = _source_share(table, row.name, Origin.DECKS, row.peer_share)
    if staples.lists and (not staples.peers or meta >= decks):
        return Idea(row.name, Origin.META, reason, meta, multiplier=row.multiplier)
    return Idea(row.name, Origin.DECKS, reason, decks, multiplier=row.multiplier)


def adjacent_ideas(
    deck: Deck, pool: Sequence[PeerList], groups: Sequence[tuple[str, ...]], db: CardDb
) -> list[Idea]:
    """The staples of the adjacent commanders' pooled decks the deck misses, highest score first."""
    if not groups or len(pool) < MIN_PEER_LISTS:
        return []
    names = [" + ".join(short_commander_name(name) for name in group) for group in groups]
    who = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    ideas = [
        Idea(
            row.name,
            Origin.ADJACENT,
            f"in {row.peer_lists} of {len(pool)} recent {who} decks",
            row.peer_share,
            multiplier=row.multiplier,
            pool=OriginPool(row.peer_lists, len(pool)),
        )
        for row in pool_staples(deck, pool, db)
        if row.fit is not Fit.OFF_PLAN
    ]
    return _best_first(ideas)


def tagged_ideas(deck: Deck, pools: Sequence[TaggedDecks], db: CardDb) -> list[Idea]:
    """The staples of each tag's decks the deck misses; a card several tags' decks play belongs to the tag
    playing it most (the first on a tie). Highest score first."""
    best: dict[str, Idea] = {}
    for pool in pools:
        if len(pool.decks) < MIN_PEER_LISTS:
            continue
        for row in pool_staples(deck, pool.decks, db):
            if row.fit is Fit.OFF_PLAN:
                continue
            idea = Idea(
                row.name,
                Origin.TAGGED,
                f"in {row.peer_lists} of {len(pool.decks)} recent decks tagged {pool.tag}",
                row.peer_share,
                pool.tag,
                row.multiplier,
                OriginPool(row.peer_lists, len(pool.decks)),
            )
            seen = best.get(row.name.casefold())
            if seen is None or idea.score > seen.score:
                best[row.name.casefold()] = idea
    return _best_first(list(best.values()))


def compile_ideas(
    stored: StoredDeck,
    data: MetaData,
    db: CardDb,
    *,
    now: Clock,
    adjacent: Sequence[PeerList] = (),
    tagged: Sequence[TaggedDecks] = (),
) -> dict[Origin, list[BoardCard]]:
    """Put each source's highest-scoring ideas on the deck's board; what each added.

    adjacent holds the pooled decks of the commanders the deck's profile names as alike (ADR 0028); tagged
    the decks of other commanders found by each of the deck's theme tags.
    Ideas cmdr already put on the board for a source count towards its MAX_PER_SOURCE, so repeated syncs
    settle on the current top ideas rather than piling up; cards by you or Claude, or from another source,
    take no place. A turned-down idea takes no place unless its weighted share has risen by RETURN_RISE
    since it was turned down, and an applied card the deck no longer holds is an idea like any other
    (`feed_ideas`).
    """
    deck = stored.deck
    profile = read_profile(stored.folder)
    table = weighted_shares(data, now=now, grace=profile.grace_by_key())
    ideas = staple_ideas(deck, all_staples(deck, data, db, is_staple=table.is_idea), data.since, table)
    ideas += adjacent_ideas(deck, adjacent, profile.adjacent, db)
    ideas += tagged_ideas(deck, tagged, db)
    shares = table.fractions() or None
    board = read_board(stored.folder)
    main = db.resolve(deck.main_cards)
    infos = db.cards_named(idea.name for idea in ideas)
    budget = resolved_budget(deck.bracket, main, profile.max_card_cents)
    screen = IdeaFilter(
        db,
        resolve_themes(profile, db).exclude,
        name_keys_of(profile.exclude_cards),
        deck.bracket,
        len(game_changers(main)),
        profile.bracket_ceiling,
        budget,
    )
    banned = screen.banned(info.oracle_id for info in infos.values())

    def within(idea: Idea) -> bool:
        info = infos.get(idea.name.casefold())
        return budget.within(info.cheapest_cents if info else None)

    # Within the deck's working budget first, highest score first within each half (ADR 0025).
    ideas.sort(key=lambda idea: not within(idea))
    service = BoardService(stored.folder, deck, db, now=now)
    added: dict[Origin, list[BoardCard]] = {}
    for origin in Origin:
        added[origin] = []
        placed = 0
        for idea in (i for i in ideas if i.origin is origin):
            if placed >= MAX_PER_SOURCE:
                break
            info = infos.get(idea.name.casefold())
            if info is None or screen.admit(info, banned) is None:
                continue
            existing = board.find(idea.name)
            if existing is not None and existing.status not in {Status.REJECTED, Status.APPLIED}:
                if existing.origin is origin and existing.source is Actor.CMDR:
                    if idea.pool is not None and existing.origin_pool is None:
                        service.remember_pool(existing.id, idea.pool)
                    placed += 1
                continue
            # One at a time: a card the board refuses (banned, off colour) or a turned-down one the meta
            # does not bring back takes no place.
            new = feed_ideas(
                service,
                {idea.name: idea.reason},
                (),
                board,
                shares=shares,
                origin=origin,
                origin_tag=idea.tag,
                origin_pool=idea.pool,
            )
            added[origin].extend(new)
            placed += len(new)
    return added


@dataclass(slots=True, frozen=True)
class _IdeaJudge:
    """What `rejudge_ideas` tests an open idea against: the lists read and the pools of decks."""

    data: MetaData
    table: ShareTable
    profile: Profile
    # The keys at least half of the alike decks' pool plays; None when a group's decks could not be read or
    # the pool is too small to name staples. The same for each tag's pool.
    adjacent_keys: frozenset[str] | None
    # By casefolded tag; a tag with no entry here had its pool read wrongly or not at all.
    tag_keys: Mapping[str, frozenset[str] | None]
    # The casefolded theme tags the deck carries now; a tagged idea's tag that is not among them is gone.
    deck_tags: frozenset[str]

    def source_gone(self, card: BoardCard) -> bool:
        """An alike idea with no profile commander group left, or a tagged one with the tag search off or
        its tag no longer among the deck's."""
        return (card.origin is Origin.ADJACENT and not self.profile.adjacent) or (
            card.origin is Origin.TAGGED
            and (not self.profile.tag_search or (card.origin_tag or "").casefold() not in self.deck_tags)
        )

    def commanders_test(self, card: BoardCard) -> bool | None:
        """Whether the card passes the test of the commander's own lists; None when none was read."""
        return self.table.is_idea(card.name) if self.table else None

    def still_an_idea(self, card: BoardCard) -> bool:
        match card.origin:
            case Origin.ADJACENT | Origin.TAGGED if self.source_gone(card):
                return self.commanders_test(card) is not False
            case Origin.ADJACENT:
                keys = self.adjacent_keys
            case Origin.TAGGED:
                keys = self.tag_keys.get((card.origin_tag or "").casefold())
            case Origin.META if self.data.lists.data is not None and self.table:
                return self.table.is_idea(card.name)
            case Origin.DECKS if self.data.peers.data is not None and self.table:
                return self.table.is_idea(card.name)
            case None if card.source is Actor.ARCHIDEKT and self.table:
                return self.table.is_idea(card.name)
            case _:
                return True
        return keys is None or card_key(card.name) in keys


def rejudge_ideas(
    stored: StoredDeck,
    data: MetaData,
    db: CardDb,
    *,
    now: Clock,
    adjacent: Sequence[PeerList] | None = None,
    tagged: Sequence[TaggedDecks] = (),
) -> list[BoardCard]:
    """Turn down the open ideas by cmdr or from the old Maybeboard pick-up that no longer pass the test new
    ideas pass; what it turned down, with the reason on the activity line (ADR 0028).

    Meta and Archidekt-deck ideas, and old pick-ups with no origin, are tested with `ShareTable.is_idea`,
    but only against sources read now: none without a list, no meta idea without the TopDeck lists and no
    deck idea without the Archidekt decks. An alike idea is tested by the half of the pooled decks that
    plays it, a tagged one by its own tag's pool; `adjacent` is None when a group's decks could not be
    read, and a pool too small to name staples judges nothing. An alike idea whose commander groups the
    profile no longer lists, and a tagged one while the profile's tag search is off, have lost their source:
    they face the commander's own test (`ShareTable.is_idea`) like a meta idea, a failing one is turned
    down and a passing one relabelled to the source that plays it more (`relabel_pooled_open`), nothing
    without a list read. Cards by you or Claude, and queued, applied or turned-down ones, are never touched
    (`turn_down_open` checks the card is still open), nor is one the user reopened until they decide it.
    """
    profile = read_profile(stored.folder)
    table = weighted_shares(data, now=now, grace=profile.grace_by_key())
    judge = _IdeaJudge(
        data,
        table,
        profile,
        None if adjacent is None else pool_staple_keys(adjacent),
        {pool.tag.casefold(): pool_staple_keys(pool.decks) for pool in tagged},
        frozenset(tag.casefold() for tag in search_tags(stored.deck, profile, db)),
    )

    open_cards = [
        card
        for card in read_board(stored.folder).cards
        if card.source in {Actor.CMDR, Actor.ARCHIDEKT}
        and card.status is Status.OPEN
        and not card.reconsidered
    ]
    failing = [card for card in open_cards if not judge.still_an_idea(card)]
    passing_orphans = [card for card in open_cards if judge.source_gone(card) and judge.commanders_test(card)]
    if not failing and not passing_orphans:
        return []
    service = BoardService(stored.folder, stored.deck, db, now=now)
    labels = _labels(stored.deck, data, db, table, passing_orphans)
    for card in passing_orphans:
        service.relabel_pooled_open(card.id, labels[card.id])
    shares = table.fractions()
    turned_down = (
        service.turn_down_open(card.id, NO_LONGER_MAKES_THE_CUT, shares.get(card_key(card.name), 0.0))
        for card in failing
    )
    return [card for card in turned_down if card is not None]


def idea_labels(
    deck: Deck, data: MetaData | None, db: CardDb, table: ShareTable, cards: Sequence[BoardCard]
) -> dict[str, Relabel]:
    """What the lists read make of each relabellable card, by board id: the source that plays it more (the
    meta on a tie) with the reason a compiled idea of it has, as `staple_ideas` words it. A card no list
    plays, or no data at all, gets only the plain reason (`NO_LIST_PLAYS_IT`). Cards that are not
    relabellable (alike and tagged ideas among them) are left out. A play style that finds the card off
    plan does not stop the label: the user chose the card."""
    return _labels(deck, data, db, table, [card for card in cards if card.relabellable])


def _labels(
    deck: Deck, data: MetaData | None, db: CardDb, table: ShareTable, chosen: Sequence[BoardCard]
) -> dict[str, Relabel]:
    if not chosen:
        return {}
    wanted = {card_key(card.name) for card in chosen}
    found: dict[str, Idea] = {}
    if data is not None:
        staples = all_staples(deck, data, db, is_staple=lambda name: card_key(name) in wanted)
        for row in staples.rows:
            found[card_key(row.name)] = _staple_idea(deck, row, staples, data.since, table)
    labels: dict[str, Relabel] = {}
    for card in chosen:
        idea = found.get(card_key(card.name))
        labels[card.id] = Relabel(idea.origin, idea.reason) if idea else Relabel(None, NO_LIST_PLAYS_IT)
    return labels


def relabel_legacy_ideas(stored: StoredDeck, data: MetaData, db: CardDb, *, now: Clock) -> list[BoardCard]:
    """Label the open ideas of the old Maybeboard pick-up that pass the idea test as ideas cmdr compiled,
    from the source that plays them more, so none stays neutral in Considering; what it relabelled. Nothing
    without a list read (`rejudge_ideas` judges none then either)."""
    table = weighted_shares(data, now=now, grace=read_profile(stored.folder).grace_by_key())
    if not table:
        return []
    legacy = [
        card
        for card in read_board(stored.folder).cards
        if card.source is Actor.ARCHIDEKT
        and card.status is Status.OPEN
        and card.relabellable
        and table.is_idea(card.name)
    ]
    labels = idea_labels(stored.deck, data, db, table, legacy)
    service = BoardService(stored.folder, stored.deck, db, now=now)
    relabelled = (service.relabel_open(card.id, labels[card.id]) for card in legacy)
    return [card for card in relabelled if card is not None]


def search_tags(deck: Deck, profile: Profile, db: CardDb) -> list[str]:
    """The deck tags whose decks are searched: none while the profile's tag search is off, else at most the
    MAX_SEARCH_TAGS that the most of the deck's main cards match, ties in Archidekt's tag order."""
    if not profile.tag_search:
        return []
    # Theme tags only: the play style, role and card type tags are too broad to pick decks by (ADR 0009).
    themes = [t for t in merge_themes(deck, profile, db).used if t.source is ThemeSource.DECK_TAG]
    if len(themes) <= 1:
        return [t.name for t in themes]
    oracle_ids = [r.info.oracle_id for r in db.resolve(deck.main_cards) if r.info is not None]
    ids = db.tag_ids([slug for t in themes for slug in t.spec.tags])
    matches = {t.name: len(db.matching(resolve_spec(t.spec, ids), oracle_ids)) for t in themes}
    # sorted is stable, so equal counts stay in the order of the deck's tags.
    return sorted(matches, key=lambda name: -matches[name])[:MAX_SEARCH_TAGS]


def gather_idea_pools(
    stored: StoredDeck,
    db: CardDb,
    sources: MetaSources,
    cache_root: Path,
    *,
    now: Clock,
    refresh: bool,
    warn: Callable[[str], None],
    on_progress: Progress | None = None,
    on_done: Progress | None = None,
    prune: bool = True,
) -> AdjacentDecks:
    """The recent decks of the commanders the deck's profile names as alike, and of other commanders carrying
    its theme tags (only when the profile turns the tag search on), gathered first (ADR 0028); a commander or
    tag whose decks cannot be read is named to warn, and the rest carry on."""
    profile = read_profile(stored.folder)
    tags = search_tags(stored.deck, profile, db)
    pools = gather_adjacent(
        stored.deck,
        profile.adjacent,
        sources,
        cache_root,
        now=now,
        refresh=refresh,
        tags=tags,
        on_progress=on_progress,
        on_done=on_done,
        prune=prune,
    )
    for name, why in pools.errors:
        warn(f"{stored.deck.short_name}: no recent {name} decks to learn from ({why})")
    return pools


def compile_ideas_with_adjacent(
    stored: StoredDeck,
    data: MetaData,
    db: CardDb,
    sources: MetaSources,
    cache_root: Path,
    *,
    now: Clock,
    refresh: bool,
    warn: Callable[[str], None],
    on_progress: Progress | None = None,
) -> dict[Origin, list[BoardCard]]:
    """`compile_ideas` with the pools `gather_idea_pools` reads."""
    pools = gather_idea_pools(
        stored, db, sources, cache_root, now=now, refresh=refresh, warn=warn, on_progress=on_progress
    )
    return compile_ideas(stored, data, db, now=now, adjacent=pools.decks, tagged=pools.tagged)


def ideas_summary(added: dict[Origin, list[BoardCard]]) -> str:
    """ "+2 from meta, +1 from archidekt decks", or "no new ideas"."""
    parts = [f"+{len(cards)} from {origin.value}" for origin, cards in added.items() if cards]
    return ", ".join(parts) or "no new ideas"
