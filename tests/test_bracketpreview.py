"""Spellbook's estimate for a deck as a plan would leave it, shown before saving."""

import copy
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from commander.apply import Resolution, resolve_plan
from commander.archidekt import parse_deck
from commander.bracket import estimate_cache, estimate_deck
from commander.bracketpreview import BracketPreview, preview_bracket, preview_json
from commander.carddb import CardDb
from commander.deckcache import AnswerSource
from commander.models import Deck
from commander.net import Fetcher
from commander.plan import AddCard, Plan, RemoveCard
from commander.spellbook import BracketTag, SpellbookClient
from conftest import FakeSpellbook, Json

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
SERVER_ERROR = 500


@pytest.fixture
def db(card_db_file: Path) -> Iterator[CardDb]:
    with CardDb(card_db_file) as opened:
        yield opened


@pytest.fixture
def deck(raw_deck: Json) -> Deck:
    """Magda (commander), Battered Golem, Disruptor Flute and six Mountains, set to bracket 5; High-Speed
    Hoverbike and Tempestra, Dame of Games wait on the Maybeboard."""
    return parse_deck(raw_deck)


def swaps() -> Plan:
    return Plan(
        "magda",
        (AddCard("Arcane Signet"), AddCard("High-Speed Hoverbike")),
        (RemoveCard("Disruptor Flute"), RemoveCard("Mountain", keep=5)),
    )


def preview(
    resolution: Resolution, fake: FakeSpellbook, cache_root: Path, *, ceiling: int | None = None
) -> BracketPreview | None:
    with fake.client() as http:
        client = SpellbookClient(Fetcher(http, max_attempts=1))
        cache = estimate_cache(cache_root)
        return preview_bracket(resolution, client, cache, ceiling=ceiling, now=lambda: NOW)


def test_spellbook_is_asked_about_the_deck_as_the_plan_leaves_it(
    deck: Deck, db: CardDb, tmp_path: Path
) -> None:
    fake = FakeSpellbook()
    preview(resolve_plan(deck, swaps(), db), fake, tmp_path)
    assert fake.bodies == [
        {
            "commanders": [{"card": "Magda, Brazen Outlaw", "quantity": 1}],
            "main": [
                {"card": "Arcane Signet", "quantity": 1},
                {"card": "Battered Golem", "quantity": 1},
                {"card": "High-Speed Hoverbike", "quantity": 1},
                {"card": "Mountain", "quantity": 5},
            ],
        }
    ]


def test_the_estimate_after_is_checked_against_the_set_bracket_and_the_ceiling(
    deck: Deck, db: CardDb, tmp_path: Path
) -> None:
    result = preview(resolve_plan(deck, swaps(), db), FakeSpellbook(), tmp_path, ceiling=3)
    assert result is not None
    assert result.after.data is not None
    assert result.after.data.tag is BracketTag.RUTHLESS
    assert result.check.notes == (
        "the profile's bracket ceiling (3) is below the Archidekt bracket (5)",
        "the estimate (4+) is above the profile's bracket ceiling (3)",
    )
    view = preview_json(result)
    assert view is not None
    assert (view["before"], view["after"], view["afterLabel"], view["unavailable"]) == (
        None,
        "4+",
        "Ruthless",
        None,
    )


def test_the_estimate_cached_for_the_deck_is_the_before_and_stays_the_decks(
    deck: Deck, db: CardDb, tmp_path: Path
) -> None:
    fake = FakeSpellbook()
    powerful = copy.deepcopy(fake.answer)
    powerful["bracketTag"] = "P"
    fake.answer = powerful
    with fake.client() as http:
        estimate_deck(
            deck, SpellbookClient(Fetcher(http)), estimate_cache(tmp_path), now=lambda: NOW, refresh=False
        )
    fake.answer = FakeSpellbook().answer

    result = preview(resolve_plan(deck, swaps(), db), fake, tmp_path)

    assert result is not None
    assert result.before is not None
    assert result.before.tag is BracketTag.POWERFUL
    deck_cache = json.loads((tmp_path / "10000001.json").read_text(encoding="utf-8"))
    assert deck_cache["answer"]["tag"] == "P"
    assert (tmp_path / "10000001-after.json").is_file()


def test_the_same_plan_previewed_again_is_answered_from_the_cache(
    deck: Deck, db: CardDb, tmp_path: Path
) -> None:
    fake = FakeSpellbook()
    resolution = resolve_plan(deck, swaps(), db)
    preview(resolution, fake, tmp_path)
    again = preview(resolution, fake, tmp_path)
    assert again is not None
    assert again.after.source is AnswerSource.CACHED
    assert len(fake.bodies) == 1


def test_no_preview_for_a_plan_with_problems(deck: Deck, db: CardDb, tmp_path: Path) -> None:
    fake = FakeSpellbook()
    resolution = resolve_plan(deck, Plan("magda", (AddCard("No Such Card Anywhere"),), ()), db)
    assert resolution.problems
    assert preview(resolution, fake, tmp_path) is None
    assert fake.bodies == []


def test_no_preview_when_no_counted_card_changes(deck: Deck, db: CardDb, tmp_path: Path) -> None:
    fake = FakeSpellbook()
    resolution = resolve_plan(
        deck, Plan("magda", (AddCard("Arcane Signet", category="Burn"),), ()), db, categories={"Burn": False}
    )
    assert resolution.changes
    assert not resolution.changes[0].in_deck
    assert preview(resolution, fake, tmp_path) is None
    assert fake.bodies == []


def test_spellbook_failing_leaves_the_estimate_unknown_with_the_reason(
    deck: Deck, db: CardDb, tmp_path: Path
) -> None:
    fake = FakeSpellbook()
    fake.status = SERVER_ERROR
    result = preview(resolve_plan(deck, swaps(), db), fake, tmp_path)
    assert result is not None
    assert result.after.source is AnswerSource.UNAVAILABLE
    assert result.after.error
    view = preview_json(result)
    assert view is not None
    assert view["after"] is None
    assert view["unavailable"] == result.after.error
