"""Ideas for Considered, compiled on every sync (ADR 0014).

Bracket 5 decks get the staples of their commander's EDHTop16 lists ("meta") and of the most-viewed recent
Archidekt decks for the commander ("archidekt decks"); decks below bracket 5 get the Archidekt ones only,
from decks at their own bracket. Each source puts at most MAX_PER_SOURCE ideas on the board, most played
first, and only ideas with a deck card to replace. EDHREC is never asked (ADR 0003): its rank, from
Scryfall's data, only orders the cards an idea could replace.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from commander.analysis import game_changers
from commander.board import BoardCard, BoardService, Origin, Status, feed_ideas, read_board
from commander.carddb import CardDb, CardInfo, ResolvedCard
from commander.deckcache import Clock
from commander.deckprofile import Profile, read_profile, resolve_themes
from commander.meta import (
    MIN_PEER_LISTS,
    MIN_STAPLE_LISTS,
    PEER_STAPLE_SHARE,
    STAPLE_SHARE,
    Basis,
    Staples,
    all_staples,
    card_shares,
)
from commander.metadata import MetaData
from commander.models import Deck
from commander.store import StoredDeck
from commander.suggest import affordable, card_raise, name_keys
from commander.targets import kept_by_profile, role_targets

MAX_PER_SOURCE = 10


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
    for row in staples.rows:
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


def compile_ideas(
    stored: StoredDeck, data: MetaData, db: CardDb, *, now: Clock
) -> dict[Origin, list[BoardCard]]:
    """Put each source's most-played ideas with a card to replace on the deck's board; what each added.

    Ideas already on the board count towards a source's MAX_PER_SOURCE, so repeated syncs settle on the
    current top ideas rather than piling up. A turned-down idea takes no place unless the meta brings it
    back (`feed_ideas`).
    """
    deck = stored.deck
    ideas = staple_ideas(deck, all_staples(deck, data, db), data.since)
    shares = card_shares(data) or None
    board = read_board(stored.folder)
    main = db.resolve(deck.main_cards)
    profile = read_profile(stored.folder)
    kept = kept_by_profile(main, profile)
    infos = db.cards_named(idea.name for idea in ideas)
    allowed = _profile_filter(deck, profile, main, db, infos.values())
    service = BoardService(stored.folder, deck, db, now=now)
    added: dict[Origin, list[BoardCard]] = {}
    for origin in Origin:
        added[origin] = []
        placed = 0
        for idea in (i for i in ideas if i.origin is origin):
            if placed >= MAX_PER_SOURCE:
                break
            info = infos.get(idea.name.casefold())
            if (
                info is None
                or not allowed(info)
                or not role_targets(info, main, protected=kept, shares=shares)
            ):
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


def _profile_filter(
    deck: Deck, profile: Profile, main: Sequence[ResolvedCard], db: CardDb, infos: Iterable[CardInfo]
) -> Callable[[CardInfo], bool]:
    """Whether the profile lets a card in, as it does for `suggest`: not excluded by name, type, keyword
    or tag, not lifting the deck over its bracket ceiling, and within its price cap."""
    banned = db.matching(resolve_themes(profile, db).exclude, (info.oracle_id for info in infos))
    banned_names = {key for name in profile.exclude_cards for key in name_keys(name)}
    changers = len(game_changers(main))

    def allowed(info: CardInfo) -> bool:
        if info.oracle_id in banned or name_keys(info.name) & banned_names:
            return False
        raises = card_raise(info, deck.bracket, changers)
        ceiling = profile.bracket_ceiling
        if raises is not None and ceiling is not None and raises.bracket > ceiling:
            return False
        return affordable(info.cheapest_cents, profile.max_card_cents)

    return allowed


def ideas_summary(added: dict[Origin, list[BoardCard]]) -> str:
    """ "+2 from meta, +1 from archidekt decks", or "no new ideas"."""
    parts = [f"+{len(cards)} from {origin.value}" for origin, cards in added.items() if cards]
    return ", ".join(parts) or "no new ideas"
