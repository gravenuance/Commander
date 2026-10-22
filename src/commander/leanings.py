"""What a deck plays far more of than recent decks with its commander, by Scryfall Tagger tag (ADR 0016).

Arabella decks mostly go wide with tokens; a list that adds untappers and extra combats leans into combat.
A leaning is the owner's choice, so replacements keep it (`targets`), and `cmdr profile` and the app show it.
"""

from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from statistics import fmean

from commander.answers import card_key
from commander.carddb import CardDb, ResolvedCard
from commander.meta import MIN_PEER_LISTS
from commander.metadata import PeerList
from commander.models import COMMANDER_CATEGORY

# Tuned on the owner's 11 decks against 10 recent decks each (2026-10-16).
MIN_CARDS = 3
# At least this many cards more than the peer decks' mean, and this many times it.
MIN_GAP = 2.0
MIN_RATIO = 2.0
# Fewer than this share of the peer decks play as many: the list stands out, not the commander's decks.
MAX_PEERS_AS_MANY = 0.2
# Narrower tags say little; wider ones (triggered-ability) say nothing.
MIN_TAG_CARDS = 8
MAX_TAG_CARDS = 3000
MAX_LEANINGS = 6
# Tagger's trivia (names like alliteration, vanilla flavours, one-of-a-kind type lines or mana costs) and
# printed cycles carry no play.
TRIVIA_ROOTS = ("card-names", "flavors-of-vanilla", "unique-mana-cost", "unique-type-line")
CYCLE_PREFIX = "cycle-"
# A wider leaning whose cards are mostly a narrower one's folds into it: reanimate into reanimate-creature.
FOLD_SHARE = 0.8


@dataclass(slots=True, frozen=True)
class Leaning:
    tag: str
    # The deck's nonland cards with the tag, by name.
    cards: tuple[str, ...]
    # How many the peer decks play on average, and how many peer decks were read.
    peer_mean: float
    peers: int


def find_leanings(
    main: Sequence[ResolvedCard], peers: Sequence[PeerList], db: CardDb, *, ignore: Collection[str] = ()
) -> tuple[Leaning, ...]:
    """The tags the deck's nonland cards hold far more of than the peer decks', the widest gap first.

    Commanders and lands count on neither side. ignore holds tag slugs never to report (casefolded).
    """
    if len(peers) < MIN_PEER_LISTS:
        return ()
    commanders = {card_key(r.card.name) for r in main if COMMANDER_CATEGORY in r.card.categories}
    # A card filed as several entries (printings) is one card, as it is on the peer side.
    own = list(
        {
            r.card.name: r
            for r in main
            if r.info is not None and not r.info.is_land and card_key(r.card.name) not in commanders
        }.values()
    )
    lineage = db.tag_lineage(r.info.oracle_id for r in own if r.info is not None)
    held: dict[str, list[str]] = {}
    for r in own:
        if r.info is not None:
            for slug in lineage.get(r.info.oracle_id, ()):
                held.setdefault(slug, []).append(r.card.name)
    theirs = _peer_counts(peers, commanders, db)
    skip = {slug for root in TRIVIA_ROOTS for slug in db.tag_subtree(root)}
    found: list[Leaning] = []
    for slug, cards in held.items():
        count = len(cards)
        if count < MIN_CARDS or slug in skip or slug.startswith(CYCLE_PREFIX):
            continue
        counts = [c[slug] for c in theirs]
        mean = fmean(counts)
        as_many = sum(1 for c in counts if c >= count) / len(counts)
        if count - mean >= MIN_GAP and count >= MIN_RATIO * mean and as_many < MAX_PEERS_AS_MANY:
            found.append(Leaning(slug, tuple(sorted(cards, key=str.casefold)), mean, len(peers)))
    sizes = db.tag_sizes(leaning.tag for leaning in found)
    found = [f for f in found if MIN_TAG_CARDS <= sizes.get(f.tag, 0) <= MAX_TAG_CARDS]
    # Ignored leanings still fold their wider tags away, or ignoring gives-flying would surface gives-evasion.
    kept = [f for f in found if not _folds(f, found, sizes) and f.tag not in ignore]
    kept.sort(key=lambda f: (f.peer_mean - len(f.cards), f.tag))
    return tuple(kept[:MAX_LEANINGS])


def _peer_counts(peers: Sequence[PeerList], commanders: Collection[str], db: CardDb) -> list[Counter[str]]:
    """Per peer deck, how many of its nonland cards hold each tag, sub-tags counting for their parents."""
    names = {name for p in peers for name in p.cards if card_key(name) not in commanders}
    infos = db.cards_named(names)
    lineage = db.tag_lineage(i.oracle_id for i in infos.values() if not i.is_land)
    found: list[Counter[str]] = []
    for peer in peers:
        counts: Counter[str] = Counter()
        for name in {n.casefold() for n in peer.cards}:
            info = infos.get(name)
            if info is not None and not info.is_land and card_key(name) not in commanders:
                counts.update(lineage.get(info.oracle_id, ()))
        found.append(counts)
    return found


def _folds(leaning: Leaning, others: Sequence[Leaning], sizes: dict[str, int]) -> bool:
    """Whether a narrower leaning holds most of this one's cards and nothing else, so it says it better."""
    cards = set(leaning.cards)
    for other in others:
        if other is leaning:
            continue
        theirs = set(other.cards)
        if not theirs <= cards or len(theirs) < FOLD_SHARE * len(cards):
            continue
        narrower = (sizes.get(other.tag, 0), other.tag) < (sizes.get(leaning.tag, 0), leaning.tag)
        if theirs != cards or narrower:
            return True
    return False


def leaning_json(leaning: Leaning) -> dict[str, object]:
    return {
        "tag": leaning.tag,
        "cards": list(leaning.cards),
        "peerMean": round(leaning.peer_mean, 1),
        "peers": leaning.peers,
    }


def leaning_text(leaning: Leaning) -> str:
    """One line: 'untapper-creature: 6 cards, recent decks 2.3 on average'."""
    return f"{leaning.tag}: {len(leaning.cards)} cards, recent decks {leaning.peer_mean:.1f} on average"
