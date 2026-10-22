import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from commander.deckcache import (
    NOT_FETCHED,
    Accepted,
    Answer,
    AnswerSource,
    DeckCache,
    NotFetchedError,
    cached_answer,
    request_key,
)
from commander.jsonshape import ShapeError

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
FRESH_FOR = timedelta(hours=24)
REQUEST: Mapping[str, object] = {"q": 1}


def decode(raw: object) -> str:
    if not isinstance(raw, str):
        raise ShapeError("expected a string")
    return raw


def cache_in(folder: Path) -> DeckCache[str]:
    return DeckCache(folder, version=1, what="test answer", encode=str, decode=decode)


class Fetches:
    def __init__(self, answer: str | Exception) -> None:
        self.answer = answer
        self.calls = 0

    def __call__(self, _: Mapping[str, object]) -> str:
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def answer_with(cache: DeckCache[str], fetch: Fetches, *, refresh: bool = False) -> AnswerSource:
    return cached_answer(
        7,
        REQUEST,
        fetch,
        cache,
        now=lambda: NOW,
        refresh=refresh,
        fresh_for=FRESH_FOR,
        errors=(ConnectionError,),
    ).source


def save_fetched(cache: DeckCache[str], age: timedelta) -> None:
    # Saving under the same request key the lookup will use.
    probe = Fetches("old")
    cached_answer(
        7, REQUEST, probe, cache, now=lambda: NOW - age, refresh=True, fresh_for=FRESH_FOR, errors=()
    )


@pytest.mark.parametrize(
    ("age", "source", "calls"),
    [
        (timedelta(hours=23, minutes=59), AnswerSource.CACHED, 0),
        (timedelta(hours=24), AnswerSource.FETCHED, 1),
        (timedelta(0), AnswerSource.CACHED, 0),
        (timedelta(seconds=-1), AnswerSource.FETCHED, 1),
        (timedelta(days=-3), AnswerSource.FETCHED, 1),
    ],
    ids=["just inside", "exactly at the limit", "just fetched", "a second ahead", "days ahead"],
)
def test_an_answer_is_fresh_only_between_its_fetch_and_the_limit(
    tmp_path: Path, age: timedelta, source: AnswerSource, calls: int
) -> None:
    cache = cache_in(tmp_path)
    save_fetched(cache, age)
    fetch = Fetches("new")

    assert answer_with(cache, fetch) is source
    assert fetch.calls == calls


def test_an_answer_stamped_in_the_future_still_stands_in_when_the_service_is_down(tmp_path: Path) -> None:
    cache = cache_in(tmp_path)
    save_fetched(cache, timedelta(days=-3))

    result = cached_answer(
        7,
        REQUEST,
        Fetches(ConnectionError("down")),
        cache,
        now=lambda: NOW,
        refresh=False,
        fresh_for=FRESH_FOR,
        errors=(ConnectionError,),
    )

    assert (result.source, result.data) == (AnswerSource.STALE, "old")


def read_with(cache: DeckCache[str], fetch: Fetches, *, refresh: bool = False) -> Answer[str]:
    return cached_answer(
        7,
        REQUEST,
        fetch,
        cache,
        now=lambda: NOW,
        refresh=refresh,
        fresh_for=FRESH_FOR,
        errors=(ConnectionError, NotFetchedError),
    )


def cache_only() -> Fetches:
    return Fetches(NotFetchedError(NOT_FETCHED))


def test_a_cache_only_read_after_a_failed_fetch_says_why_it_failed(tmp_path: Path) -> None:
    cache = cache_in(tmp_path)
    read_with(cache, Fetches(ConnectionError("service refused the query")))
    read = read_with(cache, cache_only())
    assert (read.source, read.data, read.error) == (
        AnswerSource.UNAVAILABLE,
        None,
        "service refused the query",
    )


def test_a_cache_only_read_after_a_failed_fetch_keeps_the_old_answer_and_says_why(tmp_path: Path) -> None:
    cache = cache_in(tmp_path)
    save_fetched(cache, timedelta(days=3))
    read_with(cache, Fetches(ConnectionError("down")))
    read = read_with(cache, cache_only())
    assert (read.source, read.data, read.error) == (AnswerSource.STALE, "old", "down")


def test_a_cache_only_read_with_no_failed_fetch_says_not_fetched_yet(tmp_path: Path) -> None:
    read = read_with(cache_in(tmp_path), cache_only())
    assert (read.source, read.error) == (AnswerSource.UNAVAILABLE, "not fetched yet")


def test_a_later_good_fetch_forgets_the_failure(tmp_path: Path) -> None:
    cache = cache_in(tmp_path)
    read_with(cache, Fetches(ConnectionError("down")))
    read_with(cache, Fetches("new"))
    assert read_with(cache, cache_only(), refresh=True).error == "not fetched yet"


def test_a_failure_belongs_to_its_own_cache_folder(tmp_path: Path) -> None:
    read_with(cache_in(tmp_path / "one"), Fetches(ConnectionError("down")))
    assert read_with(cache_in(tmp_path / "two"), cache_only()).error == "not fetched yet"


def test_a_cache_only_miss_is_logged_at_debug_and_a_failed_fetch_at_info(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cache = cache_in(tmp_path)
    with caplog.at_level(logging.INFO, logger="commander.deckcache"):
        read_with(cache, cache_only())
        read_with(cache, cache_only())
        assert caplog.messages == []
        read_with(cache, Fetches(ConnectionError("down")))
        assert caplog.messages == ["test answer for deck 7 failed: down"]
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="commander.deckcache"):
        read_with(cache, cache_only())
    assert caplog.messages == ["test answer for deck 7 not read from the cache: down"]


def test_the_newest_of_an_answer_and_its_accepted_ones_stands_and_a_fetch_leaves_the_accepted_in_place(
    tmp_path: Path,
) -> None:
    cache = cache_in(tmp_path)
    wide: Mapping[str, object] = {"q": 2}
    narrow: Mapping[str, object] = {"q": 3}
    cache.save("own", request_key(wide), "wide answer", NOW - timedelta(hours=2))
    cache.save("alike", request_key(narrow), "alike answer", NOW - timedelta(hours=30))

    def read(fetch: Fetches) -> Answer[str]:
        return cached_answer(
            "alike",
            narrow,
            fetch,
            cache,
            now=lambda: NOW,
            refresh=False,
            fresh_for=FRESH_FOR,
            errors=(ConnectionError,),
            also_accept=[Accepted("own", wide)],
        )

    served = read(Fetches("never"))
    assert (served.data, served.source) == ("wide answer", AnswerSource.CACHED)

    # Both go stale: the newer stands in when the fetch fails, and a good fetch is stored under the own item.
    cache.save("own", request_key(wide), "wide answer", NOW - timedelta(hours=26))
    assert read(Fetches(ConnectionError("down"))).data == "wide answer"
    assert read(Fetches("new alike")).source is AnswerSource.FETCHED
    kept = cache.load("own", request_key(wide))
    assert kept is not None
    assert kept.data == "wide answer"
    stored = cache.load("alike", request_key(narrow))
    assert stored is not None
    assert stored.data == "new alike"
