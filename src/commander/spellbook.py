"""Commander Spellbook: the bracket a deck points to, its combos, and the combos it is one card short of."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

from commander.jsonshape import (
    ShapeError,
    as_object,
    opt_int,
    opt_str,
    req_bool,
    req_list,
    req_object,
    req_str,
)
from commander.models import COMMANDER_CATEGORY, Deck
from commander.net import Fetcher

API_BASE = "https://backend.commanderspellbook.com"
COMBO_URL = "https://commanderspellbook.com/combo/{id}/"
# Request limits from the API schema (DeckRequest, CardInDeckRequest).
MAX_MAIN_ENTRIES = 600
MAX_COMMANDERS = 12
MAX_NAME_LENGTH = 256
# Spellbook's combo data changes slowly; a changed card list is asked about again whatever its age.
FRESH_FOR = timedelta(days=7)
# Spellbook asks for sparse requests; four at a time keeps a full run of decks to a few seconds.
PARALLEL_REQUESTS = 4


class SpellbookError(Exception):
    """Commander Spellbook answered with something this tool cannot use."""


class BracketTag(Enum):
    """Spellbook's bracket tags, with the lowest bracket each one fits, per its syntax guide."""

    RUTHLESS = ("R", "Ruthless", 4, "4+")
    SPICY = ("S", "Spicy", 3, "3-4")
    POWERFUL = ("P", "Powerful", 3, "3+")
    ODDBALL = ("O", "Oddball", 2, "2-3")
    CORE = ("C", "Core", 2, "2+")
    EXHIBITION = ("E", "Exhibition", 1, "1+")
    BANNED = ("B", "Banned", None, "not legal")

    def __init__(self, code: str, label: str, minimum: int | None, brackets: str) -> None:
        self.code = code
        self.label = label
        self.minimum = minimum
        self.brackets = brackets

    @classmethod
    def from_code(cls, code: str) -> "BracketTag":
        for tag in cls:
            if tag.code == code:
                return tag
        raise ShapeError(f"unknown bracket tag {code!r}")


class TwoCard(Enum):
    YES = "yes"
    # Spellbook's call: for example a third piece that is the commander, so always at hand.
    ARGUABLY = "arguably"
    NO = "no"


@dataclass(slots=True, frozen=True)
class Combo:
    id: str
    tag: BracketTag
    cards: tuple[str, ...]
    results: tuple[str, ...]
    two_card: TwoCard

    @property
    def url(self) -> str:
        return COMBO_URL.format(id=self.id)


@dataclass(slots=True, frozen=True)
class BracketEstimate:
    tag: BracketTag
    combos: tuple[Combo, ...]


@dataclass(slots=True, frozen=True)
class ComboCard:
    name: str
    # Scryfall's oracle id; None if Spellbook ever leaves it out, and then the name has to do.
    oracle_id: str | None


@dataclass(slots=True, frozen=True)
class Variant:
    """A combo as find-my-combos lists it; unlike a Combo, it says nothing about two-card play."""

    id: str
    tag: BracketTag
    cards: tuple[ComboCard, ...]
    # Pieces described rather than named, such as "Creature with Saddle 4 or less".
    requirements: tuple[str, ...]
    results: tuple[str, ...]
    popularity: int | None

    @property
    def url(self) -> str:
        return COMBO_URL.format(id=self.id)


@dataclass(slots=True, frozen=True)
class ComboMatches:
    included: tuple[Variant, ...]
    # One piece short, within the deck's colours.
    almost_included: tuple[Variant, ...]


class SpellbookClient:
    def __init__(self, fetcher: Fetcher, base_url: str = API_BASE) -> None:
        self._fetcher = fetcher
        self._base_url = base_url.rstrip("/")

    def estimate_bracket(self, request: Mapping[str, object]) -> BracketEstimate:
        raw = self._fetcher.post_json(f"{self._base_url}/estimate-bracket", request)
        try:
            return parse_estimate(raw)
        except ShapeError as error:
            raise SpellbookError(f"unexpected bracket estimate from Commander Spellbook: {error}") from error

    def find_my_combos(self, request: Mapping[str, object]) -> ComboMatches:
        raw = self._fetcher.post_json(f"{self._base_url}/find-my-combos", request)
        try:
            return parse_matches(raw)
        except ShapeError as error:
            raise SpellbookError(f"unexpected combo list from Commander Spellbook: {error}") from error


def deck_request(deck: Deck) -> dict[str, object]:
    """The deck as Spellbook's DeckRequest: commanders apart from the rest, by name."""
    commanders: list[dict[str, object]] = []
    main: list[dict[str, object]] = []
    for card in sorted(deck.main_cards, key=lambda c: c.name):
        if len(card.name) > MAX_NAME_LENGTH:
            raise SpellbookError(f"{deck.name}: card name longer than {MAX_NAME_LENGTH} characters")
        entry: dict[str, object] = {"card": card.name, "quantity": card.quantity}
        (commanders if COMMANDER_CATEGORY in card.categories else main).append(entry)
    if len(main) > MAX_MAIN_ENTRIES or len(commanders) > MAX_COMMANDERS:
        raise SpellbookError(
            f"{deck.name}: Commander Spellbook takes at most {MAX_MAIN_ENTRIES} cards "
            f"and {MAX_COMMANDERS} commanders"
        )
    return {"commanders": commanders, "main": main}


def parse_estimate(raw: object) -> BracketEstimate:
    obj = as_object(raw, "estimate")
    tag = BracketTag.from_code(req_str(obj, "bracketTag", "estimate"))
    combos = tuple(
        _parse_combo(item, f"estimate.combos[{i}]")
        for i, item in enumerate(req_list(obj, "combos", "estimate"))
    )
    return BracketEstimate(tag, combos)


def _parse_combo(raw: object, where: str) -> Combo:
    classified = as_object(raw, where)
    variant = req_object(classified, "combo", where)
    at = f"{where}.combo"
    cards = _names(variant, "uses", "card", at)
    if not cards:
        raise ShapeError(f"{at}.uses: a combo needs at least one card")
    results = _names(variant, "produces", "feature", at)
    if req_bool(classified, "definitelyTwoCard", where):
        two_card = TwoCard.YES
    elif req_bool(classified, "arguablyTwoCard", where):
        two_card = TwoCard.ARGUABLY
    else:
        two_card = TwoCard.NO
    return Combo(
        id=req_str(variant, "id", at),
        tag=BracketTag.from_code(req_str(variant, "bracketTag", at)),
        cards=cards,
        results=results,
        two_card=two_card,
    )


def parse_matches(raw: object) -> ComboMatches:
    results = req_object(as_object(raw, "combos"), "results", "combos")
    return ComboMatches(
        included=_variants(results, "included"),
        almost_included=_variants(results, "almostIncluded"),
    )


def _variants(results: Mapping[str, object], key: str) -> tuple[Variant, ...]:
    return tuple(
        _parse_variant(item, f"combos.results.{key}[{index}]")
        for index, item in enumerate(req_list(results, key, "combos.results"))
    )


def _parse_variant(raw: object, where: str) -> Variant:
    variant = as_object(raw, where)
    cards: list[ComboCard] = []
    for index, item in enumerate(req_list(variant, "uses", where)):
        at = f"{where}.uses[{index}].card"
        card = req_object(as_object(item, at), "card", at)
        cards.append(ComboCard(req_str(card, "name", at), opt_str(card, "oracleId", at)))
    if not cards:
        raise ShapeError(f"{where}.uses: a combo needs at least one card")
    return Variant(
        id=req_str(variant, "id", where),
        tag=BracketTag.from_code(req_str(variant, "bracketTag", where)),
        cards=tuple(cards),
        requirements=_names(variant, "requires", "template", where),
        results=_names(variant, "produces", "feature", where),
        popularity=opt_int(variant, "popularity", where),
    )


def _names(variant: Mapping[str, object], key: str, inner: str, where: str) -> tuple[str, ...]:
    """The `name` of each item's `inner` object, as in `uses[].card.name`."""
    names: list[str] = []
    for index, item in enumerate(req_list(variant, key, where)):
        at = f"{where}.{key}[{index}]"
        names.append(req_str(req_object(as_object(item, at), inner, at), "name", f"{at}.{inner}"))
    return tuple(names)
