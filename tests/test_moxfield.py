from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from commander.deckcache import NOT_FETCHED, Answer, AnswerSource
from commander.deckreads import DeckRead, DeckReads
from commander.jsonshape import ShapeError
from commander.metadata import PeerList, PeerLists, joined_peers
from commander.moxfield import (
    MoxfieldBlockedError,
    MoxfieldError,
    MoxfieldSearch,
    parse_card_id,
    parse_deck,
    parse_search_page,
)
from commander.net import Fetcher, Pacer
from commander.peersearch import PeerDeck, Site
from conftest import FakeMoxfield, load_fixture, moxfield_deck, moxfield_page, moxfield_row

WINOTA = "Winota, Joiner of Forces"
SINCE = datetime(2026, 8, 16, tzinfo=UTC)
IN_WINDOW = "2026-10-01T10:00:00.5Z"
BEFORE_WINDOW = "2026-08-15T23:59:59Z"
FETCHED_AT = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def search_for(fake: FakeMoxfield, sleeps: list[float] | None = None) -> MoxfieldSearch:
    clock = [0.0]

    def sleep(seconds: float) -> None:
        (sleeps if sleeps is not None else []).append(seconds)
        clock[0] += seconds

    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return MoxfieldSearch(
        Fetcher(client, max_attempts=1), Pacer(2.0, sleep=sleep, monotonic=lambda: clock[0])
    )


def test_a_search_page_lists_its_public_commander_decks_with_views_update_and_bracket() -> None:
    decks, more = parse_search_page(load_fixture("moxfield_search.json"))
    assert [(d.id, d.views, d.bracket, d.site) for d in decks] == [
        ("Dr4JXq5D7kSBufwF8krxqA", 3740, 3, Site.MOXFIELD),
        ("qrfEU-QVOkC0N95RPAEFMA", 3358, 3, Site.MOXFIELD),
        ("Ql2cDaiJuEqc9u-pMDrV4Q", 3237, 3, Site.MOXFIELD),
    ]
    assert decks[0].updated_at == datetime(2025, 4, 10, 6, 38, 18, 583000, tzinfo=UTC)
    assert decks[0].owner == "moxfield-player-1"
    assert decks[0].url == "https://moxfield.com/decks/Dr4JXq5D7kSBufwF8krxqA"
    # Page 1 of 258.
    assert more


def test_a_search_page_leaves_out_decks_that_are_not_public_commander_ones() -> None:
    page = load_fixture("moxfield_search.json")
    page["data"][0]["visibility"] = "unlisted"
    page["data"][1]["format"] = "brawl"
    page["pageNumber"] = page["totalPages"]
    decks, more = parse_search_page(page)
    assert [d.id for d in decks] == ["Ql2cDaiJuEqc9u-pMDrV4Q"]
    assert not more


def test_a_search_page_without_view_counts_is_refused() -> None:
    page = load_fixture("moxfield_search.json")
    del page["data"][2]["viewCount"]
    with pytest.raises(ShapeError, match=r"page\.data\[2\]"):
        parse_search_page(page)


def test_a_deck_reads_its_commanders_main_cards_and_counts() -> None:
    read = parse_deck(load_fixture("moxfield_deck.json"))
    assert read.commanders == (WINOTA,)
    assert read.cards == (
        "Esper Sentinel",
        "Fable of the Mirror-Breaker // Reflection of Kiki-Jiki",
        "Legion's Landing // Adanto, the First Fort",
        "Snow-Covered Mountain",
        "Snow-Covered Plains",
        "Sol Ring",
        WINOTA,
    )
    assert read.multiples == (("Snow-Covered Mountain", 10), ("Snow-Covered Plains", 10))
    # Its one author tag is "ramp", which names no plan.
    assert read.planned == ()


def test_a_deck_card_its_author_tags_as_a_combo_piece_is_planned() -> None:
    raw = load_fixture("moxfield_deck.json")
    raw["authorTags"]["Esper Sentinel"] = ["Combo Pieces"]
    assert parse_deck(raw).planned == ("Esper Sentinel",)


def test_a_deck_of_another_format_is_refused() -> None:
    raw = load_fixture("moxfield_deck.json")
    raw["format"] = "modern"
    with pytest.raises(ShapeError, match="not a Commander deck"):
        parse_deck(raw)


def test_the_card_search_gives_the_exact_matchs_id_only_for_the_name_asked() -> None:
    raw = load_fixture("moxfield_card_search.json")
    assert parse_card_id(raw, {WINOTA.casefold()}) == "Er9bV"
    assert parse_card_id(raw, {"winota"}) is None
    assert parse_card_id({"hasMore": False, "totalCards": 0}, {WINOTA.casefold()}) is None


def test_a_double_faced_commander_is_looked_up_by_its_front_face() -> None:
    fake = FakeMoxfield()
    fake.cards["esika, queen of myriads"] = "Ek1"
    found = search_for(fake).commander_card_id("Esika, Queen of Myriads // The Prismatic Bridge")
    assert found == "Ek1"
    assert fake.requests[0].params["q"] == '!"Esika, Queen of Myriads"'


def test_each_bracket_is_searched_by_views_and_pages_until_the_last() -> None:
    fake = FakeMoxfield()
    fake.pages["W1"] = [
        moxfield_page([moxfield_row("a", 3000, IN_WINDOW)], 1, 2),
        moxfield_page([moxfield_row("b", 2000, IN_WINDOW), moxfield_row("old", 1500, BEFORE_WINDOW)], 2, 2),
    ]
    sleeps: list[float] = []
    decks = search_for(fake, sleeps).popular_decks("W1", brackets=(3, 4), since=SINCE, label="winota")
    # Two pages for each bracket (the fake answers both alike); the deck updated before the window is out.
    assert sorted(d.id for d in decks) == ["a", "b"]
    searched = [
        (r.params["minBracket"], r.params["maxBracket"], r.params["pageNumber"]) for r in fake.requests
    ]
    assert searched == [("3", "3", "1"), ("3", "3", "2"), ("4", "4", "1"), ("4", "4", "2")]
    first = fake.requests[0].params
    assert (first["sortType"], first["sortDirection"], first["fmt"]) == ("views", "Descending", "commander")
    assert first["commanderCardId"] == "W1"
    assert first["pageSize"] == "60"
    # One request every 2 seconds of this pacer: the first goes at once.
    assert sleeps == [2.0, 2.0, 2.0]


def test_a_search_of_any_bracket_sends_no_bracket() -> None:
    fake = FakeMoxfield()
    search_for(fake).popular_decks("W1", brackets=(None,), since=SINCE, label="winota")
    assert "minBracket" not in fake.requests[0].params


def test_a_deck_is_read_by_its_public_id() -> None:
    fake = FakeMoxfield()
    fake.decks["abc-D_1"] = moxfield_deck("abc-D_1", [WINOTA], {"Sol Ring": 1, "Mountain": 2})
    read = search_for(fake).deck("abc-D_1")
    assert read == DeckRead((WINOTA,), ("Mountain", "Sol Ring", WINOTA), (), (("Mountain", 2),))
    assert fake.requests[0].path == "/v3/decks/all/abc-D_1"


def test_a_deck_moxfield_answers_oddly_for_is_a_moxfield_error() -> None:
    fake = FakeMoxfield()
    fake.decks["odd"] = {"format": "commander"}
    with pytest.raises(MoxfieldError, match="odd"):
        search_for(fake).deck("odd")


def test_a_403_stops_every_later_request_of_the_run() -> None:
    fake = FakeMoxfield()
    fake.status = 403
    search = search_for(fake)
    with pytest.raises(MoxfieldBlockedError, match="HTTP 403"):
        search.commander_card_id(WINOTA)
    fake.status = 200
    with pytest.raises(MoxfieldBlockedError):
        search.deck("abc")
    assert len(fake.requests) == 1


def test_a_403_is_no_moxfield_error_so_skipping_an_unreadable_deck_never_skips_a_block() -> None:
    assert not issubclass(MoxfieldBlockedError, MoxfieldError)


def test_a_peer_deck_id_must_be_a_number_on_archidekt_and_text_on_moxfield() -> None:
    with pytest.raises(ValueError, match="wrong kind"):
        PeerDeck("12", "x", "o", 1, SINCE, 3)
    with pytest.raises(ValueError, match="wrong kind"):
        PeerDeck(12, "x", "o", 1, SINCE, 3, Site.MOXFIELD)
    with pytest.raises(ValueError, match="wrong kind"):
        PeerDeck("", "x", "o", 1, SINCE, 3, Site.MOXFIELD)
    assert PeerDeck(12, "x", "o", 1, SINCE, 3).url == "https://archidekt.com/decks/12"


def test_a_moxfield_deck_read_is_cached_under_a_safe_name_apart_from_archidekt_ones(tmp_path: Path) -> None:
    reads = DeckReads(tmp_path)
    mixed_case = PeerDeck("AbC-d_E", "x", "o", 1, SINCE, 3, Site.MOXFIELD)
    other_case = PeerDeck("abc-d_e", "x", "o", 1, SINCE, 3, Site.MOXFIELD)
    read = DeckRead((WINOTA,), (WINOTA,), (), ())
    reads.save(mixed_case, read, FETCHED_AT)
    assert reads.load(mixed_case, FETCHED_AT) == read
    # Ids that differ only in case are other decks.
    assert reads.load(other_case, FETCHED_AT) is None
    assert [p.name.startswith("moxfield-abc-d-e-") for p in tmp_path.iterdir()] == [True]


def peer(site: Site, deck_id: int | str, cards: tuple[str, ...], views: int = 900) -> PeerList:
    deck = PeerDeck(deck_id, "x", "someone", views, SINCE, 3, site)
    return PeerList(deck, cards, (), (("Mountain", 30),), (WINOTA,))


def answer(*decks: PeerList, source: AnswerSource = AnswerSource.FETCHED) -> Answer[PeerLists]:
    return Answer(PeerLists(decks, 0), source, FETCHED_AT, "old" if source is AnswerSource.STALE else None)


def unavailable(why: str) -> Answer[PeerLists]:
    return Answer(None, AnswerSource.UNAVAILABLE, None, why)


def test_a_list_cross_posted_to_both_sites_counts_once() -> None:
    same = (WINOTA, "Sol Ring", "Mountain")
    joined = joined_peers(
        answer(peer(Site.ARCHIDEKT, 1, same)),
        answer(peer(Site.MOXFIELD, "m1", same), peer(Site.MOXFIELD, "m2", (WINOTA, "Mana Crypt"))),
    )
    assert joined.data is not None
    assert [(p.deck.site, p.deck.id) for p in joined.data.decks] == [
        (Site.ARCHIDEKT, 1),
        (Site.MOXFIELD, "m2"),
    ]
    assert joined.source is AnswerSource.FETCHED
    assert joined.data.missing == ()


def test_a_list_with_other_counts_on_moxfield_is_another_list() -> None:
    same = (WINOTA, "Sol Ring", "Mountain")
    other = PeerList(PeerDeck("m1", "x", "o", 900, SINCE, 3, Site.MOXFIELD), same, (), (("Mountain", 29),))
    joined = joined_peers(answer(peer(Site.ARCHIDEKT, 1, same)), answer(other))
    assert joined.data is not None
    assert len(joined.data.decks) == 2


def test_a_site_that_cannot_be_read_is_named_while_the_other_stands_in() -> None:
    joined = joined_peers(answer(peer(Site.ARCHIDEKT, 1, (WINOTA,))), unavailable("refused"))
    assert joined.data is not None
    assert [p.deck.id for p in joined.data.decks] == [1]
    assert joined.data.missing == ((Site.MOXFIELD, "refused"),)
    assert joined.source is AnswerSource.STALE
    assert joined.error == "Moxfield: refused"


def test_moxfield_decks_stand_in_when_archidekt_cannot_be_read() -> None:
    joined = joined_peers(unavailable("429"), answer(peer(Site.MOXFIELD, "m1", (WINOTA,))))
    assert joined.data is not None
    assert [p.deck.id for p in joined.data.decks] == ["m1"]
    assert joined.data.missing == ((Site.ARCHIDEKT, "429"),)


def test_one_site_unread_and_the_other_without_decks_is_no_answer() -> None:
    joined = joined_peers(unavailable("429"), answer())
    assert joined.data is None
    assert joined.source is AnswerSource.UNAVAILABLE
    assert joined.error == "Archidekt: 429"


def test_neither_site_read_is_no_answer_naming_both() -> None:
    joined = joined_peers(unavailable("429"), unavailable("refused"))
    assert joined.data is None
    assert joined.error == "Archidekt: 429; Moxfield: refused"


def test_a_stale_site_makes_the_joined_answer_stale_and_as_old_as_the_older() -> None:
    older = Answer(PeerLists((), 1), AnswerSource.STALE, datetime(2026, 10, 1, tzinfo=UTC), "down")
    joined = joined_peers(answer(peer(Site.ARCHIDEKT, 1, (WINOTA,))), older)
    assert joined.source is AnswerSource.STALE
    assert joined.fetched_at == datetime(2026, 10, 1, tzinfo=UTC)
    assert joined.data is not None
    assert joined.data.skipped == 1
    assert joined.data.missing == ()


def test_both_sites_cached_is_cached() -> None:
    joined = joined_peers(
        answer(source=AnswerSource.CACHED), answer(peer(Site.MOXFIELD, "m", ()), source=AnswerSource.CACHED)
    )
    assert joined.source is AnswerSource.CACHED


def test_a_cache_only_read_without_a_stored_moxfield_answer_leaves_archidekts_as_it_is() -> None:
    archidekt = answer(peer(Site.ARCHIDEKT, 1, (WINOTA,)))
    assert joined_peers(archidekt, unavailable(NOT_FETCHED)) is archidekt
    assert joined_peers(archidekt, None) is archidekt
