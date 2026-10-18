"""Ideas for Considering, compiled on every sync (ADR 0028).

Decks at brackets 4 and 5 get the staples of their commander's EDHTop16 lists ("meta") and of its most-viewed
recent Archidekt decks ("archidekt decks"), the latter at the deck's own bracket and the one above; lower
brackets get the Archidekt ones only. A card is one of these ideas when the lower bound of its weighted
share, over every list read, reaches IDEA_LOWER_BOUND (ADR 0026); it belongs to the source that plays it
more. An idea's score is that source's plain share times its weight for the deck's play style (ADR 0031).
Each source puts at most MAX_PER_SOURCE ideas on the board, highest score first. An idea with no deck card
to replace is still put on the board, where the app lists it as "No cut yet". A deck whose profile names
commanders that play alike also gets the cards at least half of their pooled recent Archidekt decks play
("adjacent decks"). So does one whose Archidekt theme tags find recent decks of other commanders ("tagged
decks"), each tag's decks a pool of their own. Ideas pass the profile's exclusions, bracket ceiling and
price cap; below bracket 5 also the deck's budget (ADR 0025), those within its working budget first.
EDHREC is never asked (ADR 0003): its rank, from Scryfall's data, only orders the cards an idea could
replace.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from commander.analysis import game_changers
from commander.board import BoardCard, BoardService, Origin, Status, feed_ideas, read_board
from commander.budget import resolved_budget
from commander.carddb import CardDb
from commander.deckcache import Clock
from commander.deckprofile import read_profile, resolve_themes
from commander.deckthemes import ThemeSource, merge_themes
from commander.meta import MIN_PEER_LISTS, Basis, Staples, all_staples, pool_staples
from commander.metadata import MetaData, MetaSources, PeerList, Progress, TaggedDecks, gather_adjacent
from commander.models import Deck, short_commander_name
from commander.playstyle import Fit
from commander.store import StoredDeck
from commander.suggest import IdeaFilter, name_keys_of
from commander.weightedshare import weighted_shares

MAX_PER_SOURCE = 50


@dataclass(slots=True, frozen=True)
class Idea:
    name: str
    origin: Origin
    reason: str
    # The share of the source's lists or decks playing the card.
    share: float
    # For a tagged idea: the deck tag whose decks play it.
    tag: str | None = None
    # What the deck's play style makes of the card (`playstyle.Weight`): the share's factor in the score.
    multiplier: float = 1.0

    @property
    def score(self) -> float:
        """What ideas are taken in the order of: the share weighted by the deck's play style."""
        return self.share * self.multiplier


def _best_first(ideas: list[Idea]) -> list[Idea]:
    return sorted(ideas, key=lambda idea: (-idea.score, idea.name))


def staple_ideas(deck: Deck, staples: Staples, since: date) -> list[Idea]:
    """The missing staples as ideas, each from the source that plays it more: "meta" for the EDHTop16 lists
    (also on a tie), else "archidekt decks". The rows are staples already: `compile_ideas` picks them by the
    weighted share's lower bound."""
    lists = f"{deck.short_name} lists" if staples.basis is Basis.ALL else f"top-cut {deck.short_name} lists"
    ideas: list[Idea] = []
    # What the deck's play style doesn't need (a Turbo deck's interaction) is no idea for it (ADR 0031).
    for row in (r for r in staples.rows if r.fit is not Fit.OFF_PLAN):
        parts: list[str] = []
        if staples.lists:
            parts.append(f"in {row.lists} of {staples.lists} {lists} on EDHTop16 since {since}")
        if staples.peers:
            parts.append(f"in {row.peer_lists} of {staples.peers} most-viewed recent Archidekt decks")
        reason = "; ".join(parts)
        if staples.lists and (not staples.peers or row.share >= row.peer_share):
            ideas.append(Idea(row.name, Origin.META, reason, row.share, multiplier=row.multiplier))
        else:
            ideas.append(Idea(row.name, Origin.DECKS, reason, row.peer_share, multiplier=row.multiplier))
    return _best_first(ideas)


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
    Ideas already on the board count towards a source's MAX_PER_SOURCE, so repeated syncs settle on the
    current top ideas rather than piling up. A turned-down idea takes no place unless the meta brings it
    back (`feed_ideas`).
    """
    deck = stored.deck
    profile = read_profile(stored.folder)
    table = weighted_shares(data, now=now)
    ideas = staple_ideas(deck, all_staples(deck, data, db, is_staple=table.is_idea), data.since)
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
            if existing is not None and existing.status is not Status.REJECTED:
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
            )
            added[origin].extend(new)
            placed += len(new)
    return added


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
    """`compile_ideas` with the recent decks of the commanders the deck's profile names as alike, and of other
    commanders carrying its theme tags, gathered first (ADR 0028); a commander or tag whose decks cannot be
    read is named to warn, and the rest carry on."""
    profile = read_profile(stored.folder)
    # Theme tags only: the play style, role and card type tags are too broad to pick decks by (ADR 0009).
    tags = [t.name for t in merge_themes(stored.deck, profile, db).used if t.source is ThemeSource.DECK_TAG]
    adjacent = gather_adjacent(
        stored.deck,
        profile.adjacent,
        sources,
        cache_root,
        now=now,
        refresh=refresh,
        tags=tags,
        on_progress=on_progress,
    )
    for name, why in adjacent.errors:
        warn(f"{stored.deck.short_name}: no recent {name} decks to learn from ({why})")
    return compile_ideas(stored, data, db, now=now, adjacent=adjacent.decks, tagged=adjacent.tagged)


def ideas_summary(added: dict[Origin, list[BoardCard]]) -> str:
    """ "+2 from meta, +1 from archidekt decks", or "no new ideas"."""
    parts = [f"+{len(cards)} from {origin.value}" for origin, cards in added.items() if cards]
    return ", ".join(parts) or "no new ideas"
