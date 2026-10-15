"""Reading other players' lists against a deck: what performing lists play that the deck doesn't, their spicy
picks, the deck's own outliers, and which cards the most-played commanders answer the deck's key pieces with.
"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from commander.archidekt import PeerDeck
from commander.board import MAX_CUT_OPTIONS, ReplaceOption
from commander.carddb import CardDb, CardInfo, Theme
from commander.deckcache import Answer, AnswerSource
from commander.deckprofile import Profile
from commander.edhtop16 import CommanderStats, Entry
from commander.metadata import MetaCommander, MetaData, PeerList
from commander.models import COMMANDER_CATEGORY, Deck

# Fewer top-cut lists than this, and every list in the window stands in, marked thin.
MIN_PERFORMING = 5
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
MAX_ROWS = 15


class Basis(Enum):
    TOP_CUT = "top-cut lists"
    ALL = "every list in the window"


class AnswerKind(Enum):
    """What a meta card does to a key piece, by the Tagger tags that say so and the piece types it reaches."""

    # Widest reach first, so a card's first kind is the broadest thing it does.
    COUNTERSPELL = ("counterspell", ("counterspell",), "any")
    SWEEPER = ("sweeper", ("sweeper",), "permanent")
    PERMANENT_REMOVAL = (
        "permanent removal",
        ("removal-nonland", "removal-permanent", "removal-bounce"),
        "permanent",
    )
    CREATURE_REMOVAL = ("creature removal", ("removal-creature",), "Creature")
    ARTIFACT_REMOVAL = ("artifact removal", ("removal-artifact",), "Artifact")
    ENCHANTMENT_REMOVAL = ("enchantment removal", ("removal-enchantment",), "Enchantment")

    def __init__(self, label: str, slugs: tuple[str, ...], reaches: str) -> None:
        self.label = label
        self.slugs = slugs
        # A card type word, "permanent" for any nonland permanent, or "any" for every piece.
        self.reaches = reaches

    def hits(self, info: CardInfo) -> bool:
        words = info.front_type_line.split()
        if self.reaches == "any":
            return True
        if self.reaches == "permanent":
            return not ({"Instant", "Sorcery"} & set(words)) and "Land" not in words
        return self.reaches in words


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
    # On the deck's Sideboard or Maybeboard (for missing cards) or kept by the profile (for the deck's own).
    flagged: bool


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
    field: tuple[CommanderStats, ...]
    peers: tuple[PeerDeck, ...]
    peers_skipped: int
    key_pieces: tuple[str, ...]
    # True when the profile names key pieces; otherwise only the commander counts as one.
    keys_from_profile: bool
    sources: tuple[SourceState, ...]


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


def card_key(name: str) -> str:
    """The name lists are compared by: the front face, casefolded, as sites spell two-faced cards apart."""
    return name.casefold().split(" // ")[0]


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
    pieces, from_profile = key_pieces(deck, profile)
    names = _display_names(performing, peer_lists, deck)
    return MetaReport(
        deck=deck.short_name,
        commander=data.commander,
        since=data.since,
        sample=sample,
        missing=missing_staples(deck, performing, peer_lists, names, db),
        spicy=spicy_picks(deck, performing),
        outliers=outliers(deck, performing, peer_lists, profile, db),
        threats=threats(pieces, field, db),
        field=tuple(c.stats for c in (data.field.data or ())),
        peers=tuple(p.deck for p in peer_lists),
        peers_skipped=data.peers.data.skipped if data.peers.data else 0,
        key_pieces=tuple(sorted(pieces)),
        keys_from_profile=from_profile,
        sources=(
            _state("EDHTop16 lists", data.lists),
            _state("EDHTop16 meta", data.field),
            _state("Archidekt decks", data.peers),
        ),
    )


def board_ideas(report: MetaReport) -> tuple[dict[str, str], list[ReplaceOption]]:
    """The staples the deck is missing as board ideas with their reasons, and its outliers (bar kept ones) as
    cards to replace."""
    lists = f"{report.deck} lists" if report.sample.basis is Basis.ALL else f"top-cut {report.deck} lists"
    ideas: dict[str, str] = {}
    for card in report.missing:
        parts: list[str] = []
        if report.sample.performing:
            parts.append(
                f"in {card.lists} of {report.sample.performing} {lists} on EDHTop16 since {report.since}"
            )
        if report.peers:
            parts.append(f"{card.peer_lists} of {len(report.peers)} recent Archidekt lists")
        ideas[card.name] = "; ".join(parts)
    cuts = [
        ReplaceOption(card.name, f"in {card.lists} of {report.sample.performing} {lists}")
        for card in report.outliers
        if not card.flagged
    ]
    return ideas, cuts[:MAX_CUT_OPTIONS]


def performing_lists(entries: Sequence[Entry]) -> tuple[list[Entry], Basis]:
    """The lists to learn from: top-cut ones, or every list best placing first when too few made a cut."""
    with_lists = [e for e in entries if e.cards]
    top = [e for e in with_lists if e.made_top_cut]
    if len(top) >= MIN_PERFORMING:
        return sorted(top, key=lambda e: e.placing), Basis.TOP_CUT
    return sorted(with_lists, key=lambda e: e.placing), Basis.ALL


def key_pieces(deck: Deck, profile: Profile) -> tuple[frozenset[str], bool]:
    """The deck's commanders plus the cards its profile keeps, by name or category."""
    keep_names = {card_key(name) for name in profile.keep_cards}
    keep_categories = {category.casefold() for category in profile.keep_categories}
    pieces: set[str] = set()
    for card in deck.main_cards:
        categories = {category.casefold() for category in card.categories}
        if (
            COMMANDER_CATEGORY.casefold() in categories
            or categories & keep_categories
            or card_key(card.name) in keep_names
        ):
            pieces.add(card.name)
    return frozenset(pieces), bool(keep_names or keep_categories)


def missing_staples(
    deck: Deck,
    performing: Sequence[Entry],
    peers: Sequence[PeerList],
    names: Mapping[str, str],
    db: CardDb,
) -> tuple[CardShare, ...]:
    in_deck = {card_key(card.name) for card in deck.main_cards}
    aside = {card_key(card.name) for card in deck.aside_cards}
    counts = _list_counts(performing)
    peer_counts = Counter(key for p in peers for key in {card_key(name) for name in p.cards})
    spice = _top_spice(performing)
    basics = _basics(db, names.values())
    rows: list[CardShare] = []
    for key in set(counts) | set(peer_counts):
        if key in in_deck or key in basics:
            continue
        share = counts[key] / len(performing) if performing else 0.0
        peer_share = peer_counts[key] / len(peers) if peers else 0.0
        staple = counts[key] >= MIN_STAPLE_LISTS and share >= STAPLE_SHARE
        peer_staple = len(peers) >= MIN_PEER_LISTS and peer_share >= PEER_STAPLE_SHARE
        if staple or peer_staple:
            rows.append(
                CardShare(
                    names[key], counts[key], share, peer_counts[key], peer_share, spice.get(key), key in aside
                )
            )
    rows.sort(key=lambda r: (-r.share, -r.peer_share, r.name))
    return tuple(rows[:MAX_ROWS])


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
    peer_counts = Counter(key for p in peers for key in {card_key(name) for name in p.cards})
    keep_names = {card_key(name) for name in profile.keep_cards}
    keep_categories = {category.casefold() for category in profile.keep_categories}
    basics = _basics(db, (card.name for card in deck.main_cards))
    rows: list[CardShare] = []
    for card in deck.main_cards:
        key = card_key(card.name)
        if COMMANDER_CATEGORY in card.categories or key in basics:
            continue
        share = counts[key] / len(performing)
        if share >= OUTLIER_SHARE:
            continue
        kept = key in keep_names or bool({c.casefold() for c in card.categories} & keep_categories)
        peer_share = peer_counts[key] / len(peers) if peers else 0.0
        rows.append(CardShare(card.name, counts[key], share, peer_counts[key], peer_share, None, kept))
    rows.sort(key=lambda r: (r.share, r.peer_share, r.name))
    return tuple(rows)


def threats(pieces: Iterable[str], field: Sequence[MetaCommander], db: CardDb) -> tuple[Threat, ...]:
    """Cards the top commanders' lists commonly play that answer at least one key piece."""
    piece_info = list(db.cards_named(pieces).values())
    shares: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
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
    candidates = {key for key, rows in shares.items() if max(share for _, share, _ in rows) >= THREAT_SHARE}
    infos = {card_key(info.name): info for info in db.cards_named(names[key] for key in candidates).values()}
    kinds = _answer_kinds(db, [info.oracle_id for info in infos.values()])
    found: list[Threat] = []
    for key in candidates:
        info = infos.get(key)
        if info is None:
            continue
        answer = tuple(kind for kind in AnswerKind if info.oracle_id in kinds[kind])
        hit = sorted({p.name for kind in answer for p in piece_info if kind.hits(p)})
        if not hit:
            continue
        rows = shares[key]
        found.append(
            Threat(
                name=info.name,
                kinds=answer,
                hits=tuple(hit),
                exposure=sum(share * meta for _, share, meta in rows),
                played_by=tuple(
                    sorted(((n, s) for n, s, _ in rows if s >= THREAT_SHARE), key=lambda r: -r[1])
                ),
            )
        )
    found.sort(key=lambda t: (-t.exposure, t.name))
    return tuple(found)


def check_card(name: str, deck: Deck, data: MetaData) -> CardCheck:
    """How the commander's lists, Archidekt lists and the meta's lists use one card."""
    key = card_key(name)
    lists = [e for e in _window(data.lists.data.entries if data.lists.data else (), data.since) if e.cards]
    performing, _ = performing_lists(lists)
    having = [e for e in lists if key in {card_key(c.name) for c in e.cards}]
    lacking = [e for e in lists if e not in having]
    peers = data.peers.data.decks if data.peers.data else ()
    meta_lists = [e for c in (data.field.data or ()) for e in _window(c.entries, data.since) if e.cards]
    spice = [c.spice for e in lists for c in e.cards if card_key(c.name) == key]
    return CardCheck(
        name=name,
        in_deck=key in {card_key(card.name) for card in deck.main_cards},
        performing=len(performing),
        performing_with=sum(1 for e in performing if e in having),
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
        meta_lists_with=sum(1 for e in meta_lists if key in {card_key(c.name) for c in e.cards}),
    )


def _state[T](name: str, answer: Answer[T]) -> SourceState:
    return SourceState(name, answer.source, answer.fetched_at, answer.error)


def _window(entries: Iterable[Entry], since: date) -> tuple[Entry, ...]:
    # A cached answer from an earlier day can reach a little further back than today's window.
    return tuple(e for e in entries if e.tournament.held_on >= since)


def _list_counts(entries: Iterable[Entry]) -> Counter[str]:
    return Counter(key for e in entries for key in {card_key(c.name) for c in e.cards})


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


def _answer_kinds(db: CardDb, oracle_ids: Sequence[str]) -> dict[AnswerKind, frozenset[str]]:
    slugs = {slug for kind in AnswerKind for slug in kind.slugs}
    ids = db.tag_ids(slugs)
    return {
        kind: db.matching(Theme(tag_ids=tuple(ids[s] for s in kind.slugs if s in ids)), oracle_ids)
        for kind in AnswerKind
    }


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None
