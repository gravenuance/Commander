"""Commander Spellbook's bracket estimate for a deck as a plan would leave it, shown before a save."""

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from commander.apply import Resolution
from commander.bracket import CACHE_FOLDER, FRESH_FOR, EstimateResult, estimate_cache
from commander.bracketcheck import BracketCheck, check_bracket, check_json
from commander.deckcache import Answer, AnswerSource, Clock, DeckCache, cached_answer, request_key
from commander.models import COMMANDER_CATEGORY
from commander.net import Fetcher, NetworkError
from commander.spellbook import (
    BracketEstimate,
    RequestCard,
    SpellbookClient,
    SpellbookError,
    cards_request,
    deck_request,
)


@dataclass(slots=True, frozen=True)
class BracketPreview:
    # The deck's current estimate when one is cached for its cards; never fetched for a preview.
    before: BracketEstimate | None
    after: EstimateResult
    # The estimate after the changes against the bracket set on Archidekt and the profile's ceiling.
    check: BracketCheck


def after_cards(resolution: Resolution) -> list[RequestCard]:
    """The cards counted in the deck once every change is made."""
    counts = Counter[str]()
    commanders: set[str] = set()
    for card in resolution.deck.main_cards:
        counts[card.name] += card.quantity
        if COMMANDER_CATEGORY in card.categories:
            commanders.add(card.name)
    for change in resolution.changes:
        counts[change.name] += change.counted_after - change.counted_before
    return [RequestCard(name, count, name in commanders) for name, count in counts.items() if count > 0]


def preview_bracket(
    resolution: Resolution,
    client: SpellbookClient,
    cache: DeckCache[BracketEstimate],
    *,
    ceiling: int | None,
    now: Clock,
) -> BracketPreview | None:
    """The estimate after the changes; None when the plan has problems or changes no counted card.

    It is cached under its own name beside the deck's, so a preview never replaces the deck's estimate.
    """
    if resolution.problems:
        return None
    deck = resolution.deck
    try:
        before_request = deck_request(deck)
        after_request = cards_request(deck.name, after_cards(resolution))
    except SpellbookError as error:
        unavailable: EstimateResult = Answer(None, AnswerSource.UNAVAILABLE, None, str(error))
        return BracketPreview(None, unavailable, check_bracket(deck.bracket, ceiling, None))
    if request_key(after_request) == request_key(before_request):
        return None
    cached = cache.load(deck.id, request_key(before_request))
    after = cached_answer(
        f"{deck.id}-after",
        after_request,
        client.estimate_bracket,
        cache,
        now=now,
        refresh=False,
        fresh_for=FRESH_FOR,
        errors=(NetworkError, SpellbookError),
    )
    before = cached.data if cached else None
    return BracketPreview(before, after, check_bracket(deck.bracket, ceiling, after.data))


def spellbook_preview(
    resolution: Resolution, fetcher: Fetcher, cache_root: Path, *, ceiling: int | None, now: Clock
) -> BracketPreview | None:
    """preview_bracket with the Spellbook client and the cache every command shares."""
    cache = estimate_cache(cache_root / CACHE_FOLDER)
    return preview_bracket(resolution, SpellbookClient(fetcher), cache, ceiling=ceiling, now=now)


def preview_json(preview: BracketPreview | None) -> dict[str, object] | None:
    if preview is None:
        return None
    after = preview.after.data
    return {
        "before": preview.before.tag.brackets if preview.before else None,
        "after": after.tag.brackets if after else None,
        "afterLabel": after.tag.label if after else None,
        "unavailable": preview.after.error if after is None else None,
        "check": check_json(preview.check),
    }
