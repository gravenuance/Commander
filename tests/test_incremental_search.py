"""An Archidekt search lists decks newest-updated first, so a refresh pages only as far as its cached hits
(ADR 0027)."""

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from commander.archidekt import ArchidektSearch
from commander.metadata import CACHE_FOLDER, AdjacentDecks, gather_adjacent
from commander.net import Fetcher, Pacer
from conftest import Json
from test_metadata import ELLIVERE, NOW, SETTINGS, FakeArchidektSearch, Rig, ellivere_deck

SINCE = datetime(2026, 8, 16, tzinfo=NOW.tzinfo)


def listing_deck(number: int, days_ago: float, *, views: int = 500, bracket: int = 5) -> Json:
    """A search result for deck 1000000<number>, updated that long before the test's NOW."""
    return {
        "id": 10_000_000 + number,
        "name": f"Deck {number}",
        "owner": {"id": 777_000 + number, "username": f"player{number}"},
        "viewCount": views,
        "updatedAt": (NOW - timedelta(days=days_ago)).isoformat(),
        "createdAt": "2024-11-13T23:50:07.696146Z",
        "edhBracket": bracket,
        "deckFormat": 3,
        "size": 100,
        "private": False,
        "unlisted": False,
        "theorycrafted": False,
    }


class PagedArchidekt(FakeArchidektSearch):
    """A deck listing sorted like `orderBy=-updatedAt`, served a few decks to a page with a next link, as
    Archidekt does; the deck reads are the base fake's."""

    def __init__(self, page_size: int) -> None:
        super().__init__()
        self.listing: list[Json] = []
        self.page_size = page_size
        # The bracket filter ("5", or None) and number of each search page asked for.
        self.pages: list[tuple[str | None, int]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/decks/v3/":
            return super().handler(request)
        self.requests.append(request.url.path)
        query = dict(request.url.params)
        self.searches.append(query)
        bracket = query.get("edhBracket")
        page = int(query.get("page", "1"))
        self.pages.append((bracket, page))
        rows = sorted(
            (d for d in self.listing if bracket is None or str(d["edhBracket"]) == bracket),
            key=lambda d: datetime.fromisoformat(d["updatedAt"]),
            reverse=True,
        )
        shown = rows[(page - 1) * self.page_size : page * self.page_size]
        more = page * self.page_size < len(rows)
        link = f"http://archidekt.com/api/decks/v3/?formats=3&orderBy=-updatedAt&page={page + 1}" + (
            f"&edhBracket={bracket}" if bracket else ""
        )
        body = {"count": len(rows), "next": link if more else None, "results": shown}
        return httpx.Response(200, json=body)


def paged_rig(tmp_path: Path, listing: list[Json], *, page_size: int = 4) -> Rig:
    rig = Rig(tmp_path)
    paged = PagedArchidekt(page_size)
    paged.listing = listing
    rig.archidekt = paged
    return rig


def paged(rig: Rig) -> PagedArchidekt:
    assert isinstance(rig.archidekt, PagedArchidekt)
    return rig.archidekt


def sync(rig: Rig, *, bracket: int = 5) -> AdjacentDecks:
    deck = replace(ellivere_deck(), bracket=bracket)
    return gather_adjacent(deck, ((ELLIVERE,),), rig.sources(), rig.cache, now=lambda: rig.now, refresh=False)


def sync_tag(rig: Rig, tag: str) -> AdjacentDecks:
    return gather_adjacent(
        ellivere_deck(), (), rig.sources(), rig.cache, now=lambda: rig.now, refresh=False, tags=(tag,)
    )


def asked_since(rig: Rig, mark: int) -> list[tuple[str | None, int]]:
    return paged(rig).pages[mark:]


def stored_search(rig: Rig, prefix: str) -> Json:
    [path] = (rig.cache / CACHE_FOLDER).glob(f"{prefix}*.json")
    answer: Json = json.loads(path.read_text(encoding="utf-8"))["answer"]["search"]
    return answer


def hits_by_id(rig: Rig, prefix: str) -> dict[int, tuple[int, str]]:
    """Each stored hit's views and update time by deck id."""
    return {d["id"]: (d["views"], d["updatedAt"]) for d in stored_search(rig, prefix)["decks"]}


def twelve_decks_and_four_old_ones() -> list[Json]:
    # Decks 11 and 12 fall out of the 60 day window within three days; 13 and 14 are already out of it.
    days = (1, 6, 11, 16, 21, 26, 31, 36, 41, 46, 58, 59, 70, 75)
    return [listing_deck(n, ago) for n, ago in enumerate(days, 1)]


# --- ArchidektSearch.recent_decks ---


def search_over(fake: PagedArchidekt) -> ArchidektSearch:
    fetcher = Fetcher(httpx.Client(transport=httpx.MockTransport(fake.handler)), max_attempts=1)
    return ArchidektSearch(fetcher, Pacer(0))


def test_a_search_given_a_stop_pages_until_a_deck_at_or_older_than_it() -> None:
    fake = PagedArchidekt(4)
    fake.listing = [listing_deck(n, n) for n in range(1, 11)]
    search = search_over(fake)
    # Deck 6 was updated 6 days before NOW and sits on the second page of four.
    decks = search.recent_decks(ELLIVERE, brackets=(5,), since=SINCE, stop_at={5: NOW - timedelta(days=6)})
    assert fake.pages == [("5", 1), ("5", 2)]
    assert [d.id for d in decks] == [10_000_000 + n for n in range(1, 9)]


def test_a_search_without_a_stop_pages_to_the_end_of_the_window() -> None:
    fake = PagedArchidekt(4)
    fake.listing = [listing_deck(n, n) for n in range(1, 11)]
    decks = search_over(fake).recent_decks(ELLIVERE, brackets=(5,), since=SINCE)
    assert fake.pages == [("5", 1), ("5", 2), ("5", 3)]
    assert len(decks) == 10


def test_each_bracket_stops_at_its_own_deck() -> None:
    fake = PagedArchidekt(2)
    fake.listing = [
        *(listing_deck(n, n, bracket=4) for n in (1, 2, 3, 4)),
        *(listing_deck(n, n, bracket=5) for n in (5, 6, 7, 8)),
    ]
    stops: dict[int | None, datetime] = {4: NOW - timedelta(days=2), 5: NOW - timedelta(days=7)}
    decks = search_over(fake).recent_decks(ELLIVERE, brackets=(4, 5), since=SINCE, stop_at=stops)
    assert fake.pages == [("4", 1), ("5", 1), ("5", 2)]
    assert [d.id - 10_000_000 for d in decks] == [1, 2, 5, 6, 7, 8]


# --- the cached hits of a group ---


def test_a_refresh_pages_only_as_far_as_the_cached_hits_and_gives_what_a_full_search_would(
    tmp_path: Path,
) -> None:
    rig = paged_rig(tmp_path / "incremental", twelve_decks_and_four_old_ones())
    sync(rig)
    assert paged(rig).pages == [("5", 1), ("5", 2), ("5", 3), ("5", 4)]
    # Three days on: two new decks, and deck 3 edited (with more views); decks 11 and 12 leave the window.
    rig.now = NOW + timedelta(days=3)
    listing = [d for d in twelve_decks_and_four_old_ones() if d["id"] != 10_000_003]
    listing += [
        listing_deck(3, -2.5, views=900),
        listing_deck(15, -2),
        listing_deck(16, -1),
    ]
    paged(rig).listing = listing
    mark = len(paged(rig).pages)
    sync(rig)
    assert asked_since(rig, mark) == [("5", 1)]
    expected_ids = [10_000_000 + n for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 16)]
    incremental = hits_by_id(rig, "archidekt-ellivere")
    assert sorted(incremental) == expected_ids
    assert incremental[10_000_003][0] == 900

    full = paged_rig(tmp_path / "full", listing)
    full.now = rig.now
    sync(full)
    assert len(paged(full).pages) == 4
    assert incremental == hits_by_id(full, "archidekt-ellivere")


def test_a_bracket_4_deck_finds_new_bracket_5_decks_older_than_its_newest_bracket_4_one(
    tmp_path: Path,
) -> None:
    listing = [listing_deck(1, 1, bracket=4), listing_deck(2, 10, bracket=5), listing_deck(3, 20, bracket=5)]
    rig = paged_rig(tmp_path, listing, page_size=1)
    sync(rig, bracket=4)
    rig.now = NOW + timedelta(days=3)
    # Decks 4 and 5 are newer than the bracket 5 hits but older than the bracket 4 one, so a stop at the
    # newest hit of any bracket would end the bracket 5 search after deck 4.
    paged(rig).listing = [*listing, listing_deck(4, 5, bracket=5), listing_deck(5, 6, bracket=5)]
    sync(rig, bracket=4)
    assert sorted(hits_by_id(rig, "archidekt-ellivere")) == [10_000_000 + n for n in (1, 2, 3, 4, 5)]


def test_a_group_is_paged_in_full_once_a_week_and_otherwise_to_its_newest_hit(tmp_path: Path) -> None:
    # Six decks two to a page, then a deck far outside the window: a full search takes four pages.
    listing = [*(listing_deck(n, n) for n in range(1, 7)), listing_deck(7, 200), listing_deck(8, 210)]
    rig = paged_rig(tmp_path, listing, page_size=2)
    sync(rig)
    pages_each_day: dict[int, int] = {}
    for day in range(1, 14):
        rig.now = NOW + timedelta(days=day)
        mark = len(paged(rig).pages)
        sync(rig)
        pages_each_day[day] = len(asked_since(rig, mark))
    # Ellivere at bracket 5 is refreshed on days 1, 4, 7, 10 and 13, and its full-search days fall on the
    # 14th of October, the 21st (day 6) and the 28th (day 13): the first refresh after each pages in full.
    assert {day: pages for day, pages in pages_each_day.items() if pages} == {
        1: 1,
        4: 1,
        7: 4,
        10: 1,
        13: 4,
    }


def test_a_tag_search_is_refreshed_incrementally_too(tmp_path: Path) -> None:
    rig = paged_rig(tmp_path, [listing_deck(n, 3 * n) for n in range(1, 10)])
    sync_tag(rig, "Tokens")
    # Nine decks, four to a page.
    assert paged(rig).pages == [("5", 1), ("5", 2), ("5", 3)]
    rig.now = NOW + timedelta(days=3)
    paged(rig).listing = [*paged(rig).listing, listing_deck(10, -1)]
    mark = len(paged(rig).pages)
    sync_tag(rig, "Tokens")
    assert asked_since(rig, mark) == [("5", 1)]
    assert 10_000_010 in hits_by_id(rig, "archidekt-tag-tokens")


def as_written_before_cached_hits(rig: Rig) -> None:
    """Rewrite the stored Ellivere answer the way the version before the cached hits wrote it."""
    [path] = (rig.cache / CACHE_FOLDER).glob("archidekt-ellivere*.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["version"] = 2
    del raw["answer"]["search"]
    path.write_text(json.dumps(raw), encoding="utf-8")


def test_a_cache_from_before_cached_hits_still_answers_offline(tmp_path: Path) -> None:
    rig = paged_rig(tmp_path, twelve_decks_and_four_old_ones())
    first = sync(rig)
    as_written_before_cached_hits(rig)
    offline = gather_adjacent(
        replace(ellivere_deck(), bracket=5),
        ((ELLIVERE,),),
        rig.sources(),
        rig.cache,
        now=lambda: rig.now,
        refresh=False,
        offline=True,
    )
    assert offline.errors == ()
    assert offline.decks
    assert offline.decks == first.decks


def test_a_cache_from_before_cached_hits_is_searched_in_full(tmp_path: Path) -> None:
    rig = paged_rig(tmp_path, twelve_decks_and_four_old_ones())
    sync(rig)
    as_written_before_cached_hits(rig)
    rig.now = NOW + timedelta(days=3)
    mark = len(paged(rig).pages)
    sync(rig)
    assert len(asked_since(rig, mark)) == 3
    assert len(hits_by_id(rig, "archidekt-ellivere")) == 10


def test_hits_cached_for_another_request_are_not_reused(tmp_path: Path) -> None:
    rig = paged_rig(tmp_path, twelve_decks_and_four_old_ones())
    sync(rig)
    rig.settings = replace(SETTINGS, window_days=30)
    mark = len(paged(rig).pages)
    sync(rig)
    # The 30 day window is another question, so the cached answer is not the one to extend: the search runs
    # to the first deck past 30 days (deck 7, on the second page).
    assert asked_since(rig, mark) == [("5", 1), ("5", 2)]


@pytest.mark.parametrize("refresh", [False, True])
def test_a_run_that_changes_nothing_still_stops_at_the_first_page(tmp_path: Path, refresh: bool) -> None:  # noqa: FBT001 - a parametrized flag
    rig = paged_rig(tmp_path, twelve_decks_and_four_old_ones())
    sync(rig)
    rig.now = NOW + timedelta(days=3)
    mark = len(paged(rig).pages)
    gather_adjacent(
        ellivere_deck(), ((ELLIVERE,),), rig.sources(), rig.cache, now=lambda: rig.now, refresh=refresh
    )
    assert asked_since(rig, mark) == [("5", 1)]
