import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from commander.archidekt import parse_deck
from commander.bracket import (
    BracketCards,
    BracketReport,
    EstimateResult,
    bracket_cards,
    estimate_cache,
    estimate_deck,
    estimate_decks,
)
from commander.carddb import CardDb
from commander.deckcache import Answer, AnswerSource
from commander.models import Deck
from commander.net import Fetcher
from commander.spellbook import BracketEstimate, BracketTag, SpellbookClient, TwoCard
from conftest import FakeSpellbook, Json

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
NO_CARDS = BracketCards((), (), (), (), ())


class Env:
    """A deck, a fake Spellbook and a cache folder, with the clock under the test's control."""

    def __init__(self, raw_deck: Json, tmp_path: Path) -> None:
        self.deck = parse_deck(raw_deck)
        self.fake = FakeSpellbook()
        self.cache = estimate_cache(tmp_path / "spellbook")
        self.folder = tmp_path / "spellbook"
        self.now = NOW

    def estimate(self, deck: Deck | None = None, *, refresh: bool = False) -> EstimateResult:
        with self.fake.client() as http:
            client = SpellbookClient(Fetcher(http, max_attempts=1))
            return estimate_deck(deck or self.deck, client, self.cache, now=lambda: self.now, refresh=refresh)


@pytest.fixture
def env(raw_deck: Json, tmp_path: Path) -> Env:
    return Env(raw_deck, tmp_path)


def test_first_estimate_is_fetched_and_cached(env: Env) -> None:
    result = env.estimate()
    assert (result.source, result.fetched_at, result.error) == (AnswerSource.FETCHED, NOW, None)
    assert result.data is not None
    assert result.data.tag is BracketTag.RUTHLESS
    assert (env.folder / "10000001.json").is_file()


def test_recent_estimate_comes_from_the_cache(env: Env) -> None:
    env.estimate()
    env.now = NOW + timedelta(days=6, hours=23)
    result = env.estimate()
    assert len(env.fake.bodies) == 1
    assert (result.source, result.fetched_at) == (AnswerSource.CACHED, NOW)
    assert result.data is not None
    assert [c.two_card for c in result.data.combos] == [TwoCard.YES, TwoCard.NO, TwoCard.ARGUABLY]


def test_a_cached_estimate_reads_back_as_fetched(env: Env) -> None:
    fetched = env.estimate().data
    cached = env.estimate().data
    assert len(env.fake.bodies) == 1
    assert fetched is not None
    assert fetched.cards
    assert cached == fetched


def test_week_old_estimate_is_fetched_again(env: Env) -> None:
    env.estimate()
    env.now = NOW + timedelta(days=7)
    assert env.estimate().source is AnswerSource.FETCHED
    assert len(env.fake.bodies) == 2


def test_refresh_ignores_a_recent_estimate(env: Env) -> None:
    env.estimate()
    assert env.estimate(refresh=True).source is AnswerSource.FETCHED
    assert len(env.fake.bodies) == 2


def test_changed_card_list_is_fetched_again(env: Env) -> None:
    env.estimate()
    changed = replace(env.deck, cards=tuple(c for c in env.deck.cards if c.name != "Disruptor Flute"))
    assert env.estimate(changed).source is AnswerSource.FETCHED
    assert len(env.fake.bodies) == 2


def test_failed_refresh_falls_back_to_the_older_estimate(env: Env) -> None:
    env.estimate()
    env.now = NOW + timedelta(days=8)
    env.fake.status = 503
    result = env.estimate()
    assert (result.source, result.fetched_at) == (AnswerSource.STALE, NOW)
    assert result.error is not None
    assert "HTTP 503" in result.error
    assert result.data is not None


def test_failure_without_a_cached_estimate_is_unavailable(env: Env) -> None:
    env.fake.status = 404
    result = env.estimate()
    assert (result.source, result.data, result.fetched_at) == (AnswerSource.UNAVAILABLE, None, None)
    assert result.error is not None
    assert "HTTP 404" in result.error


def test_older_estimate_for_other_cards_is_not_a_fallback(env: Env) -> None:
    env.estimate()
    env.fake.status = 503
    changed = replace(env.deck, cards=env.deck.cards[1:])
    assert env.estimate(changed).source is AnswerSource.UNAVAILABLE


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '{"version": 99}',
        '{"version": 3, "key": "?", "fetchedAt": "2026-10-15T12:00:00+00:00", "answer": {}}',
        "[]",
    ],
)
def test_unusable_cache_file_is_fetched_over(env: Env, content: str) -> None:
    env.folder.mkdir()
    (env.folder / "10000001.json").write_text(content, encoding="utf-8")
    assert env.estimate().source is AnswerSource.FETCHED
    saved = json.loads((env.folder / "10000001.json").read_text(encoding="utf-8"))
    assert saved["version"] == 3


def test_cached_estimate_with_a_bad_combo_is_ignored_with_a_warning(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    env.estimate()
    path = env.folder / "10000001.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["answer"]["combos"][0]["twoCard"] = "maybe"
    path.write_text(json.dumps(saved), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert env.estimate().source is AnswerSource.FETCHED
    assert "ignoring malformed cached bracket estimate" in caplog.text


def test_cache_time_without_a_zone_is_ignored(env: Env) -> None:
    env.estimate()
    path = env.folder / "10000001.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["fetchedAt"] = "2026-10-15T12:00:00"
    path.write_text(json.dumps(saved), encoding="utf-8")
    assert env.estimate().source is AnswerSource.FETCHED


def test_unwritable_cache_still_returns_the_estimate(
    env: Env, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A file where the cache folder should be makes every save fail.
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    env.cache = estimate_cache(blocker)
    with caplog.at_level(logging.WARNING):
        result = env.estimate()
    assert result.source is AnswerSource.FETCHED
    assert "could not cache the bracket estimate for deck 10000001" in caplog.text


def test_several_decks_come_back_in_deck_order(env: Env) -> None:
    decks = [replace(env.deck, id=deck_id) for deck_id in (5, 3, 9, 1, 7, 2)]
    env.fake.answer["bracketTag"] = "C"
    with env.fake.client() as http:
        results = estimate_decks(
            decks, SpellbookClient(Fetcher(http)), env.cache, now=lambda: NOW, refresh=False
        )
    assert [r.source for r in results] == [AnswerSource.FETCHED] * 6
    assert sorted(p.stem for p in env.folder.iterdir()) == ["1", "2", "3", "5", "7", "9"]


def report(bracket: int | None, tag: BracketTag | None) -> BracketReport:
    deck = Deck(1, "deck", "TESTER", bracket, "2026-10-15T00:00:00Z", ())
    if tag is None:
        result: EstimateResult = Answer(None, AnswerSource.UNAVAILABLE, None, "down")
    else:
        result = Answer(BracketEstimate(tag, ()), AnswerSource.FETCHED, NOW, None)
    return BracketReport(deck, NO_CARDS, result)


@pytest.mark.parametrize(
    ("bracket", "tag", "below"),
    [
        (3, BracketTag.RUTHLESS, True),
        (4, BracketTag.RUTHLESS, False),
        (2, BracketTag.SPICY, True),
        (3, BracketTag.SPICY, False),
        (1, BracketTag.EXHIBITION, False),
        (1, BracketTag.BANNED, False),
        (None, BracketTag.RUTHLESS, False),
        (2, None, False),
    ],
)
def test_set_bracket_below_the_estimate(bracket: int | None, tag: BracketTag | None, *, below: bool) -> None:
    assert report(bracket, tag).set_below_estimate is below


def test_estimate_and_source_must_agree() -> None:
    with pytest.raises(ValueError, match="exactly when"):
        Answer(None, AnswerSource.CACHED, NOW, None)


def test_bracket_cards_come_from_the_card_data(raw_deck: Json, card_db_file: Path) -> None:
    deck = parse_deck(raw_deck)
    with CardDb(card_db_file) as db:
        cards = bracket_cards(db.resolve(deck.main_cards), db.available_roles)
    # The fixture tags give Magda the tutor role, list no extra-turn cards and lack mass land denial.
    assert cards.tutors == ("Magda, Brazen Outlaw",)
    assert cards.extra_turns == ()
    assert cards.mass_land_denial is None
    assert cards.game_changers == ()
    assert cards.not_legal == ()
