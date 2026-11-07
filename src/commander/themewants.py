"""The cards a theme asks for beyond those its Tagger tags name (ADR 0055).

A payoff tag such as synergy-artifact names the cards that reward artifacts, while a deck built on artifacts
is full of the artifacts themselves. A `Want` describes those feeding cards by type, tag, rules text, power
or mana value, and reads a card's `CardFacts`, which the card data gives a deck's card and a preview gives
itself.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from functools import cache

from commander.carddb import CardDb, Theme
from commander.deckprofile import ThemeSpec
from commander.deckthemes import TYPE_NAMES
from commander.scryfall import SetCard

# One thing a card has of a theme: ("type", "dwarf"), ("keyword", "flash"), ("tag", slug) or ("want", name).
type Trait = tuple[str, str]


@dataclass(slots=True, frozen=True)
class CardFacts:
    """What a Want reads of a card."""

    type_line: str
    mana_value: float
    # The front face's power as printed ("2", "*"); None for a card without one.
    power: str | None
    text: str
    # Casefolded.
    keywords: frozenset[str]
    # The card's Tagger tags with every tag above them.
    slugs: frozenset[str]


@dataclass(slots=True, frozen=True)
class Want:
    """A kind of card: one with any of the types, tags or rules text, that none of `not_types` rules out and
    that meets the power and mana value limits."""

    # Names the want as a feature of a theme, so two wants of one name are one.
    name: str
    types: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    # A regular expression searched in the rules text.
    text: str = ""
    not_types: tuple[str, ...] = ()
    max_power: int | None = None
    min_mana_value: int | None = None

    def matches(self, card: CardFacts, words: str | None = None) -> bool:
        """Whether the card is one; `words` is `type_words` of its type line, for a caller testing many
        wants against one card."""
        # The cheap limits first, so the rules text is searched only for a card that could still match.
        if self.max_power is not None and not _power_at_most(card.power, self.max_power):
            return False
        if self.min_mana_value is not None and card.mana_value < self.min_mana_value:
            return False
        words = type_words(card.type_line) if words is None else words
        if any(_has_word(words, t) for t in self.not_types):
            return False
        return (
            any(_has_word(words, t) for t in self.types)
            or any(t in card.slugs for t in self.tags)
            or bool(self.text and _pattern(self.text).search(card.text))
        )


ARTIFACT = Want("artifact", types=("Artifact",))
ENCHANTMENT = Want("enchantment", types=("Enchantment",))
LOW_POWER = Want("creature with power 2 or less", types=("Creature",), max_power=2)
TREASURE = Want("treasure maker", tags=("repeatable-treasures",), text=r"\b[Cc]reates?\b[^.\n]*\bTreasure\b")
# Spacecraft are Vehicles to a Vehicles deck (checked 2026-10-22).
VEHICLE = Want("vehicle", types=("Vehicle", "Spacecraft"))
AURA = Want("aura", types=("Aura",))
EQUIPMENT = Want("equipment", types=("Equipment",))
# "Create", not "creates": a card that has an opponent create tokens ("its controller creates") feeds no one.
# Nor does a payoff ("whenever you create", "whenever you investigate"), a hoser ("can't create") or a
# doubler ("would create", "create twice that many ... instead"), which make no token of their own.
TOKEN_MAKER = Want(
    "token maker",
    tags=("repeatable-token-generator",),
    text=(
        r"(?<!\byou )(?<!\bwould )(?<!\bcan't )\b[Cc]reate\b(?![^.\n]*\binstead\b)[^.\n]*\btokens?\b"
        r"|(?<!\byou )\b(?:[Ii]nvestigate|[Aa]mass|[Ii]ncubate)\b"
    ),
)
TEAM_PROTECTION = Want(
    "protection for your creatures",
    text=(
        r"(?i)\bcreatures you control (?:gain|have|get)\b[^.\n]*"
        r"\b(?:hexproof|indestructible|shroud|protection from)\b"
    ),
)
# Removal the deck's own creatures survive, or that leaves them for the opponents' to die first.
OPPONENT_REMOVAL = Want(
    "removal for creatures",
    tags=("removal-creature",),
    text=(
        r"(?i)\beach opponent sacrifices\b|\ball (?:other )?creatures get -"
        r"|\b(?:destroy|exile) all (?:other )?\w*\s?creatures\b"
    ),
)
# The spells a commander that casts from the top of the library at a mana value wants.
BIG_SPELL = Want(
    "spell of mana value 4 or more",
    types=("Artifact", "Battle", "Creature", "Enchantment", "Instant", "Kindred", "Planeswalker", "Sorcery"),
    not_types=("Land",),
    min_mana_value=4,
)
# Cards that make the deck's creatures bigger for good: a lasting anthem or a +1/+1 counter put on one, not
# a pump with "until end of turn" anywhere in its sentence, nor a card that only cares about counters ("with
# a +1/+1 counter on it", "whenever you put one or more +1/+1 counters on").
GROWS_CREATURES = Want(
    "grows your creatures",
    tags=("anthem", "gives-pp-counters"),
    text=(
        r"(?im)(?:^|[.•]\s*)(?:(?!until end of turn)[^.\n])*?"
        r"\bcreatures you control get \+[1-9X](?![^.\n]*until end of turn)"
        r"|(?<!\byou )\b(?:put|puts|place|places|distribute)\b[^.\n]*?\+1/\+1 counters?\b"
        r"|\benters? with (?:an )?additional \+1/\+1 counter"
    ),
)
# The payoff tags a deck tag stands for, by tag, and the cards that feed them (checked 2026-10-22, the aura
# and equipment lines 2026-10-30).
FEEDERS_BY_TAG: Mapping[str, Want] = {
    "synergy-artifact": ARTIFACT,
    "synergy-aura": AURA,
    "synergy-enchantment": ENCHANTMENT,
    "synergy-equipment": EQUIPMENT,
    "synergy-low-power": LOW_POWER,
    "synergy-treasure": TREASURE,
    "synergy-vehicle": VEHICLE,
}
# The same by type word, casefolded, for a card data that lacks the tag.
FEEDERS_BY_TYPE: Mapping[str, Want] = {"vehicle": VEHICLE}
# What a theme needs beside the cards it names and feeds, by the spec's tags: a wide board wants to survive a
# wipe, and a sacrifice deck wants fodder. Previews read these; the theme reading does not count them.
SUPPORT_BY_TAG: Mapping[str, Want] = {
    "repeatable-creature-tokens": TEAM_PROTECTION,
    "sacrifice-outlet": TOKEN_MAKER,
    "free-sacrifice-outlet": TOKEN_MAKER,
    "death-trigger": TOKEN_MAKER,
}
# What a commander's Tagger tags say its deck wants (checked 2026-10-22, the token lines 2026-10-30): its
# payoff tags want what feeds them, its Treasure makers want Treasure, and one that adds to the tokens made
# wants token makers.
COMMANDER_WANTS_BY_TAG: Mapping[str, Want] = {
    **FEEDERS_BY_TAG,
    "repeatable-treasures": TREASURE,
    "synergy-token": TOKEN_MAKER,
    "token-doubler": TOKEN_MAKER,
    "token-increaser": TOKEN_MAKER,
    "token-replacer": TOKEN_MAKER,
}
# What a commander's rules text says its deck wants where no tag says it (checked 2026-10-30): one that
# profits when the opponents' creatures die wants removal.
COMMANDER_WANTS_BY_TEXT: tuple[tuple[str, Want], ...] = (
    (
        r"(?i)\bcreatures? (?:an opponent controls|your opponents control) (?:would )?dies?\b",
        OPPONENT_REMOVAL,
    ),
)
# What works against a commander, by its tags (checked 2026-10-30): one counting creatures with power 2 or
# less loses a creature to every counter or lasting anthem.
CONFLICTS_BY_TAG: Mapping[str, Want] = {"synergy-low-power": GROWS_CREATURES}
# A commander that animates cards wants the ones it can animate: no Aura, which is attached, and no
# Equipment, which is too.
ANIMATED_BY_TAG: Mapping[str, Want] = {
    "animate-artifact": Want("artifact that is no Equipment", types=("Artifact",), not_types=("Equipment",)),
    "animate-enchantment": Want("enchantment that is no Aura", types=("Enchantment",), not_types=("Aura",)),
}
# ...and, when it also cares about high mana values, only those of at least BIG_MANA_VALUE.
HIGH_VALUE_TAG = "high-mana-value-matters"
BIG_MANA_VALUE = 4
TYPAL_PREFIX = "typal-"


def spec_wants(spec: ThemeSpec) -> tuple[Want, ...]:
    """The cards feeding a spec's payoff tags and types, each once."""
    wants = [FEEDERS_BY_TAG[tag] for tag in spec.tags if tag in FEEDERS_BY_TAG]
    wants.extend(FEEDERS_BY_TYPE[t.casefold()] for t in spec.types if t.casefold() in FEEDERS_BY_TYPE)
    return tuple(dict.fromkeys(wants))


def named_traits(spec: ThemeSpec, card: CardFacts) -> frozenset[Trait]:
    """The spec's own types, keywords and tags (sub-tags included) that the card has."""
    words = type_words(card.type_line)
    return frozenset(
        [("type", t.casefold()) for t in spec.types if _has_word(words, t)]
        + [("keyword", k.casefold()) for k in spec.keywords if k.casefold() in card.keywords]
        + [("tag", t) for t in spec.tags if t in card.slugs]
    )


def theme_wants(spec: ThemeSpec) -> tuple[Want, ...]:
    """The cards feeding a spec and those its theme needs beside them (SUPPORT_BY_TAG), each once."""
    support = [SUPPORT_BY_TAG[tag] for tag in spec.tags if tag in SUPPORT_BY_TAG]
    return tuple(dict.fromkeys([*spec_wants(spec), *support]))


def traits(spec: ThemeSpec, card: CardFacts) -> frozenset[Trait]:
    """What of the spec the card has: its named traits, and the wants of its theme (`theme_wants`) the card
    meets."""
    return named_traits(spec, card) | {want_trait(w) for w in theme_wants(spec) if w.matches(card)}


def want_trait(want: Want) -> Trait:
    return ("want", want.name)


def commander_wants(db: CardDb, slugs: Iterable[str], texts: Iterable[str] = ()) -> tuple[Want, ...]:
    """The cards a commander with these Tagger tags (sub-tags' parents included) and rules texts wants in
    its deck."""
    tags = sorted(set(slugs))
    wants = [COMMANDER_WANTS_BY_TAG[tag] for tag in tags if tag in COMMANDER_WANTS_BY_TAG]
    wants.extend(
        want for text in texts for rule, want in COMMANDER_WANTS_BY_TEXT if _pattern(rule).search(text)
    )
    animates = [ANIMATED_BY_TAG[tag] for tag in tags if tag in ANIMATED_BY_TAG]
    if HIGH_VALUE_TAG in tags:
        # A commander animating cards wants the big ones among them; any other wants big spells.
        wants.extend([_big(animated) for animated in animates] or [BIG_SPELL])
    else:
        wants.extend(animates)
    for tag in tags:
        tribe = tag.removeprefix(TYPAL_PREFIX).replace("-", " ") if tag.startswith(TYPAL_PREFIX) else ""
        # Only a subtype some card has is a tribe: typal-creature and typal-choose are not.
        if tribe and tribe not in TYPE_NAMES and db.theme_size(Theme(types=(tribe,))):
            wants.append(Want(f"{tribe} typal", types=(tribe,)))
    return tuple(dict.fromkeys(wants))


def commander_conflicts(slugs: Iterable[str]) -> tuple[Want, ...]:
    """The cards that work against a commander with these Tagger tags (CONFLICTS_BY_TAG)."""
    tags = sorted(set(slugs))
    return tuple(dict.fromkeys(CONFLICTS_BY_TAG[tag] for tag in tags if tag in CONFLICTS_BY_TAG))


def want_sizes(db: CardDb, wants: Iterable[Want]) -> dict[Want, int]:
    """How many Commander-legal cards each want matches, worked out once per card data build."""
    asked = list(dict.fromkeys(wants))
    missing = [want for want in asked if db.build_count(("want", want)) is None]
    if missing:
        tags_of: dict[str, set[str]] = {}
        for tag in {t for want in missing for t in want.tags}:
            for oracle_id in db.tag_members(tag):
                tags_of.setdefault(oracle_id, set()).add(tag)
        # A list by position: hashing a frozen dataclass for every card and want costs more than the match.
        counts = [0] * len(missing)
        for face in db.legal_faces():
            slugs = frozenset(tags_of.get(face.oracle_id, ()))
            facts = CardFacts(face.type_line, face.mana_value, face.power, face.text, frozenset(), slugs)
            words = type_words(face.type_line)
            for index, want in enumerate(missing):
                if want.matches(facts, words):
                    counts[index] += 1
        for want, count in zip(missing, counts, strict=True):
            db.set_build_count(("want", want), count)
    return {want: db.build_count(("want", want)) or 0 for want in asked}


def card_facts(db: CardDb, oracle_ids: Iterable[str]) -> dict[str, CardFacts]:
    """The facts of the cards the card data knows, by oracle id."""
    ids = list(dict.fromkeys(oracle_ids))
    texts = db.card_texts(ids)
    slugs = db.tag_lineage(ids)
    facts: dict[str, CardFacts] = {}
    for oracle_id, info in db.cards(ids).items():
        text = texts.get(oracle_id)
        facts[oracle_id] = CardFacts(
            info.type_line,
            info.cmc,
            text.power if text else None,
            text.text if text else "",
            frozenset(k.casefold() for k in text.keywords) if text else frozenset(),
            slugs.get(oracle_id, frozenset()),
        )
    return facts


def preview_facts(card: SetCard, slugs: frozenset[str]) -> CardFacts:
    """The facts of a preview, which the probe read; `slugs` are its tags from the card data."""
    return CardFacts(
        card.type_line,
        card.mana_value,
        card.power,
        card.oracle_text,
        frozenset(k.casefold() for k in card.keywords),
        slugs,
    )


def type_words(type_line: str) -> str:
    """The type line's words with a space around each, so "Elf" never matches "Elfball" or "Self"."""
    return f" {type_line.casefold().replace('—', ' ').replace('//', ' ')} "


def _big(want: Want) -> Want:
    return replace(
        want, name=f"{want.name} of mana value {BIG_MANA_VALUE} or more", min_mana_value=BIG_MANA_VALUE
    )


def _has_word(words: str, word: str) -> bool:
    return _spaced(word) in words


@cache
def _spaced(word: str) -> str:
    return f" {word.casefold()} "


@cache
def _pattern(text: str) -> re.Pattern[str]:
    return re.compile(text)


def _power_at_most(power: str | None, limit: int) -> bool:
    # A power such as "*" or "1+*" is not a number the card has.
    try:
        return power is not None and int(power) <= limit
    except ValueError:
        return False
