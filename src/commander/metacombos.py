"""The combos the most-played commanders' lists run, and what a deck has to answer each of them.

An answer is a deck card that removes or counters a piece (commander.answers reads what it reaches), or
stops the combo working: a stax piece, found by its tags or, since Tagger misses some (Null Rod, Stony
Silence), by a phrase in its rules text, and matched against what the combo's steps do.
"""

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from enum import Enum

from commander.answers import CardAnswer, Piece, card_answers, card_key, piece_facts
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
        r"enters[^.]*without being cast|weren't cast|can't enter",
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
        # Tagger files Grafdigger's Cage and one-shot exile (Dawnbringer Cleric's one card) here too. Only
        # cards that seal graveyards, exile whole ones, or keep exiling from an activated ability count.
        (
            r"would be put into [^.]*graveyard[^.]*exile"
            r"|exile (?:all graveyards|all cards from all graveyards)"
            r"|exile (?:target|each) (?:player|opponent)'s graveyard"
            r"|\}[^:\n]*:[^\n]*\bexile\b[^.\n]*\bgraveyard"
        ),
    )
    TOKENS = ("token hate", ("hate-token",), r"tokens? (?:can't|don't) (?:be created|enter)")

    def __init__(self, label: str, slugs: tuple[str, ...], phrase: str, requires: str | None = None) -> None:
        self.label = label
        self.slugs = slugs
        self.phrase = re.compile(phrase, re.IGNORECASE)
        # Rules text a card must have for this stop, whatever its tags say.
        self.requires = re.compile(requires, re.IGNORECASE) if requires else None


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
    # What keeps a removal or counter from being sure ("unless they pay {2}"); empty when nothing does.
    catch: str = ""


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
    """What a lock reaches (a card type or any, some zones or all) and whether mana abilities escape."""

    type_word: str | None = None
    spares_mana: bool = False
    # "graveyard" and "library" for Grafdigger's Cage; empty for a lock on every zone.
    zones: frozenset[str] = frozenset()


ANY = LockScope()


@dataclass(slots=True, frozen=True)
class DeckAnswers:
    """What each deck card can do against a combo, worked out once per deck."""

    removal: dict[str, CardAnswer]
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
    facts = piece_facts(db, {(p.name, p.type_line) for combo in seen.values() for p in combo.pieces})
    ranked = [
        MetaCombo(
            id=combo.id,
            url=combo.url,
            pieces=tuple(p.name for p in combo.pieces),
            requirements=combo.requirements,
            results=combo.results,
            exposure=sum(share * meta for _, share, meta in shares[combo.id]),
            played_by=tuple(sorted(((n, s) for n, s, _ in shares[combo.id]), key=lambda r: -r[1])),
            answers=combo_answers(combo, answers, facts),
        )
        for combo in seen.values()
    ]
    ranked.sort(key=lambda c: (-c.exposure, c.id))
    return tuple(ranked[:MAX_COMBOS])


def combo_answers(
    combo: ComboDetail, deck: DeckAnswers, facts: Mapping[str, Piece]
) -> tuple[ComboAnswer, ...]:
    """Each deck card's best answer to the combo: stax first, then removal, then counters, by name within."""
    found: dict[str, ComboAnswer] = {}
    for card, stops in sorted(deck.stops.items()):
        for stop, scope in stops:
            if stop_applies(stop, scope, combo):
                found[card] = ComboAnswer(card, stop.label, AnswerRole.STAX)
                break
    pieces = [(piece, as_cast(combo, piece, facts)) for piece in combo.pieces]
    for card, answer in sorted(deck.removal.items()):
        if card in found:
            continue
        interaction = _interaction(answer, pieces)
        if interaction is not None:
            found[card] = interaction
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
            return any(_enter_trigger(line, scope, combo.pieces) for line in steps.splitlines())
        case Stop.CHEATED_IN:
            return any(_put_onto_battlefield(line, scope, combo.pieces) for line in steps.splitlines())
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


_WHEN_ENTERS = re.compile(r"\bwhen (.+?) enters\b")
# Spellbook words a piece's own enter trigger as "Thassa's Oracle enters the battlefield, causing ...".
_ENTERS_CAUSING = re.compile(r"^(.+?) enters(?: the battlefield)?, (?:causing|triggering)")
_ONTO_BATTLEFIELD = re.compile(r"\b(?:return|put)\w*[^.\n]*(?:onto|to) the battlefield")
_FROM_ZONE = re.compile(
    r"\bfrom (?:your |their |a |an |the |its owner's |each )?(?:opponent's )?(graveyard|library)"
)
_TYPE_HINTS = {"creature": "Creature", "artifact": "Artifact", "treasure": "Artifact", "land": "Land"}


def _enter_trigger(line: str, scope: LockScope, pieces: Sequence[ComboPiece]) -> bool:
    """Whether the step is something triggering as a card enters, of a type the lock reaches."""
    found = _WHEN_ENTERS.search(line) or _ENTERS_CAUSING.search(line)
    if found is None:
        return False
    what = found.group(1)
    # "When it enters" means the piece the step casts.
    named = _named_pieces(line if what == "it" else what, pieces)
    return _of_type(scope.type_word, what, named)


def _put_onto_battlefield(line: str, scope: LockScope, pieces: Sequence[ComboPiece]) -> bool:
    """Whether the step puts a card onto the battlefield from a zone, and of a type, the lock reaches."""
    if _ONTO_BATTLEFIELD.search(line) is None:
        return False
    if scope.zones:
        zone = _FROM_ZONE.search(line)
        if zone is None or zone.group(1) not in scope.zones:
            return False
    return _of_type(scope.type_word, line, _named_pieces(line, pieces))


def _named_pieces(text: str, pieces: Sequence[ComboPiece]) -> list[ComboPiece]:
    return [p for p in pieces if _short(p.name) in text]


def _of_type(type_word: str | None, text: str, named: Sequence[ComboPiece]) -> bool:
    """Whether what enters is of the lock's type: by the pieces named, else by a type word, else unknown."""
    if type_word is None:
        return True
    if named:
        return any(type_word in p.type_line.split(" // ", maxsplit=1)[0].split() for p in named)
    hinted = {kind for word, kind in _TYPE_HINTS.items() if word in text}
    return not hinted or type_word in hinted


def _short(name: str) -> str:
    return name.split(" // ", maxsplit=1)[0].split(",", maxsplit=1)[0].casefold()


def _activated(piece: ComboPiece, scope: LockScope, steps: str) -> bool:
    if piece.zones and not set(piece.zones) & _BATTLEFIELD:
        return False
    if scope.type_word and scope.type_word not in piece.type_line.split(" // ", maxsplit=1)[0].split():
        return False
    short = _short(piece.name)
    for line in steps.splitlines():
        if "activate" not in line or short not in line:
            continue
        # A step that adds mana is a mana ability, which locks like Pithing Needle let through.
        if scope.spares_mana and "adding" in line:
            continue
        return True
    return False


def as_cast(combo: ComboDetail, piece: ComboPiece, facts: Mapping[str, Piece]) -> Piece:
    """The piece's facts, marked cast for free when it costs nothing (no X) or the steps cast it for {0}."""
    fact = facts.get(piece.name.casefold(), Piece(piece.name, piece.type_line))
    free = re.search(
        rf"\bcast {re.escape(_short(piece.name))}[^.\n]*by paying \{{0\}}", combo.description.casefold()
    )
    if (fact.mana_value == 0 and not fact.x_cost) or free:
        return replace(fact, cast_free=True)
    return fact


def _interaction(answer: CardAnswer, pieces: Sequence[tuple[ComboPiece, Piece]]) -> ComboAnswer | None:
    """Removal reaching pieces on the battlefield, else a counterspell reaching pieces as they are cast."""
    on_field = [(p, fact) for p, fact in pieces if not p.zones or set(p.zones) & _BATTLEFIELD]
    removed = [(p.name, catch) for p, fact in on_field if (catch := answer.removal(fact)) is not None]
    if removed:
        return _combo_answer(answer.name, "removes", removed, AnswerRole.REMOVAL)
    countered = [(p.name, catch) for p, fact in pieces if (catch := answer.counter(fact)) is not None]
    if countered:
        return _combo_answer(answer.name, "counters", countered, AnswerRole.COUNTER)
    return None


def _combo_answer(card: str, verb: str, hits: Sequence[tuple[str, str]], role: AnswerRole) -> ComboAnswer:
    """Every piece the card reaches, "A, B or C", with the smallest catch among them."""
    names = [name for name, _ in hits]
    listed = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"
    return ComboAnswer(card, f"{verb} {listed}", role, min((catch for _, catch in hits), key=len))


def _deck_answers(deck: Deck, db: CardDb) -> DeckAnswers:
    infos = db.cards_named(card.name for card in deck.main_cards)
    by_id = {info.oracle_id: info.name for info in infos.values()}
    one_shots = {
        info.oracle_id
        for info in infos.values()
        if {"Instant", "Sorcery"} & set(info.type_line.split(" // ", maxsplit=1)[0].split())
    }
    removal = {answer.name: answer for answer in card_answers(db, by_id).values()}
    tagged = _stop_tags(db, list(by_id))
    stops: dict[str, tuple[tuple[Stop, LockScope], ...]] = {}
    for oracle_id, name in by_id.items():
        text = db.card_text(oracle_id)
        found_stops = card_stops(
            text.text if text else "",
            {stop for stop in Stop if oracle_id in tagged[stop]},
            one_shot=oracle_id in one_shots,
        )
        if found_stops:
            stops[name] = found_stops
    return DeckAnswers(removal, stops)


def card_stops(
    rules: str, tagged: set[Stop], *, one_shot: bool = False
) -> tuple[tuple[Stop, LockScope], ...]:
    """How a card stops combos, from its rules text or else its tags, with the scope its text sets.

    An instant or sorcery (`one_shot`) locks only by its text, as Silence does: Tagger files one-shot answers
    such as Mindbreak Trap under storm hate too.
    """
    found: list[tuple[Stop, LockScope]] = []
    for stop in Stop:
        if stop.requires is not None and not stop.requires.search(rules):
            continue
        match = stop.phrase.search(rules)
        if match:
            # The whole sentence: what a lock covers often comes before its phrase ("Creature cards in ...").
            start = max(rules.rfind(".", 0, match.start()), rules.rfind("\n", 0, match.start())) + 1
            found.append((stop, _scope(rules[start : rules.find(".", match.end()) + 1])))
        elif stop in tagged and not one_shot:
            found.append((stop, ANY))
    return tuple(found)


def _scope(sentence: str) -> LockScope:
    """Which permanents a lock covers, from the sentence that sets it up."""
    words = sentence.casefold()
    spares_mana = "mana abilities" in words
    zones = frozenset(
        zone for zone, stem in (("graveyard", "graveyard"), ("library", "librar")) if stem in words
    )
    if "artifact" in words:
        return LockScope("Artifact", spares_mana, zones)
    if "creature" in words:
        return LockScope("Creature", spares_mana, zones)
    return LockScope(None, spares_mana, zones)


def _stop_tags(db: CardDb, oracle_ids: Sequence[str]) -> dict[Stop, frozenset[str]]:
    ids = db.tag_ids({slug for stop in Stop for slug in stop.slugs})
    return {
        stop: db.matching(Theme(tag_ids=tuple(ids[s] for s in stop.slugs if s in ids)), oracle_ids)
        for stop in Stop
    }


def _commander_keys(name: str) -> set[str]:
    # A list's commander is in every game, though decklists don't always repeat it among the cards.
    return {card_key(part) for part in name.split(" / ")}
