"""The combos the most-played commanders' lists run, and what a deck has to answer each of them.

An answer is a deck card that removes a piece (removal whose Tagger tags reach the piece's type), counters
one, or stops the combo working: a stax piece, found by its tags or, since Tagger misses some (Null Rod,
Stony Silence), by a phrase in its rules text, and matched against what the combo's steps do.
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum

from commander.answers import AnswerKind, answer_kinds, card_key
from commander.carddb import CardDb, Theme
from commander.metadata import CommanderCombos, MetaCommander
from commander.models import Deck
from commander.spellbook import ComboDetail, ComboPiece

# A combo counts for a commander when this share of its lists hold every named piece.
COMBO_SHARE = 0.2
MAX_COMBOS = 15
_BATTLEFIELD = frozenset({"B", "C"})
_OFF_ZONES = frozenset({"G", "E", "L"})


class Stop(Enum):
    """A way a stax piece stops a combo: its Tagger tags and a rules-text phrase."""

    ACTIVATIONS = (
        "stops activated abilities",
        ("hate-activation", "prevent-activation"),
        r"activated abilities[^.]*can't be activated",
    )
    ENTER_TRIGGERS = (
        "stops enter triggers",
        ("hate-etb", "prevent-trigger"),
        r"(?:entering|enters)[^.]*(?:don't|doesn't) cause abilities to trigger",
    )
    CHEATED_IN = (
        "stops permanents put onto the battlefield",
        ("prevent-etb",),
        r"enters[^.]*without being cast|weren't cast",
    )
    CASTING = (
        "limits casting",
        # Not the wider prevent-cast tag: it also holds Drannith Magistrate and other narrow locks.
        ("rule-of-law", "hate-storm"),
        r"can't cast more than one|can't cast (?:noncreature )?spells this turn",
    )
    OFF_HAND_CASTING = (
        "stops casting from outside the hand",
        ("hate-nonhand-cast", "hate-graveyard-cast", "hate-library-cast"),
        r"from anywhere other than (?:their|your|a player's) hand",
    )
    GRAVEYARD = (
        "graveyard hate",
        ("hate-graveyard", "graveyard-seal", "sweeper-graveyard"),
        r"would be put into [^.]*graveyard[^.]*exile it instead",
    )
    TOKENS = ("token hate", ("hate-token",), r"tokens? (?:can't|don't) (?:be created|enter)")

    def __init__(self, label: str, slugs: tuple[str, ...], phrase: str) -> None:
        self.label = label
        self.slugs = slugs
        self.phrase = re.compile(phrase, re.IGNORECASE)


class AnswerRole(Enum):
    """How an answer meets a combo, in the order a stax deck reaches for them."""

    STAX = "stax"
    REMOVAL = "removal"
    COUNTER = "counter"


@dataclass(slots=True, frozen=True)
class ComboAnswer:
    card: str
    how: str
    role: AnswerRole


@dataclass(slots=True, frozen=True)
class MetaCombo:
    id: str
    url: str
    pieces: tuple[str, ...]
    requirements: tuple[str, ...]
    results: tuple[str, ...]
    # Sum over the top commanders of meta share x the share of their lists that hold every piece.
    exposure: float
    played_by: tuple[tuple[str, float], ...]
    answers: tuple[ComboAnswer, ...]


@dataclass(slots=True, frozen=True)
class LockScope:
    """What an activation lock reaches (one card type, or any permanent) and whether mana abilities escape."""

    type_word: str | None = None
    spares_mana: bool = False


ANY = LockScope()


@dataclass(slots=True, frozen=True)
class _DeckAnswers:
    """What each deck card can do against a combo, worked out once per deck."""

    removal: dict[str, tuple[AnswerKind, ...]]
    stops: dict[str, tuple[tuple[Stop, LockScope], ...]]


def meta_combos(
    deck: Deck, field: Sequence[MetaCommander], combos: Sequence[CommanderCombos], since: date, db: CardDb
) -> tuple[MetaCombo, ...]:
    """The combos common in top commanders' lists since `since`, most seen first, with the deck's answers."""
    stats = {c.stats.name: c for c in field}
    seen: dict[str, ComboDetail] = {}
    shares: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
    for found in combos:
        commander = stats.get(found.commander)
        if commander is None:
            continue
        lists = [e for e in commander.entries if e.cards and e.tournament.held_on >= since]
        if not lists:
            continue
        held = [{card_key(c.name) for c in e.cards} | _commander_keys(found.commander) for e in lists]
        for combo in found.combos:
            pieces = {card_key(p.name) for p in combo.pieces}
            share = sum(1 for cards in held if pieces <= cards) / len(lists)
            if share >= COMBO_SHARE:
                seen.setdefault(combo.id, combo)
                shares[combo.id].append((commander.stats.name, share, commander.stats.meta_share))
    answers = _deck_answers(deck, db)
    ranked = [
        MetaCombo(
            id=combo.id,
            url=combo.url,
            pieces=tuple(p.name for p in combo.pieces),
            requirements=combo.requirements,
            results=combo.results,
            exposure=sum(share * meta for _, share, meta in shares[combo.id]),
            played_by=tuple(sorted(((n, s) for n, s, _ in shares[combo.id]), key=lambda r: -r[1])),
            answers=combo_answers(combo, answers),
        )
        for combo in seen.values()
    ]
    ranked.sort(key=lambda c: (-c.exposure, c.id))
    return tuple(ranked[:MAX_COMBOS])


def combo_answers(combo: ComboDetail, deck: _DeckAnswers) -> tuple[ComboAnswer, ...]:
    """Each deck card's best answer to the combo: stax first, then removal, then counters, by name within."""
    found: dict[str, ComboAnswer] = {}
    for card, stops in sorted(deck.stops.items()):
        for stop, scope in stops:
            if stop_applies(stop, scope, combo):
                found[card] = ComboAnswer(card, stop.label, AnswerRole.STAX)
                break
    for card, kinds in sorted(deck.removal.items()):
        if card in found:
            continue
        answer = _interaction(card, combo, kinds)
        if answer is not None:
            found[card] = answer
    order = list(AnswerRole)
    return tuple(sorted(found.values(), key=lambda a: (order.index(a.role), a.card)))


def stop_applies(stop: Stop, scope: LockScope, combo: ComboDetail) -> bool:  # noqa: PLR0911 — one rule per way to stop
    """Whether a stax piece of this kind (and scope, for activations) stops the combo, read from its steps."""
    steps = combo.description.casefold()
    results = " ".join(combo.results).casefold()
    match stop:
        case Stop.ACTIVATIONS:
            return any(_activated(piece, scope, steps) for piece in combo.pieces)
        case Stop.ENTER_TRIGGERS:
            return "etb" in results or "enters" in steps
        case Stop.CHEATED_IN:
            return re.search(r"(?:return|put)\w*[^.\n]*(?:onto|to) the battlefield", steps) is not None
        case Stop.CASTING:
            return "storm" in results or "cast" in results or len(re.findall(r"\bcast\b", steps)) >= 2  # noqa: PLR2004 — a loop casts at least twice
        case Stop.OFF_HAND_CASTING:
            cast_off = re.search(
                r"\bcast\b[^.\n]*\bfrom (?:your |their |a |the )?(?:graveyard|exile|library)", steps
            )
            return cast_off is not None or (
                any(set(p.zones) & _OFF_ZONES for p in combo.pieces) and "cast" in steps
            )
        case Stop.GRAVEYARD:
            return any("G" in p.zones for p in combo.pieces) or "graveyard" in steps
        case Stop.TOKENS:
            return "token" in results


def _activated(piece: ComboPiece, scope: LockScope, steps: str) -> bool:
    if piece.zones and not set(piece.zones) & _BATTLEFIELD:
        return False
    if scope.type_word and scope.type_word not in piece.type_line.split(" // ", maxsplit=1)[0].split():
        return False
    short = piece.name.split(" // ", maxsplit=1)[0].split(",", maxsplit=1)[0].casefold()
    for line in steps.splitlines():
        if "activate" not in line or short not in line:
            continue
        # A step that adds mana is a mana ability, which locks like Pithing Needle let through.
        if scope.spares_mana and "adding" in line:
            continue
        return True
    return False


def _interaction(card: str, combo: ComboDetail, kinds: tuple[AnswerKind, ...]) -> ComboAnswer | None:
    """Removal reaching a piece on the battlefield, else a counterspell, which meets any piece as cast."""
    for piece in combo.pieces:
        on_field = not piece.zones or bool(set(piece.zones) & _BATTLEFIELD)
        if on_field and any(
            kind.hits(piece.type_line) for kind in kinds if kind is not AnswerKind.COUNTERSPELL
        ):
            return ComboAnswer(card, f"removes {piece.name}", AnswerRole.REMOVAL)
    if AnswerKind.COUNTERSPELL in kinds:
        return ComboAnswer(card, "counters a piece", AnswerRole.COUNTER)
    return None


def _deck_answers(deck: Deck, db: CardDb) -> _DeckAnswers:
    infos = db.cards_named(card.name for card in deck.main_cards)
    by_id = {info.oracle_id: info.name for info in infos.values()}
    kinds = answer_kinds(db, list(by_id))
    removal: dict[str, tuple[AnswerKind, ...]] = {}
    for oracle_id, name in by_id.items():
        found = tuple(kind for kind in AnswerKind if oracle_id in kinds[kind])
        if found:
            removal[name] = found
    tagged = _stop_tags(db, list(by_id))
    stops: dict[str, tuple[tuple[Stop, LockScope], ...]] = {}
    for oracle_id, name in by_id.items():
        text = db.card_text(oracle_id)
        rules = text.text if text else ""
        found_stops: list[tuple[Stop, LockScope]] = []
        for stop in Stop:
            match = stop.phrase.search(rules)
            if match:
                found_stops.append((stop, _scope(rules[match.start() : rules.find(".", match.end()) + 1])))
            elif oracle_id in tagged[stop]:
                found_stops.append((stop, ANY))
        if found_stops:
            stops[name] = tuple(found_stops)
    return _DeckAnswers(removal, stops)


def _scope(sentence: str) -> LockScope:
    """Which permanents an activation lock covers, from the sentence that sets it up."""
    words = sentence.casefold()
    spares_mana = "mana abilities" in words
    if "artifact" in words:
        return LockScope("Artifact", spares_mana)
    if "creature" in words:
        return LockScope("Creature", spares_mana)
    return LockScope(None, spares_mana)


def _stop_tags(db: CardDb, oracle_ids: Sequence[str]) -> dict[Stop, frozenset[str]]:
    ids = db.tag_ids({slug for stop in Stop for slug in stop.slugs})
    return {
        stop: db.matching(Theme(tag_ids=tuple(ids[s] for s in stop.slugs if s in ids)), oracle_ids)
        for stop in Stop
    }


def _commander_keys(name: str) -> set[str]:
    # A list's commander is in every game, though decklists don't always repeat it among the cards.
    return {card_key(part) for part in name.split(" / ")}
