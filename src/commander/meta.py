"""Reading other players' lists against a deck: what performing lists play that the deck doesn't, their spicy
picks, the deck's own outliers, and which cards the most-played commanders answer the deck's key cards with.
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import Enum

from commander.answers import AnswerKind, card_key, piece_facts
from commander.archidekt import PeerDeck
from commander.carddb import CardDb, CardInfo
from commander.deckcache import Answer, AnswerSource
from commander.deckprofile import Profile
from commander.edhtop16 import CommanderStats, Entry
from commander.keycards import KeyCard, key_cards
from commander.metacombos import AnswerRole, DeckAnswers, MetaCombo, combo_answers, infos_answers, meta_combos
from commander.metadata import MetaCommander, MetaData, PeerList
from commander.models import COMMANDER_CATEGORY, Deck
from commander.playstyle import NEUTRAL, Fit, PlayStyle, Weight, deck_play_style, staple_weights
from commander.spellbook import ComboDetail
from commander.suggest import deck_identity

# Fewer lists with decklists than this is too few to judge a card by, whatever placed.
THIN_LISTS = 10
# A card missing from the deck is a staple when this share of performing lists play it (at least two lists)...
STAPLE_SHARE = 0.4
MIN_STAPLE_LISTS = 2
# ...or this share of the Archidekt lists, when there are at least three of them.
PEER_STAPLE_SHARE = 0.5
MIN_PEER_LISTS = 3
# EDHTop16's spice runs 0-4; 3 and up is a new or rarely played card.
SPICY = 3
MIN_SPICY_LISTS = 2
# A deck card this few performing lists play is the deck's own pick.
OUTLIER_SHARE = 0.1
# A meta card is a threat when this share of one top commander's lists play it.
THREAT_SHARE = 0.3
# The deck's own combos shown, fewest pieces first: the short ones are the lines it plays for.
MAX_OWN_COMBOS = 15
MAX_ROWS = 15


class Basis(Enum):
    TOP_CUT = "top-cut lists"
    ALL = "every list in the window"


@dataclass(slots=True, frozen=True)
class Sample:
    known: bool
    entries: int
    with_lists: int
    top_cuts: int
    performing: int
    basis: Basis

    @property
    def thin(self) -> bool:
        return self.basis is Basis.ALL or self.with_lists < THIN_LISTS


@dataclass(slots=True, frozen=True)
class CardShare:
    name: str
    lists: int
    share: float
    peer_lists: int
    peer_share: float
    # The highest spice EDHTop16 gave the card in a performing list; None when none plays it.
    spice: int | None
    # Kept by the profile (for the deck's own cards); never set for a missing card.
    flagged: bool
    # For a missing card: how it sits with the deck's play style (ADR 0031), and the weight its shares are
    # multiplied by to rank it.
    fit: Fit = Fit.NEUTRAL
    multiplier: float = 1.0


@dataclass(slots=True, frozen=True)
class SpicyPick:
    name: str
    spice: int
    lists: int
    best: Entry


@dataclass(slots=True, frozen=True)
class Threat:
    name: str
    kinds: tuple[AnswerKind, ...]
    hits: tuple[str, ...]
    # Sum over the top commanders of meta share x the share of their lists playing it: how often it shows up.
    exposure: float
    played_by: tuple[tuple[str, float], ...]


@dataclass(slots=True, frozen=True)
class MetaAnswer:
    """A card the meta plays that stops one of the deck's combos, and how often it shows up."""

    card: str
    how: str
    role: AnswerRole
    catch: str
    exposure: float
    played_by: tuple[tuple[str, float], ...]


@dataclass(slots=True, frozen=True)
class OwnCombo:
    """One of the deck's own combos with the meta cards that stop it: stax, then removal, then counters."""

    id: str
    url: str
    pieces: tuple[str, ...]
    results: tuple[str, ...]
    answers: tuple[MetaAnswer, ...]


@dataclass(slots=True, frozen=True)
class SourceState:
    name: str
    source: AnswerSource
    fetched_at: datetime | None
    error: str | None


@dataclass(slots=True, frozen=True)
class MetaReport:
    deck: str
    commander: str
    since: date
    sample: Sample
    missing: tuple[CardShare, ...]
    spicy: tuple[SpicyPick, ...]
    outliers: tuple[CardShare, ...]
    threats: tuple[Threat, ...]
    combos: tuple[MetaCombo, ...]
    own_combos: tuple[OwnCombo, ...]
    field: tuple[CommanderStats, ...]
    peers: tuple[PeerDeck, ...]
    peers_skipped: int
    key_cards: tuple[KeyCard, ...]
    sources: tuple[SourceState, ...]
    style: PlayStyle = NEUTRAL


@dataclass(slots=True, frozen=True)
class CardCheck:
    name: str
    in_deck: bool
    performing: int
    performing_with: int
    lists: int
    lists_with: int
    top_cuts_with: int
    top_cuts_without: int
    mean_placing_with: float | None
    mean_placing_without: float | None
    peers: int
    peers_with: int
    spice: int | None
    meta_lists: int
    meta_lists_with: int


def analyse(deck: Deck, data: MetaData, profile: Profile, db: CardDb) -> MetaReport:
    lists = _window(data.lists.data.entries if data.lists.data else (), data.since)
    performing, basis = performing_lists(lists)
    entries = len(lists)
    with_lists = [e for e in lists if e.cards]
    sample = Sample(
        known=data.lists.data.known if data.lists.data else True,
        entries=entries,
        with_lists=len(with_lists),
        top_cuts=sum(1 for e in lists if e.made_top_cut),
        performing=len(performing),
        basis=basis,
    )
    peer_lists = data.peers.data.decks if data.peers.data else ()
    field = tuple(
        MetaCommander(c.stats, _window(c.entries, data.since)) for c in (data.field.data or ()) if c.entries
    )
    keys = key_cards(deck, profile, data.own_combos.data or (), peer_lists, db)
    names = _display_names(performing, peer_lists, deck)
    style = deck_play_style(deck)
    pool = meta_pool(field, db)
    return MetaReport(
        deck=deck.short_name,
        commander=data.commander,
        since=data.since,
        sample=sample,
        missing=missing_staples(deck, performing, peer_lists, names, db, style=style),
        spicy=spicy_picks(deck, performing),
        outliers=outliers(deck, performing, peer_lists, profile, db),
        threats=threats((k.name for k in keys), pool, db),
        combos=meta_combos(deck, field, data.combos.data or (), data.since, db),
        own_combos=own_combos(data.own_combos.data or (), pool, db),
        field=tuple(c.stats for c in (data.field.data or ())),
        peers=tuple(p.deck for p in peer_lists),
        peers_skipped=data.peers.data.skipped if data.peers.data else 0,
        key_cards=keys,
        sources=(
            _state("EDHTop16 lists", data.lists),
            _state("EDHTop16 meta", data.field),
            _state("Archidekt decks", data.peers),
            _state("Spellbook combos", data.combos),
            _state("Spellbook deck combos", data.own_combos),
        ),
        style=style,
    )


def performing_lists(entries: Sequence[Entry]) -> tuple[list[Entry], Basis]:
    """The lists to learn from: top-cut ones, or every list best placing first when none made a cut."""
    with_lists = [e for e in entries if e.cards]
    top = [e for e in with_lists if e.made_top_cut]
    if top:
        return sorted(top, key=lambda e: e.placing), Basis.TOP_CUT
    return sorted(with_lists, key=lambda e: e.placing), Basis.ALL


def missing_staples(
    deck: Deck,
    performing: Sequence[Entry],
    peers: Sequence[PeerList],
    names: Mapping[str, str],
    db: CardDb,
    *,
    limit: int | None = MAX_ROWS,
    style: PlayStyle = NEUTRAL,
    is_staple: Callable[[str], bool] | None = None,
) -> tuple[CardShare, ...]:
    """The staples the deck misses, highest share times play-style weight first.

    A card is a staple by the share thresholds, or by is_staple(name) when the caller judges it.
    """
    in_deck = {card_key(card.name) for card in deck.main_cards}
    counts = _list_counts(performing)
    peer_counts = _peer_counts(peers)
    spice = _top_spice(performing)
    basics = _basics(db, names.values())
    rows: list[CardShare] = []
    for key in set(counts) | set(peer_counts):
        if key in in_deck or key in basics:
            continue
        share = counts[key] / len(performing) if performing else 0.0
        peer_share = peer_counts[key] / len(peers) if peers else 0.0
        if is_staple is not None:
            wanted = is_staple(names[key])
        else:
            staple = counts[key] >= MIN_STAPLE_LISTS and share >= STAPLE_SHARE
            wanted = staple or (len(peers) >= MIN_PEER_LISTS and peer_share >= PEER_STAPLE_SHARE)
        if wanted:
            rows.append(
                CardShare(
                    names[key],
                    counts[key],
                    share,
                    peer_counts[key],
                    peer_share,
                    spice.get(key),
                    flagged=False,
                )
            )
    rows = _playable(rows, deck, db)
    weights = staple_weights((r.name for r in rows), style, db)
    rows = [_weighed(r, weights.get(r.name.casefold())) for r in rows]
    rows.sort(key=lambda r: (-r.share * r.multiplier, -r.peer_share * r.multiplier, r.name))
    return tuple(rows if limit is None else rows[:limit])


def _weighed(row: CardShare, weight: Weight | None) -> CardShare:
    return row if weight is None else replace(row, fit=weight.fit, multiplier=weight.multiplier)


def _playable(rows: Sequence[CardShare], deck: Deck, db: CardDb) -> list[CardShare]:
    """The rows the deck could play: lists older than a ban, or in other colours, name cards it can't."""
    identity = deck_identity(db.resolve(deck.main_cards))
    found = db.cards_named(r.name for r in rows)
    kept: list[CardShare] = []
    for row in rows:
        # A name the card data doesn't spell the same may still be a two-faced card's front face.
        info = found.get(row.name.casefold()) or db.card_named(row.name)
        if info is not None and not (
            info.commander_legal and (identity is None or set(info.color_identity) <= set(identity))
        ):
            continue
        kept.append(row)
    return kept


@dataclass(slots=True, frozen=True)
class Staples:
    """Every staple the deck misses, and what was read to find them."""

    rows: tuple[CardShare, ...]
    # Lists read from EDHTop16 (top-cut ones when enough made a cut), and Archidekt decks read.
    lists: int
    basis: Basis
    peers: int


def pool_staples(deck: Deck, pool: Sequence[PeerList], db: CardDb) -> tuple[CardShare, ...]:
    """The cards at least half of a pool of Archidekt decks play that the deck misses, uncapped."""
    names = _display_names((), pool, deck)
    return missing_staples(deck, (), pool, names, db, limit=None, style=deck_play_style(deck))


def all_staples(
    deck: Deck, data: MetaData, db: CardDb, *, is_staple: Callable[[str], bool] | None = None
) -> Staples:
    """The staples the deck misses, uncapped: what the commander's lists or recent Archidekt decks play."""
    lists = _window(data.lists.data.entries if data.lists.data else (), data.since)
    performing, basis = performing_lists(lists)
    peers = data.peers.data.decks if data.peers.data else ()
    names = _display_names(performing, peers, deck)
    rows = missing_staples(
        deck, performing, peers, names, db, limit=None, style=deck_play_style(deck), is_staple=is_staple
    )
    return Staples(rows, len(performing), basis, len(peers))


def spicy_picks(deck: Deck, performing: Sequence[Entry]) -> tuple[SpicyPick, ...]:
    in_deck = {card_key(card.name) for card in deck.main_cards}
    picks: dict[str, SpicyPick] = {}
    # Best placings first, so the first list seen with a card is its best showing.
    for entry in sorted(performing, key=lambda e: e.placing):
        for card in entry.cards:
            key = card_key(card.name)
            if card.spice < SPICY or key in in_deck:
                continue
            seen = picks.get(key)
            if seen is None:
                picks[key] = SpicyPick(card.name, card.spice, 1, entry)
            else:
                picks[key] = SpicyPick(seen.name, max(seen.spice, card.spice), seen.lists + 1, seen.best)
    # One list's oddities are noise unless that list made the cut.
    kept = [p for p in picks.values() if p.lists >= MIN_SPICY_LISTS or p.best.made_top_cut]
    ranked = sorted(kept, key=lambda p: (-p.lists, p.best.placing, p.name))
    return tuple(ranked[:MAX_ROWS])


def outliers(
    deck: Deck, performing: Sequence[Entry], peers: Sequence[PeerList], profile: Profile, db: CardDb
) -> tuple[CardShare, ...]:
    """Deck cards few performing lists play: the deck's own picks, and the first places to look for cuts."""
    if not performing:
        return ()
    counts = _list_counts(performing)
    peer_counts = _peer_counts(peers)
    basics = _basics(db, (card.name for card in deck.main_cards))
    rows: list[CardShare] = []
    for card in deck.main_cards:
        key = card_key(card.name)
        if COMMANDER_CATEGORY in card.categories or key in basics:
            continue
        share = counts[key] / len(performing)
        if share >= OUTLIER_SHARE:
            continue
        kept = profile.keeps(card.name, card.categories)
        peer_share = peer_counts[key] / len(peers) if peers else 0.0
        rows.append(CardShare(card.name, counts[key], share, peer_counts[key], peer_share, None, kept))
    rows.sort(key=lambda r: (r.share, r.peer_share, r.name))
    return tuple(rows)


def performing_entries(data: MetaData) -> list[Entry]:
    """The commander's lists to learn from in the window: top cuts, or every list when none made one."""
    lists = _window(data.lists.data.entries if data.lists.data else (), data.since)
    return performing_lists(lists)[0]


def listed_entries(data: MetaData) -> list[Entry]:
    """Every list with a decklist in the window, whatever it placed."""
    return [e for e in _window(data.lists.data.entries if data.lists.data else (), data.since) if e.cards]


# Per top commander playing a card: (commander, share of its lists, the commander's meta share).
type _Plays = list[tuple[str, float, float]]


def _meta_pool(field: Sequence[MetaCommander]) -> dict[str, tuple[str, _Plays]]:
    """The cards at least THREAT_SHARE of some top commander's lists play, by `card_key`: name and plays."""
    shares: dict[str, _Plays] = defaultdict(list)
    names: dict[str, str] = {}
    for commander in field:
        lists = [e for e in commander.entries if e.cards]
        if not lists:
            continue
        for key, count in _list_counts(lists).items():
            shares[key].append((commander.stats.name, count / len(lists), commander.stats.meta_share))
        for entry in lists:
            for card in entry.cards:
                names.setdefault(card_key(card.name), card.name)
    return {
        key: (names[key], rows)
        for key, rows in shares.items()
        if max(share for _, share, _ in rows) >= THREAT_SHARE
    }


@dataclass(slots=True, frozen=True)
class MetaPool:
    """The cards the top commanders' lists commonly play, with what each can do against a combo."""

    plays: Mapping[str, tuple[str, _Plays]]
    # The card data of each, by `card_key`; a card it lacks is left out.
    infos: Mapping[str, CardInfo]
    answers: DeckAnswers


def meta_pool(field: Sequence[MetaCommander], db: CardDb) -> MetaPool:
    """The meta's common cards and their answers, worked out once for every report that reads them."""
    plays = _meta_pool(field)
    found = db.cards_named(name for name, _ in plays.values())
    infos = {card_key(info.name): info for info in found.values()}
    return MetaPool(plays, infos, infos_answers(found.values(), db))


def _exposure(rows: _Plays) -> float:
    """Sum over the top commanders of meta share x the share of their lists playing the card."""
    return sum(share * meta for _, share, meta in rows)


def _played_by(rows: _Plays) -> tuple[tuple[str, float], ...]:
    return tuple(sorted(((n, s) for n, s, _ in rows if s >= THREAT_SHARE), key=lambda r: -r[1]))


def own_combos(combos: Sequence[ComboDetail], pool: MetaPool, db: CardDb) -> tuple[OwnCombo, ...]:
    """The deck's combos, fewest pieces first, each with the cards the meta commonly plays that stop it."""
    if not combos:
        return ()
    facts = piece_facts(db, {(p.name, p.type_line) for combo in combos for p in combo.pieces})
    order = list(AnswerRole)
    found: list[OwnCombo] = []
    for combo in combos:
        stopping: list[MetaAnswer] = []
        for answer in combo_answers(combo, pool.answers, facts):
            plays = pool.plays.get(card_key(answer.card))
            if plays is None:
                continue
            rows = plays[1]
            stopping.append(
                MetaAnswer(
                    answer.card, answer.how, answer.role, answer.catch, _exposure(rows), _played_by(rows)
                )
            )
        stopping.sort(key=lambda a: (order.index(a.role), -a.exposure, a.card))
        found.append(
            OwnCombo(combo.id, combo.url, tuple(p.name for p in combo.pieces), combo.results, tuple(stopping))
        )
    found.sort(key=lambda c: (len(c.pieces), c.id))
    return tuple(found[:MAX_OWN_COMBOS])


def threats(pieces: Iterable[str], pool: MetaPool, db: CardDb) -> tuple[Threat, ...]:
    """Cards the top commanders' lists commonly play that answer at least one key piece."""
    # A key piece the card data lacks has no type line, so no answer could be matched to it.
    piece_info = [p for p in piece_facts(db, ((name, "") for name in pieces)).values() if p.type_line]
    found: list[Threat] = []
    for key, (_, rows) in pool.plays.items():
        info = pool.infos.get(key)
        if info is None:
            continue
        answer = pool.answers.removal.get(info.name)
        if answer is None:
            continue
        hit = sorted({p.name for p in piece_info if answer.removes(p) or answer.counters(p)})
        if not hit:
            continue
        found.append(
            Threat(
                name=info.name,
                kinds=answer.kinds,
                hits=tuple(hit),
                exposure=_exposure(rows),
                played_by=_played_by(rows),
            )
        )
    found.sort(key=lambda t: (-t.exposure, t.name))
    return tuple(found)


def check_card(name: str, deck: Deck, data: MetaData) -> CardCheck:
    """How the commander's lists, Archidekt lists and the meta's lists use one card."""
    key = card_key(name)
    lists = [e for e in _window(data.lists.data.entries if data.lists.data else (), data.since) if e.cards]
    performing, _ = performing_lists(lists)
    having: list[Entry] = []
    lacking: list[Entry] = []
    for e in lists:
        (having if _plays(e, key) else lacking).append(e)
    peers = data.peers.data.decks if data.peers.data else ()
    meta_lists = [e for c in (data.field.data or ()) for e in _window(c.entries, data.since) if e.cards]
    spice = [c.spice for e in lists for c in e.cards if card_key(c.name) == key]
    return CardCheck(
        name=name,
        in_deck=key in {card_key(card.name) for card in deck.main_cards},
        performing=len(performing),
        performing_with=sum(1 for e in performing if _plays(e, key)),
        lists=len(lists),
        lists_with=len(having),
        top_cuts_with=sum(1 for e in having if e.made_top_cut),
        top_cuts_without=sum(1 for e in lacking if e.made_top_cut),
        mean_placing_with=_mean(e.placing for e in having),
        mean_placing_without=_mean(e.placing for e in lacking),
        peers=len(peers),
        peers_with=sum(1 for p in peers if key in {card_key(n) for n in p.cards}),
        spice=max(spice) if spice else None,
        meta_lists=len(meta_lists),
        meta_lists_with=sum(1 for e in meta_lists if _plays(e, key)),
    )


def _plays(entry: Entry, key: str) -> bool:
    return any(card_key(c.name) == key for c in entry.cards)


def _state[T](name: str, answer: Answer[T]) -> SourceState:
    return SourceState(name, answer.source, answer.fetched_at, answer.error)


def _window(entries: Iterable[Entry], since: date) -> tuple[Entry, ...]:
    # A cached answer from an earlier day can reach a little further back than today's window.
    return tuple(e for e in entries if e.tournament.held_on >= since)


def _list_counts(entries: Iterable[Entry]) -> Counter[str]:
    return Counter(key for e in entries for key in {card_key(c.name) for c in e.cards})


def _peer_counts(peers: Iterable[PeerList]) -> Counter[str]:
    return Counter(key for p in peers for key in {card_key(name) for name in p.cards})


def _top_spice(entries: Iterable[Entry]) -> dict[str, int]:
    spice: dict[str, int] = {}
    for entry in entries:
        for card in entry.cards:
            key = card_key(card.name)
            spice[key] = max(spice.get(key, 0), card.spice)
    return spice


def _display_names(performing: Iterable[Entry], peers: Iterable[PeerList], deck: Deck) -> dict[str, str]:
    names: dict[str, str] = {}
    for entry in performing:
        for card in entry.cards:
            names.setdefault(card_key(card.name), card.name)
    for peer in peers:
        for name in peer.cards:
            names.setdefault(card_key(name), name)
    for card in deck.cards:
        names.setdefault(card_key(card.name), card.name)
    return names


def _basics(db: CardDb, names: Iterable[str]) -> frozenset[str]:
    return frozenset(
        card_key(info.name)
        for info in db.cards_named(names).values()
        if "Basic" in info.front_type_line.split()
    )


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None
