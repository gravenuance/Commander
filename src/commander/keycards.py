"""The cards a deck's plan stands on, found from the deck itself and each with why (ADR 0017).

`cmdr meta` and the app check the meta's answers against them. Glarb's Protean Hulk is found by its cost,
far above the rest of a cEDH list; Magda's Gogo, Mysterious Mime because other Magda players file it
under their combo pieces.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import fmean, pstdev

from commander.answers import card_key
from commander.carddb import CardDb
from commander.deckprofile import Profile
from commander.metadata import PeerList
from commander.models import COMMANDER_CATEGORY, Deck, plan_category
from commander.spellbook import ComboDetail

# Recent decks with the commander that must file a card under a plan category for it to count.
MIN_PLANNED_PEERS = 2
# A card this many standard deviations above the deck's mean mana value, and at least MIN_COSTLY, is there
# to be cheated in or to win: tuned on the owner's 11 decks (2026-10-16).
COSTLY_SPREAD = 2.0
MIN_COSTLY = 5
# Cards cast for less than their mana value (pitch spells, split cards) only look costly.
CHEAPER_TAGS = ("alternative-cost", "cheaper-than-mv", "manaless-value")
MAX_COMBO_NAMES = 2


@dataclass(slots=True, frozen=True)
class KeyCard:
    name: str
    reasons: tuple[str, ...]


def key_cards(
    deck: Deck,
    profile: Profile,
    own_combos: Sequence[ComboDetail],
    peers: Sequence[PeerList],
    db: CardDb,
) -> tuple[KeyCard, ...]:
    """The commander first, then the cards with the most reasons; the profile's ignored ones left out."""
    filed = _categories_by_name(deck, _ignored(profile))
    infos = db.cards_named(filed)
    reasons: dict[str, list[str]] = {name: [] for name in filed}
    planned = Counter(card_key(name) for peer in peers for name in set(peer.planned))
    costly = _costly(deck, db)
    for name, categories in filed.items():
        why = reasons[name]
        info = infos.get(name.casefold())
        if info is not None and info.is_land:
            continue
        key = card_key(name)
        if COMMANDER_CATEGORY in categories:
            why.append("your commander")
        combos = [c for c in own_combos if key in {card_key(p.name) for p in c.pieces}]
        if combos:
            why.append(_combo_reason(name, combos))
        own_plan = [category for category in categories if plan_category(category)]
        if own_plan:
            why.append(f"you file it under {own_plan[0]}")
        if planned[key] >= MIN_PLANNED_PEERS:
            why.append(f"{planned[key]} of {len(peers)} recent decks file it under a combo or win category")
        if name in costly:
            why.append(costly[name])
        if profile.keeps(name, categories):
            why.append("kept by your profile")
    found = [KeyCard(name, tuple(why)) for name, why in reasons.items() if why]
    found.sort(key=lambda k: ("your commander" not in k.reasons, -len(k.reasons), k.name.casefold()))
    return tuple(found)


def _categories_by_name(deck: Deck, ignored: frozenset[str]) -> dict[str, tuple[str, ...]]:
    """The deck's cards not ignored, each once by name, with the categories of all its entries."""
    filed: dict[str, list[str]] = {}
    for card in deck.main_cards:
        if card.name.casefold() not in ignored:
            filed.setdefault(card.name, []).extend(card.categories)
    return {name: tuple(dict.fromkeys(categories)) for name, categories in filed.items()}


def _ignored(profile: Profile) -> frozenset[str]:
    return frozenset(name.casefold() for name in profile.ignore_keys)


def _combo_reason(name: str, combos: Sequence[ComboDetail]) -> str:
    others = sorted({p.name for c in combos for p in c.pieces if card_key(p.name) != card_key(name)})
    # Names hold commas, so they are joined with "+".
    hidden = len(others) - MAX_COMBO_NAMES
    shown = " + ".join(others[:MAX_COMBO_NAMES]) + (f" + {hidden} more" if hidden > 0 else "")
    count = "a combo" if len(combos) == 1 else f"{len(combos)} combos"
    return f"in {count} with {shown}" if shown else f"in {count}"


def _costly(deck: Deck, db: CardDb) -> dict[str, str]:
    """Nonland cards other than the commander that cost far more than the rest, by name, with why."""
    infos = db.cards_named(card.name for card in deck.main_cards if COMMANDER_CATEGORY not in card.categories)
    spells = [info for info in infos.values() if not info.is_land]
    if len(spells) < 2:  # noqa: PLR2004 — a spread needs two
        return {}
    costs = [info.cmc for info in spells]
    mean = fmean(costs)
    line = max(MIN_COSTLY, mean + COSTLY_SPREAD * pstdev(costs))
    over = [info for info in spells if info.cmc >= line]
    cheaper = _with_tags(db, [info.oracle_id for info in over], CHEAPER_TAGS)
    return {
        info.name: f"costs {info.cmc:g}, far above the deck's {mean:.1f} average"
        for info in over
        if info.oracle_id not in cheaper
    }


def _with_tags(db: CardDb, oracle_ids: Sequence[str], slugs: Sequence[str]) -> frozenset[str]:
    lineage = db.tag_lineage(oracle_ids)
    wanted = set(slugs)
    return frozenset(oracle_id for oracle_id, tags in lineage.items() if tags & wanted)
