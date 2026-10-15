"""Bracket reports: a deck's bracket-relevant cards, and Commander Spellbook's estimate cached per deck."""

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from functools import partial
from pathlib import Path

from commander.analysis import game_changers, not_legal, tagged
from commander.background import run_all
from commander.carddb import ResolvedCard, Role
from commander.fsutil import write_text_atomic
from commander.jsonshape import ShapeError, as_object, req_list, req_str, str_list
from commander.models import Deck
from commander.net import NetworkError
from commander.spellbook import (
    BracketEstimate,
    BracketTag,
    Combo,
    SpellbookClient,
    SpellbookError,
    TwoCard,
    deck_request,
    request_key,
)

log = logging.getLogger(__name__)

CACHE_FOLDER = "spellbook"
CACHE_VERSION = 1
# Spellbook's combo data changes slowly; a changed card list is fetched again whatever its age.
FRESH_FOR = timedelta(days=7)
# Spellbook asks for sparse requests; four at a time keeps a full run of decks to a few seconds.
PARALLEL_REQUESTS = 4

Clock = Callable[[], datetime]


class EstimateSource(Enum):
    FETCHED = "fetched"
    CACHED = "cached"
    # Spellbook failed, so an older answer for the same card list stands in.
    STALE = "stale"
    UNAVAILABLE = "unavailable"


@dataclass(slots=True, frozen=True)
class EstimateResult:
    estimate: BracketEstimate | None
    source: EstimateSource
    fetched_at: datetime | None
    error: str | None

    def __post_init__(self) -> None:
        if (self.estimate is None) != (self.source is EstimateSource.UNAVAILABLE):
            raise ValueError("an estimate is missing exactly when the source is unavailable")


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
        estimate = self.result.estimate
        if estimate is None or estimate.tag.minimum is None or self.deck.bracket is None:
            return False
        return self.deck.bracket < estimate.tag.minimum


@dataclass(slots=True, frozen=True)
class CachedEstimate:
    estimate: BracketEstimate
    fetched_at: datetime


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


class EstimateCache:
    """One JSON file per deck; anything unreadable or from another version counts as missing."""

    def __init__(self, folder: Path) -> None:
        self._folder = folder

    def load(self, deck_id: int, key: str) -> CachedEstimate | None:
        path = self._path(deck_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            log.warning("ignoring unreadable cached estimate %s: %s", path, error)
            return None
        try:
            obj = as_object(raw, "cache")
            if obj.get("version") != CACHE_VERSION or obj.get("key") != key:
                return None
            fetched_at = datetime.fromisoformat(req_str(obj, "fetchedAt", "cache"))
            estimate = _estimate_from_json(obj.get("estimate"))
        except (ShapeError, ValueError) as error:
            log.warning("ignoring malformed cached estimate %s: %s", path, error)
            return None
        if fetched_at.tzinfo is None:
            log.warning("ignoring cached estimate %s: its time has no time zone", path)
            return None
        return CachedEstimate(estimate, fetched_at)

    def save(self, deck_id: int, key: str, estimate: BracketEstimate, fetched_at: datetime) -> None:
        document = {
            "version": CACHE_VERSION,
            "key": key,
            "fetchedAt": fetched_at.isoformat(),
            "estimate": _estimate_to_json(estimate),
        }
        write_text_atomic(self._path(deck_id), json.dumps(document, ensure_ascii=False, indent=1) + "\n")

    def _path(self, deck_id: int) -> Path:
        return self._folder / f"{deck_id}.json"


def estimate_decks(
    decks: Sequence[Deck], client: SpellbookClient, cache: EstimateCache, *, now: Clock, refresh: bool
) -> list[EstimateResult]:
    """One estimate per deck, in deck order; failures come back as results, never as exceptions."""
    jobs = [partial(estimate_deck, deck, client, cache, now=now, refresh=refresh) for deck in decks]
    return [future.result() for future in run_all(jobs, limit=PARALLEL_REQUESTS, name="spellbook")]


def estimate_deck(
    deck: Deck, client: SpellbookClient, cache: EstimateCache, *, now: Clock, refresh: bool
) -> EstimateResult:
    try:
        request = deck_request(deck)
    except SpellbookError as error:
        return EstimateResult(None, EstimateSource.UNAVAILABLE, None, str(error))
    key = request_key(request)
    cached = cache.load(deck.id, key)
    if cached is not None and not refresh and now() - cached.fetched_at < FRESH_FOR:
        return EstimateResult(cached.estimate, EstimateSource.CACHED, cached.fetched_at, None)
    try:
        estimate = client.estimate_bracket(request)
    except (NetworkError, SpellbookError) as error:
        log.info("bracket estimate for deck %d failed: %s", deck.id, error)
        if cached is not None:
            return EstimateResult(cached.estimate, EstimateSource.STALE, cached.fetched_at, str(error))
        return EstimateResult(None, EstimateSource.UNAVAILABLE, None, str(error))
    fetched_at = now()
    try:
        cache.save(deck.id, key, estimate, fetched_at)
    except OSError as error:
        log.warning("could not cache the bracket estimate for deck %d: %s", deck.id, error)
    return EstimateResult(estimate, EstimateSource.FETCHED, fetched_at, None)


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
