"""Ideas for Considering, compiled on every sync (ADR 0014).

Bracket 5 decks get the staples of their commander's EDHTop16 lists ("meta") and of the most-viewed recent
Archidekt decks for the commander ("archidekt decks"); decks below bracket 5 get the Archidekt ones only,
from decks at their own bracket. Each source puts at most MAX_PER_SOURCE ideas on the board, most played
first, and only ideas with a deck card to replace. A deck whose profile names commanders that play alike
also gets the staples of their pooled recent Archidekt decks ("adjacent decks", ADR 0015). Ideas pass the
profile's exclusions, bracket ceiling and price cap; below bracket 5 also the deck's budget (ADR 0025),
those within its working budget first. EDHREC is never asked (ADR 0003): its rank, from
Scryfall's data, only orders the cards an idea could replace.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from commander.analysis import game_changers
from commander.board import BoardCard, BoardService, Origin, Status, feed_ideas, read_board
from commander.budget import resolved_budget
from commander.carddb import CardDb
from commander.deckcache import Clock
from commander.deckprofile import read_profile, resolve_themes
from commander.meta import (
    MIN_PEER_LISTS,
    MIN_STAPLE_LISTS,
    PEER_STAPLE_SHARE,
    STAPLE_SHARE,
    Basis,
    Staples,
    all_staples,
    card_shares,
    pool_staples,
)
from commander.metadata import MetaData, PeerList
from commander.models import Deck, short_commander_name
from commander.playstyle import Fit
from commander.store import StoredDeck
from commander.suggest import IdeaFilter, name_keys_of
from commander.targets import deck_cut_rules, role_targets, with_ideas

MAX_PER_SOURCE = 50


@dataclass(slots=True, frozen=True)
class Idea:
    name: str
    origin: Origin
    reason: str
    # The share of the source's lists or decks playing the card: most played first.
    share: float


def staple_ideas(deck: Deck, staples: Staples, since: date) -> list[Idea]:
    """The missing staples as ideas: "meta" when the EDHTop16 lists make it one, else "archidekt decks"."""
    lists = f"{deck.short_name} lists" if staples.basis is Basis.ALL else f"top-cut {deck.short_name} lists"
    ideas: list[Idea] = []
    # What the deck's play style doesn't need (a Turbo deck's interaction) is no idea for it (ADR 0022).
    for row in (r for r in staples.rows if r.fit is not Fit.OFF_PLAN):
        parts: list[str] = []
        if staples.lists:
            parts.append(f"in {row.lists} of {staples.lists} {lists} on EDHTop16 since {since}")
        if staples.peers:
            parts.append(f"in {row.peer_lists} of {staples.peers} most-viewed recent Archidekt decks")
        reason = "; ".join(parts)
        if row.lists >= MIN_STAPLE_LISTS and row.share >= STAPLE_SHARE:
            ideas.append(Idea(row.name, Origin.META, reason, row.share))
        elif staples.peers >= MIN_PEER_LISTS and row.peer_share >= PEER_STAPLE_SHARE:
            ideas.append(Idea(row.name, Origin.DECKS, reason, row.peer_share))
    ideas.sort(key=lambda idea: (-idea.share, idea.name))
    return ideas


def adjacent_ideas(
    deck: Deck, pool: Sequence[PeerList], groups: Sequence[tuple[str, ...]], db: CardDb
) -> list[Idea]:
    """The staples of the adjacent commanders' pooled decks the deck misses, most played first."""
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
        )
        for row in pool_staples(deck, pool, db)
        if row.fit is not Fit.OFF_PLAN
    ]
    ideas.sort(key=lambda idea: (-idea.share, idea.name))
    return ideas


def compile_ideas(
    stored: StoredDeck, data: MetaData, db: CardDb, *, now: Clock, adjacent: Sequence[PeerList] = ()
) -> dict[Origin, list[BoardCard]]:
    """Put each source's most-played ideas with a card to replace on the deck's board; what each added.

    adjacent holds the pooled decks of the commanders the deck's profile names as alike (ADR 0015).
    Ideas already on the board count towards a source's MAX_PER_SOURCE, so repeated syncs settle on the
    current top ideas rather than piling up. A turned-down idea takes no place unless the meta brings it
    back (`feed_ideas`).
    """
    deck = stored.deck
    profile = read_profile(stored.folder)
    ideas = staple_ideas(deck, all_staples(deck, data, db), data.since)
    ideas += adjacent_ideas(deck, adjacent, profile.adjacent, db)
    shares = card_shares(data) or None
    board = read_board(stored.folder)
    main = db.resolve(deck.main_cards)
    infos = db.cards_named(idea.name for idea in ideas)
    rules = with_ideas(deck_cut_rules(deck, main, profile, db, data), infos.values(), db)
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

    # Within the deck's working budget first, most played first within each half (ADR 0025).
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
            if info is None or screen.admit(info, banned) is None or not role_targets(info, main, rules):
                continue
            existing = board.find(idea.name)
            if existing is not None and existing.status is not Status.REJECTED:
                placed += 1
                continue
            # One at a time: a card the board refuses (banned, off colour) or a turned-down one the meta
            # does not bring back takes no place.
            new = feed_ideas(service, {idea.name: idea.reason}, (), board, shares=shares, origin=origin)
            added[origin].extend(new)
            placed += len(new)
    return added


def ideas_summary(added: dict[Origin, list[BoardCard]]) -> str:
    """ "+2 from meta, +1 from archidekt decks", or "no new ideas"."""
    parts = [f"+{len(cards)} from {origin.value}" for origin, cards in added.items() if cards]
    return ", ".join(parts) or "no new ideas"
