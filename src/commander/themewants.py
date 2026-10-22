"""The cards a theme asks for beyond those its Tagger tags name (ADR 0055).

A payoff tag such as synergy-artifact names the cards that reward artifacts, while a deck built on artifacts
is full of the artifacts themselves. A `Want` describes those feeding cards by type, tag, rules text, power
or mana value, and reads a card's `CardFacts`, which the card data gives a deck's card and a preview gives
itself.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache

from commander.carddb import CardDb
from commander.deckprofile import ThemeSpec
from commander.scryfall import SetCard


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

    def matches(self, card: CardFacts) -> bool:
        words = type_words(card.type_line)
        wanted = (
            any(_has_word(words, t) for t in self.types)
            or any(t in card.slugs for t in self.tags)
            or bool(self.text and _pattern(self.text).search(card.text))
        )
        if not wanted or any(_has_word(words, t) for t in self.not_types):
            return False
        if self.max_power is not None and not _power_at_most(card.power, self.max_power):
            return False
        return self.min_mana_value is None or card.mana_value >= self.min_mana_value


ARTIFACT = Want("artifact", types=("Artifact",))
ENCHANTMENT = Want("enchantment", types=("Enchantment",))
LOW_POWER = Want("creature with power 2 or less", types=("Creature",), max_power=2)
TREASURE = Want("treasure maker", tags=("repeatable-treasures",), text=r"\bcreates?\b[^.\n]*\bTreasure\b")
# Spacecraft are Vehicles to a Vehicles deck (checked 2026-10-22).
VEHICLE = Want("vehicle", types=("Vehicle", "Spacecraft"))
# The payoff tags a deck tag stands for, by tag, and the cards that feed them (checked 2026-10-22).
FEEDERS_BY_TAG: Mapping[str, Want] = {
    "synergy-artifact": ARTIFACT,
    "synergy-enchantment": ENCHANTMENT,
    "synergy-low-power": LOW_POWER,
    "synergy-treasure": TREASURE,
    "synergy-vehicle": VEHICLE,
}
# The same by type word, casefolded, for a card data that lacks the tag.
FEEDERS_BY_TYPE: Mapping[str, Want] = {"vehicle": VEHICLE}


def spec_wants(spec: ThemeSpec) -> tuple[Want, ...]:
    """The cards feeding a spec's payoff tags and types, each once."""
    wants = [FEEDERS_BY_TAG[tag] for tag in spec.tags if tag in FEEDERS_BY_TAG]
    wants.extend(FEEDERS_BY_TYPE[t.casefold()] for t in spec.types if t.casefold() in FEEDERS_BY_TYPE)
    return tuple(dict.fromkeys(wants))


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


def _has_word(words: str, word: str) -> bool:
    return f" {word.casefold()} " in words


@cache
def _pattern(text: str) -> re.Pattern[str]:
    return re.compile(text)


def _power_at_most(power: str | None, limit: int) -> bool:
    # A power such as "*" or "1+*" is not a number the card has.
    try:
        return power is not None and int(power) <= limit
    except ValueError:
        return False
