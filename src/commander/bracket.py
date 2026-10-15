"""Bracket reports: a deck's bracket-relevant cards, and Commander Spellbook's estimate cached per deck."""

from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from commander.analysis import game_changers, not_legal, tagged
from commander.background import run_all
from commander.carddb import ResolvedCard, Role
from commander.deckcache import Answer, AnswerSource, Clock, DeckCache, cached_answer
from commander.jsonshape import as_object, req_list, req_str, str_list
from commander.models import Deck
from commander.net import NetworkError
from commander.spellbook import (
    FRESH_FOR,
    PARALLEL_REQUESTS,
    BracketEstimate,
    BracketTag,
    Combo,
    SpellbookClient,
    SpellbookError,
    TwoCard,
    deck_request,
)

CACHE_FOLDER = "spellbook"
# 2: the answer moved from the "estimate" key to "answer"; older files are fetched over.
CACHE_VERSION = 2


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
            }
            for c in estimate.combos
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
            )
        )
    return BracketEstimate(BracketTag.from_code(req_str(obj, "tag", "cache.estimate")), tuple(combos))
