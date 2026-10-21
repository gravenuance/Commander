"""Bracket reports: a deck's bracket-relevant cards, and Commander Spellbook's estimate cached per deck."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from commander.analysis import game_changers, not_legal, tagged
from commander.background import run_all
from commander.bracketcheck import BracketCheck, check_bracket
from commander.carddb import ResolvedCard, Role
from commander.deckcache import (
    NOT_FETCHED,
    Answer,
    AnswerSource,
    Clock,
    DeckCache,
    NotFetchedError,
    cached_answer,
)
from commander.jsonshape import as_object, req_int, req_list, req_str, str_list
from commander.models import Deck
from commander.net import NetworkError
from commander.spellbook import (
    FRESH_FOR,
    PARALLEL_REQUESTS,
    BracketEstimate,
    BracketTag,
    CardFlag,
    Combo,
    ComboFlag,
    FlaggedCard,
    SpellbookClient,
    SpellbookError,
    TwoCard,
    deck_request,
)

CACHE_FOLDER = "spellbook"
# 2: the answer moved from the "estimate" key to "answer"; 3: combos keep their speed and flags, and the
# flagged cards are kept. Older files are fetched over.
CACHE_VERSION = 3


type EstimateResult = Answer[BracketEstimate]


@dataclass(slots=True, frozen=True)
class BracketCards:
    game_changers: tuple[str, ...]
    tutors: tuple[str, ...] | None
    # None when the tag data lacks the role, so "none found" is never claimed without data.
    mass_land_denial: tuple[str, ...] | None
    extra_turns: tuple[str, ...] | None
    not_legal: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class BracketReport:
    deck: Deck
    cards: BracketCards
    result: EstimateResult
    # The profile's bracket_ceiling, when it sets one.
    ceiling: int | None = None

    @property
    def check(self) -> BracketCheck:
        return check_bracket(self.deck.bracket, self.ceiling, self.result.data)

    @property
    def set_below_estimate(self) -> bool:
        """The bracket set on Archidekt is lower than the lowest one the estimate allows."""
        estimate = self.result.data
        if estimate is None or estimate.tag.minimum is None or self.deck.bracket is None:
            return False
        return self.deck.bracket < estimate.tag.minimum


def bracket_cards(resolved: Sequence[ResolvedCard], available_roles: frozenset[Role]) -> BracketCards:
    def role(r: Role) -> tuple[str, ...] | None:
        return tagged(resolved, r) if r in available_roles else None

    return BracketCards(
        game_changers=game_changers(resolved),
        tutors=role(Role.TUTOR),
        mass_land_denial=role(Role.MASS_LAND_DENIAL),
        extra_turns=role(Role.EXTRA_TURN),
        not_legal=not_legal(resolved),
    )


def estimate_cache(folder: Path) -> DeckCache[BracketEstimate]:
    return DeckCache(
        folder,
        version=CACHE_VERSION,
        what="bracket estimate",
        encode=_estimate_to_json,
        decode=_estimate_from_json,
    )


def estimate_decks(
    decks: Sequence[Deck],
    client: SpellbookClient,
    cache: DeckCache[BracketEstimate],
    *,
    now: Clock,
    refresh: bool,
) -> list[EstimateResult]:
    """One estimate per deck, in deck order; failures come back as results, never as exceptions."""
    jobs = [partial(estimate_deck, deck, client, cache, now=now, refresh=refresh) for deck in decks]
    return [future.result() for future in run_all(jobs, limit=PARALLEL_REQUESTS, name="spellbook")]


def estimate_deck(
    deck: Deck, client: SpellbookClient, cache: DeckCache[BracketEstimate], *, now: Clock, refresh: bool
) -> EstimateResult:
    try:
        request = deck_request(deck)
    except SpellbookError as error:
        return Answer(None, AnswerSource.UNAVAILABLE, None, str(error))
    return cached_answer(
        deck.id,
        request,
        client.estimate_bracket,
        cache,
        now=now,
        refresh=refresh,
        fresh_for=FRESH_FOR,
        errors=(NetworkError, SpellbookError),
    )


def effective_bracket(deck: Deck, estimate: BracketEstimate | None) -> int | None:
    """The bracket the app and the CLI act on (ADR 0051): the one set on Archidekt, else the lowest the
    estimate allows; None when neither tells it (no estimate, or a Banned one)."""
    if deck.bracket is not None:
        return deck.bracket
    return estimate.tag.minimum if estimate is not None else None


def with_effective_bracket(deck: Deck, estimate: BracketEstimate | None) -> Deck:
    """The deck as the app and the CLI act on it: its bracket is the effective one. Not a deck to save."""
    bracket = effective_bracket(deck, estimate)
    return deck if bracket == deck.bracket else replace(deck, bracket=bracket)


def estimated_decks(
    decks: Sequence[Deck],
    client: SpellbookClient,
    cache: DeckCache[BracketEstimate],
    *,
    now: Clock,
    refresh: bool = False,
) -> list[Deck]:
    """The decks in order with the effective bracket; asks Spellbook (or the cache) only for the decks with
    no bracket set, and a deck whose estimate fails stays unset."""
    unset = [deck for deck in decks if deck.bracket is None]
    results = estimate_decks(unset, client, cache, now=now, refresh=refresh)
    estimates = {deck.id: result.data for deck, result in zip(unset, results, strict=True)}
    return [with_effective_bracket(deck, estimates.get(deck.id)) for deck in decks]


def cached_effective_deck(deck: Deck, cache: DeckCache[BracketEstimate], *, now: Clock) -> Deck:
    """`with_effective_bracket` from the cache alone, however old its answer: a view must not wait for
    Spellbook, and the next sync brings the answer up to date."""
    if deck.bracket is not None:
        return deck
    try:
        request = deck_request(deck)
    except SpellbookError:
        return deck
    answer = cached_answer(
        deck.id,
        request,
        _not_fetched,
        cache,
        now=now,
        refresh=False,
        fresh_for=FRESH_FOR,
        errors=(NotFetchedError,),
    )
    return with_effective_bracket(deck, answer.data)


def _not_fetched(_request: Mapping[str, object]) -> BracketEstimate:
    raise NotFetchedError(NOT_FETCHED)


def _estimate_to_json(estimate: BracketEstimate) -> dict[str, object]:
    return {
        "tag": estimate.tag.code,
        "combos": [
            {
                "id": c.id,
                "tag": c.tag.code,
                "cards": list(c.cards),
                "results": list(c.results),
                "twoCard": c.two_card.value,
                "speed": c.speed,
                "flags": sorted(flag.value for flag in c.flags),
            }
            for c in estimate.combos
        ],
        "cards": [
            {"name": c.name, "quantity": c.quantity, "flags": sorted(flag.value for flag in c.flags)}
            for c in estimate.cards
        ],
    }


def _estimate_from_json(raw: object) -> BracketEstimate:
    obj = as_object(raw, "cache.estimate")
    combos: list[Combo] = []
    for index, item in enumerate(req_list(obj, "combos", "cache.estimate")):
        where = f"cache.estimate.combos[{index}]"
        combo = as_object(item, where)
        combos.append(
            Combo(
                id=req_str(combo, "id", where),
                tag=BracketTag.from_code(req_str(combo, "tag", where)),
                cards=str_list(combo, "cards", where),
                results=str_list(combo, "results", where),
                two_card=TwoCard(req_str(combo, "twoCard", where)),
                speed=req_int(combo, "speed", where),
                flags=frozenset(ComboFlag(flag) for flag in str_list(combo, "flags", where)),
            )
        )
    cards: list[FlaggedCard] = []
    for index, item in enumerate(req_list(obj, "cards", "cache.estimate")):
        where = f"cache.estimate.cards[{index}]"
        card = as_object(item, where)
        cards.append(
            FlaggedCard(
                name=req_str(card, "name", where),
                quantity=req_int(card, "quantity", where),
                flags=frozenset(CardFlag(flag) for flag in str_list(card, "flags", where)),
            )
        )
    tag = BracketTag.from_code(req_str(obj, "tag", "cache.estimate"))
    return BracketEstimate(tag, tuple(combos), tuple(cards))
