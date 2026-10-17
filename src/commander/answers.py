"""What a card does to other cards: shared by the meta threat and combo reports.

Tagger tags say whether a card is removal or a counterspell; its rules text says what it reaches. Each mode
("deals 3 damage to target creature", "counter target blue spell") becomes a Mode, checked against a piece's
type, colours, toughness and mana value. A card whose text the reader can't follow keeps its tags' reach.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum

from commander.carddb import FACE_SEPARATOR, CardDb, Theme, front_name, front_type


def card_key(name: str) -> str:
    """The name lists are compared by: the front face, casefolded, as sites spell two-faced cards apart."""
    return front_name(name.casefold())


class AnswerKind(Enum):
    """What a meta card does to a key piece, by the Tagger tags that say so and the piece types it reaches."""

    # Widest reach first, so a card's first kind is the broadest thing it does.
    COUNTERSPELL = ("counterspell", ("counterspell",), "any")
    # Most sweepers clear creatures; ones that reach other permanents carry a removal tag as well.
    SWEEPER = ("sweeper", ("sweeper",), "Creature")
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

    def hits(self, type_line: str) -> bool:
        """Whether this kind of answer reaches a card of this type line (front face of a two-faced card)."""
        words = front_type(type_line).split()
        if self.reaches == "any":
            return True
        if self.reaches == "permanent":
            return not ({"Instant", "Sorcery"} & set(words)) and "Land" not in words
        return self.reaches in words


def answer_kinds(db: CardDb, oracle_ids: Sequence[str]) -> dict[AnswerKind, frozenset[str]]:
    slugs = {slug for kind in AnswerKind for slug in kind.slugs}
    ids = db.tag_ids(slugs)
    return {
        kind: db.matching(Theme(tag_ids=tuple(ids[s] for s in kind.slugs if s in ids)), oracle_ids)
        for kind in AnswerKind
    }


_PERMANENT_TYPES = frozenset({"Creature", "Artifact", "Enchantment", "Planeswalker", "Land", "Battle"})
_TYPE_WORDS = {word.casefold(): word for word in (*_PERMANENT_TYPES, "Instant", "Sorcery")}
_COLOR_WORDS = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G"}
# Damage reaches these; "any target" also means players, which no combo piece is.
_DAMAGEABLE = frozenset({"Creature", "Planeswalker", "Battle"})


@dataclass(slots=True, frozen=True)
class Piece:
    """A card an answer is aimed at, with the facts that decide whether the answer reaches it."""

    name: str
    type_line: str
    # None for facts the card data doesn't have (a card too new, or "*" toughness); they rule nothing out.
    colors: str | None = None
    power: int | None = None
    toughness: int | None = None
    flying: bool = False
    mana_value: float | None = None
    # Cast without spending mana in the combo, which is all some counters (Vexing Bauble) can meet.
    cast_free: bool = False
    # Copies itself when cast, so a counter has to meet every copy (Flusterstorm, Mindbreak Trap).
    storm: bool = False
    # An {X} cost: its mana value is 0, but casting it still takes mana.
    x_cost: bool = False

    @property
    def types(self) -> frozenset[str]:
        return frozenset(front_type(self.type_line).split())

    @property
    def is_permanent(self) -> bool:
        return bool(self.types & _PERMANENT_TYPES)


class Effect(Enum):
    # Destroy, exile, bounce, shuffle or tuck: whatever the piece's toughness.
    REMOVE = "remove"
    DAMAGE = "damage"
    SHRINK = "toughness loss"
    COUNTER = "counter"


@dataclass(slots=True, frozen=True)
class Mode:
    """One thing a card does to a target, with every limit its rules text puts on the target."""

    effect: Effect
    # Type words the target names; "Permanent" is any permanent and "Spell" any spell; empty is either.
    types: frozenset[str] = frozenset()
    excluded_types: frozenset[str] = frozenset()
    colors: str = ""
    excluded_colors: str = ""
    # Damage dealt or toughness lost; None for X, which can be as large as needed.
    amount: int | None = None
    spares_flying: bool = False
    mana_value: tuple[str, int] | None = None
    max_power_or_toughness: int | None = None
    free_only: bool = False
    # Meets every copy of a storm spell: the card has storm itself, or takes any number of targets.
    every_copy: bool = False
    # What keeps it from being a sure answer ("unless they pay {2}"); empty when nothing does.
    catch: str = ""

    def reaches(self, piece: Piece) -> bool:
        if not self._colors_fit(piece) or not self._mana_value_fits(piece):
            return False
        return self._counters(piece) if self.effect is Effect.COUNTER else self._removes(piece)

    def _mana_value_fits(self, piece: Piece) -> bool:
        if self.mana_value is None or piece.mana_value is None:
            return True
        op, value = self.mana_value
        return _compare(piece.mana_value, op, value)

    def _removes(self, piece: Piece) -> bool:
        if not piece.is_permanent or not self._types_fit(piece):
            return False
        if self.effect is Effect.REMOVE:
            return True
        if self.spares_flying and piece.flying:
            return False
        if "Creature" not in piece.types or self.amount is None or piece.toughness is None:
            return True
        return piece.toughness <= self.amount

    def _counters(self, piece: Piece) -> bool:
        if "Land" in piece.types or not self._types_fit(piece):
            return False
        if self.free_only and not piece.cast_free:
            return False
        if piece.storm and not self.every_copy:
            return False
        limit = self.max_power_or_toughness
        stats = [s for s in (piece.power, piece.toughness) if s is not None]
        return limit is None or not stats or min(stats) <= limit

    def _types_fit(self, piece: Piece) -> bool:
        if self.excluded_types & piece.types:
            return False
        named = self.types - {"Permanent", "Spell"}
        if named:
            return bool(named & piece.types)
        return "Permanent" not in self.types or piece.is_permanent

    def _colors_fit(self, piece: Piece) -> bool:
        if piece.colors is None:
            return True
        if self.colors and not set(self.colors) & set(piece.colors):
            return False
        return not set(self.excluded_colors) & set(piece.colors)


@dataclass(slots=True, frozen=True)
class CardAnswer:
    """A removal or counter card: its tag kinds and, when its text could be read, what each mode reaches."""

    name: str
    kinds: tuple[AnswerKind, ...]
    # None when the reader found nothing it follows (an edict, say); the tags' reach stands in.
    modes: tuple[Mode, ...] | None

    def removal(self, piece: Piece) -> str | None:
        """How the card removes the piece: "" outright, its catch when it has one, None when it can't."""
        if self.modes is None:
            reached = any(k.hits(piece.type_line) for k in self.kinds if k is not AnswerKind.COUNTERSPELL)
            return "" if reached else None
        return _surest(m.catch for m in self.modes if m.effect is not Effect.COUNTER and m.reaches(piece))

    def counter(self, piece: Piece) -> str | None:
        """How the card counters the piece, as `removal` says."""
        if self.modes is None:
            reached = AnswerKind.COUNTERSPELL in self.kinds and "Land" not in piece.types and not piece.storm
            return "" if reached else None
        return _surest(m.catch for m in self.modes if m.effect is Effect.COUNTER and m.reaches(piece))

    def removes(self, piece: Piece) -> bool:
        return self.removal(piece) is not None

    def counters(self, piece: Piece) -> bool:
        return self.counter(piece) is not None


def _surest(catches: Iterable[str]) -> str | None:
    """The smallest catch among the modes that reach a piece ("" for none), or None when none reaches it."""
    found = list(catches)
    return min(found, key=len) if found else None


def card_answers(db: CardDb, cards: Mapping[str, str]) -> dict[str, CardAnswer]:
    """The removal and counter cards among `cards` (oracle id to name), keyed by oracle id."""
    kinds = answer_kinds(db, list(cards))
    tagged = {
        oracle_id: tuple(kind for kind in AnswerKind if oracle_id in kinds[kind]) for oracle_id in cards
    }
    texts = db.card_texts(oracle_id for oracle_id, found in tagged.items() if found)
    return {
        oracle_id: CardAnswer(
            cards[oracle_id], found, read_modes(texts[oracle_id].text) if oracle_id in texts else None
        )
        for oracle_id, found in tagged.items()
        if found
    }


def piece_facts(db: CardDb, pieces: Iterable[tuple[str, str]]) -> dict[str, Piece]:
    """Facts for each (name, type line) piece by casefolded name; an unknown card keeps only its type line."""
    wanted = list(pieces)
    infos = db.cards_named(name for name, _ in wanted)
    facts: dict[str, Piece] = {}
    for name, type_line in wanted:
        info = infos.get(name.casefold())
        text = db.card_text(info.oracle_id) if info else None
        if info is None or text is None:
            facts[name.casefold()] = Piece(name, type_line)
            continue
        facts[name.casefold()] = Piece(
            info.name,
            info.type_line,
            text.colors,
            _stat(text.power),
            _stat(text.toughness),
            "flying" in text.keywords,
            info.cmc,
            storm="storm" in text.keywords,
            x_cost="{X}" in info.mana_cost,
        )
    return facts


def _compare(left: float, op: str, right: int) -> bool:
    if op == "<=":
        return left <= right
    if op == ">=":
        return left >= right
    return left == right


def _stat(value: str | None) -> int | None:
    return int(value) if value is not None and value.lstrip("-").isdigit() else None


_REMINDER = re.compile(r"\([^)]*\)")
_SENTENCE_BREAK = re.compile(r"\n|•|(?<=\.)\s+")
_TARGET = re.compile(r"\b(any target|any number of target|up to \w+ target|target|each|all)\b\s*([^.]*)")
_PHRASE_END = re.compile(
    r"\b(?:you|an opponent|with|without|if|to|from|unless|it's|its|that|on|into|until|gets?|shuffles?|"
    r"among|as|under|then|deals?|and its|of their)\b"
)
_COUNTER = re.compile(r"\bcounter (?:target|that|it\b|all|each|up to)")
_EXILE_SPELL = re.compile(r"\bexile\b[^.]*\btarget spells?\b")
_DAMAGE = re.compile(r"\bdeals? (\d+|x) damage\b")
_SHRINK = re.compile(r"\bgets? [+-](?:\d+|x)/-(\d+|x)\b")
_REMOVE = re.compile(
    r"\b(?:destroy|exile)\b|\breturn\b[^.]*\bto (?:its|their) owner'?s'? hands?\b|"
    r"\bshuffles? (?:it |them )?into\b|\bon (?:the bottom|top) of (?:its|their) owner'?s'? librar"
)
_FLICKER = re.compile(r"\breturn (?:it|that card|them|those cards) to the battlefield\b")
_COMBAT = re.compile(r"\b(?:blocking|blocked|attacking)\b")
_IF_COLOR = re.compile(r"\bif it's (white|blue|black|red|green)\b")
_MANA_VALUE = re.compile(r"\bmana value (\d+)( or less| or greater)?")
_POWER_OR_TOUGHNESS = re.compile(r"\bpower or toughness (\d+) or less\b")
_SOFT = re.compile(r"\bunless its controller pays (\{[^}]+\}|x)")
# Kataki's "sacrifice this artifact unless you pay {1}": paid in its controller's upkeep, before they combo.
_TAX = re.compile(r"\bsacrifice\b[^.]*\bunless\b[^.]*\bpays?\b")
_EDICT = re.compile(
    r"\b(?:player|opponent)s? sacrifices? (?:a|an|one|two|three|four|x|\d+|that many)\b\s*([^.]*)"
)
EDICT_CATCH = "they choose what to sacrifice"


def read_modes(text: str) -> tuple[Mode, ...] | None:
    """The modes in a card's rules text; None when no sentence says something the reader follows."""
    modes: list[Mode] = []
    understood = False
    for face in text.split(FACE_SEPARATOR):
        sentences = _SENTENCE_BREAK.split(_REMINDER.sub("", face).casefold())
        storm = any(sentence.strip() == "storm" for sentence in sentences)
        for sentence in sentences:
            found = _read_sentence(sentence)
            if found is not None:
                understood = True
                modes.extend(replace(m, every_copy=True) if storm else m for m in found)
    return tuple(modes) if understood else None


def _read_sentence(sentence: str) -> tuple[Mode, ...] | None:  # noqa: PLR0911 — one way out per kind of effect
    """A sentence's mode; () when it is read but reaches no combo piece, None when it isn't read."""
    counter = _COUNTER.search(sentence) or _EXILE_SPELL.search(sentence)
    if counter and "spell" in sentence:
        target = _target(sentence[counter.start() :], counter_text=True)
        return None if target is None else (_mode(Effect.COUNTER, sentence, target),)
    if _TAX.search(sentence):
        return ()
    edict = _EDICT.search(sentence)
    if edict:
        target = _target_phrase(edict.group(1))
        return (_mode(Effect.REMOVE, sentence, target, catch=EDICT_CATCH),) if target.nouns else None
    damage = _DAMAGE.search(sentence)
    shrink = _SHRINK.search(sentence)
    remove = _REMOVE.search(sentence)
    if not (damage or shrink or remove):
        return None
    target = _target(sentence[damage.end() :] if damage else sentence)
    if target is None:
        return None
    if _COMBAT.search(sentence) or "card" in target.nouns:
        return ()
    if damage:
        return (_mode(Effect.DAMAGE, sentence, target, _amount(damage.group(1))),)
    if shrink:
        return (_mode(Effect.SHRINK, sentence, target, _amount(shrink.group(1))),)
    if _FLICKER.search(sentence):
        return ()
    return (_mode(Effect.REMOVE, sentence, target),)


@dataclass(slots=True, frozen=True)
class _Target:
    nouns: frozenset[str]
    types: frozenset[str]
    excluded_types: frozenset[str]
    colors: str
    excluded_colors: str
    every: bool = False


_COUNTER_TARGET = re.compile(r"\b(any number of target|target|that|all|each)\b\s*([^.]*)")
_EVERY = frozenset({"any number of target", "all", "each"})


def _target(text: str, *, counter_text: bool = False) -> _Target | None:
    """What the first target phrase names; None when it names no card type (a player, say)."""
    found = (_COUNTER_TARGET if counter_text else _TARGET).search(text)
    if not found:
        return None
    keyword, phrase = found.group(1), found.group(2)
    target = _target_phrase(phrase)
    if keyword in _EVERY:
        target = replace(target, every=True)
    if keyword == "any target":
        target = replace(target, nouns=target.nouns | {"any target"}, types=target.types | _DAMAGEABLE)
    return target if target.nouns else None


def _target_phrase(phrase: str) -> _Target:
    """What a phrase naming a target says, up to where it moves on to something else."""
    end = _PHRASE_END.search(phrase)
    return _read_words(re.findall(r"[a-z]+", phrase[: end.start()] if end else phrase))


def _read_words(words: Sequence[str]) -> _Target:
    nouns: set[str] = set()
    types: set[str] = set()
    excluded: set[str] = set()
    colors = excluded_colors = ""
    for word in words:
        bare = word.removesuffix("s")
        rest = word.removeprefix("non")
        if bare in {"spell", "permanent", "card"} or bare in _TYPE_WORDS:
            nouns.add(bare)
            types.add(_TYPE_WORDS.get(bare, bare.capitalize()))
        elif word in _COLOR_WORDS:
            colors += _COLOR_WORDS[word]
        elif rest != word and rest in _TYPE_WORDS:
            excluded.add(_TYPE_WORDS[rest])
        elif rest != word and rest in _COLOR_WORDS:
            excluded_colors += _COLOR_WORDS[rest]
    return _Target(frozenset(nouns), frozenset(types), frozenset(excluded), colors, excluded_colors)


def _mode(
    effect: Effect, sentence: str, target: _Target, amount: int | None = None, *, catch: str = ""
) -> Mode:
    if_color = _IF_COLOR.search(sentence)
    soft = _SOFT.search(sentence)
    if soft:
        catch = f"unless they pay {soft.group(1).upper()}"
    elif "revealed card" in sentence:
        catch = "if the revealed card's mana value matches"
    mana_value = _MANA_VALUE.search(sentence)
    small = _POWER_OR_TOUGHNESS.search(sentence)
    return Mode(
        effect,
        target.types,
        target.excluded_types,
        target.colors + (_COLOR_WORDS[if_color.group(1)] if if_color else ""),
        target.excluded_colors,
        amount,
        spares_flying="without flying" in sentence,
        mana_value=(
            (
                {" or less": "<=", " or greater": ">="}.get(mana_value.group(2) or "", "="),
                int(mana_value.group(1)),
            )
            if mana_value
            else None
        ),
        max_power_or_toughness=int(small.group(1)) if small else None,
        free_only="no mana was spent" in sentence,
        every_copy=target.every,
        catch=catch,
    )


def _amount(text: str) -> int | None:
    return None if text == "x" else int(text)
