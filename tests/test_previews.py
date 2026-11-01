"""Scryfall's card previews (ADR 0055): the sync's daily probe, its cache and the previews fitting a deck."""

import copy
import json
import logging
import shutil
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from commander import board as board_module
from commander import desk as desk_module
from commander import previews as previews_module
from commander import syncrun
from commander.board import Actor, Status, read_activity, read_board
from commander.carddb import CARD_DB_FILE, CardDb, Role
from commander.config import MetaSettings
from commander.deckprofile import Profile, ThemeSpec
from commander.desk import Desk, DeskSettings
from commander.models import COMMANDER_CATEGORY, Deck
from commander.net import Fetcher, Pacer
from commander.previews import (
    PREVIEWS_FILE,
    PreviewCache,
    PreviewFit,
    PreviewProbe,
    ProbeState,
    fitting_previews,
    probe_previews,
    read_previews,
    write_previews,
)
from commander.scryfall import (
    CardRefresh,
    ScryfallClient,
    SetCard,
    ShapeError,
    parse_search_page,
    refresh_card_db,
)
from commander.store import Workspace
from commander.suggest import DEFAULT_TARGETS
from commander.syncrun import SyncOptions, run_sync
from conftest import (
    BATTERED_GOLEM,
    MAGDA_ORACLE,
    TAGS_URL,
    FakeArchidekt,
    FakeScryfall,
    Json,
    MemoryTokenStore,
    build_card_db,
    extra_tag_line,
    gzip_lines,
    load_fixture,
    tag_lines,
)
from test_cli_apply import run
from test_ideas import themed_deck
from test_meta import deck
from test_syncrun import DOWNLOADED

__all__ = ["deck"]  # the fixture shared with test_meta

SEARCH_URL = "https://api.scryfall.com/cards/search?q=e%3Atrk&unique=cards"
BULK_URL = "https://api.scryfall.com/bulk-data"
# Scryfall's own form of a next page link, as its answers give it.
PAGE_2 = (
    "https://api.scryfall.com/cards/search?format=json&include_extras=false&include_multilingual=false"
    "&include_variations=false&order=name&page=2&q=e%3Atrk&unique=cards"
)
# Star Trek (trk) is out on 2026-11-13: on this day the set list fixture's only upcoming set with cards.
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
TODAY = date(2026, 10, 22)
RELEASE_DAY = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)

# The fixture's cards, by oracle id.
TUTELAGE = "1d41aacf-e15d-4457-b12a-c71bcdb7b5de"  # Captain's Tutelage, red, Flash
TELEPATH = "8656291d-8de7-4341-b1ee-0fcd65d629a4"  # Cloistered Telepath, blue, Flash
DAHAR = "c9bc1a28-8d3c-4d9f-a67d-b044cfe9d6c1"  # Dahar Master, red
KIRK = "e85ad29a-ebd6-4f81-af99-556deaedbfe0"  # Kirk, Commanding Officer, red and white
MUNITIONS = "0013f005-2b49-48a4-8a74-9bbdadc88c9f"  # Munitions Enthusiast, red
QUARK = "61703e94-cb57-44d8-b37b-7b14b3cfd86d"  # Quark, Humble Proprietor, red
IMPULSE = "584cf0fd-112a-4ca6-9c0e-1f3228a7d325"  # Reckless Impulse, a reprint already legal
RAMP_TAG = "2f3e4ad7-5e60-41b4-bdbc-653f16869cf6"
# Made up for these tests: the tag a Treasure deck tag stands for, on most red previews, the reprint, the
# red-white Kirk and the deck's Battered Golem; and Munitions Enthusiast as ramp, a role the small test decks
# run short of.
TREASURE_TAG = json.dumps(
    {
        "object": "tag",
        "id": "aaaaaaaa-0000-4000-8000-0000000000aa",
        "slug": "synergy-treasure",
        "type": "oracle",
        "child_ids": [],
        "taggings": [
            {"oracle_id": o} for o in (TUTELAGE, DAHAR, KIRK, MUNITIONS, QUARK, IMPULSE, BATTERED_GOLEM)
        ],
    }
)


def ramp_with_munitions() -> str:
    ramp = next(json.loads(line) for line in tag_lines() if json.loads(line)["id"] == RAMP_TAG)
    ramp["taggings"].append({"oracle_id": MUNITIONS})
    return json.dumps(ramp)


@pytest.fixture(scope="module")
def previews_db_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_card_db(tmp_path_factory.mktemp("previewsdb"), [TREASURE_TAG, ramp_with_munitions()])


@pytest.fixture(scope="module")
def commander_db_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The same with Magda, the commander, made a synergy-low-power commander."""
    tags = [TREASURE_TAG, ramp_with_munitions(), extra_tag_line("synergy-low-power", MAGDA_ORACLE)]
    return build_card_db(tmp_path_factory.mktemp("commanderdb"), tags)


@pytest.fixture
def commander_db(commander_db_file: Path) -> Iterator[CardDb]:
    with CardDb(commander_db_file) as opened:
        yield opened


@pytest.fixture(scope="module")
def tokens_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CardDb]:
    """The same with two made-up tags: repeatable-creature-tokens on the deck's Golem, Dahar Master and Quark,
    and anthem on Quark."""
    tags = [
        TREASURE_TAG,
        ramp_with_munitions(),
        extra_tag_line("repeatable-creature-tokens", BATTERED_GOLEM, DAHAR, QUARK),
        extra_tag_line("anthem", QUARK),
    ]
    with CardDb(build_card_db(tmp_path_factory.mktemp("tokensdb"), tags)) as opened:
        yield opened


@pytest.fixture(autouse=True)
def utc_is_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local day is the UTC day here, so the tests read the same in any time zone."""

    def utc_day(moment: datetime) -> date:
        return moment.astimezone(UTC).date()

    monkeypatch.setattr(previews_module, "local_day", utc_day)
    monkeypatch.setattr(desk_module, "local_day", utc_day)


@pytest.fixture
def db(previews_db_file: Path) -> Iterator[CardDb]:
    with CardDb(previews_db_file) as opened:
        yield opened


def search_page() -> Json:
    """A trimmed real answer of Scryfall's card search for Star Trek's previews, on one page."""
    return load_fixture("scryfall_search_trk.json")


def two_pages() -> tuple[Json, Json]:
    whole = search_page()
    first, second = copy.deepcopy(whole), copy.deepcopy(whole)
    first["data"], second["data"] = whole["data"][:5], whole["data"][5:]
    first.update(has_more=True, next_page=PAGE_2)
    return first, second


class CountingPacer(Pacer):
    def __init__(self) -> None:
        super().__init__(0)
        self.waits = 0

    def wait(self) -> None:
        self.waits += 1
        super().wait()


def probe(
    fake: FakeScryfall, folder: Path, now: datetime = NOW, pacer: Pacer | None = None
) -> previews_module.PreviewProbe:
    client = ScryfallClient(Fetcher(fake.client()))
    return probe_previews(client, folder, now=lambda: now, pacer=pacer or Pacer(0))


def cached_names(folder: Path) -> list[str]:
    return sorted(card.name for card in read_previews(folder).cards)


# --- the search answer


def test_a_search_page_reads_the_real_answer_and_knows_a_reprint_from_a_preview() -> None:
    page = parse_search_page(search_page())
    by_name = {card.name: card for card in page.cards}
    assert page.next_page is None
    assert len(by_name) == 9
    tutelage = by_name["Captain's Tutelage"]
    assert (tutelage.oracle_id, tutelage.set_code, tutelage.set_name) == (TUTELAGE, "trk", "Star Trek")
    assert (tutelage.released_at, tutelage.color_identity, tutelage.already_out) == (
        date(2026, 11, 13),
        "R",
        False,
    )
    assert tutelage.keywords == ("Enchant", "Flash")
    assert tutelage.type_line == "Enchantment — Aura"
    assert by_name["Kirk, Commanding Officer"].color_identity == "WR"
    assert by_name["Reckless Impulse"].already_out


def test_a_preview_keeps_its_mana_value_and_power_through_the_cache(tmp_path: Path) -> None:
    page = parse_search_page(search_page())
    by_name = {card.name: card for card in page.cards}
    shown = [by_name["D-7 Battle Cruiser"], by_name["Quark, Humble Proprietor"], by_name["Reckless Impulse"]]
    assert [(c.mana_value, c.power) for c in shown] == [(2.0, "3"), (3.0, "1"), (2.0, None)]
    write_previews(tmp_path, PreviewCache(TODAY, page.cards))
    assert read_previews(tmp_path).cards == page.cards


def test_a_next_page_that_is_not_scryfalls_search_is_refused() -> None:
    first, _ = two_pages()
    first["next_page"] = "https://example.com/cards/search?page=2"
    with pytest.raises(ShapeError, match="not a Scryfall search URL"):
        parse_search_page(first)


# --- the probe


def test_with_no_upcoming_set_the_probe_searches_nothing_and_empties_the_cache(tmp_path: Path) -> None:
    write_previews(tmp_path, PreviewCache(TODAY, parse_search_page(search_page()).cards))
    fake = FakeScryfall()
    # On Star Trek's release day no set in the list is still to come.
    outcome = probe(fake, tmp_path, now=RELEASE_DAY)
    assert outcome.state is ProbeState.NO_SEASON
    assert fake.requests == ["https://api.scryfall.com/sets"]
    assert outcome.requests == 1
    assert read_previews(tmp_path) == PreviewCache(date(2026, 11, 13), ())


def test_an_upcoming_set_over_two_pages_is_read_whole_paced_and_cached_without_its_reprint(
    tmp_path: Path,
) -> None:
    fake = FakeScryfall()
    first, second = two_pages()
    fake.searches = {SEARCH_URL: first, PAGE_2: second}
    pacer = CountingPacer()
    outcome = probe(fake, tmp_path, pacer=pacer)
    assert (outcome.state, outcome.sets, outcome.cards, outcome.requests) == (
        ProbeState.UPDATED,
        ("trk",),
        8,
        4,
    )
    # Then the bulk file list for today's tags, paced like the search, and the tags file from the file host.
    assert fake.requests == ["https://api.scryfall.com/sets", SEARCH_URL, PAGE_2, BULK_URL, TAGS_URL]
    assert pacer.waits == 3
    assert read_previews(tmp_path).probed_on == TODAY
    assert cached_names(tmp_path) == [
        "Captain's Tutelage",
        "Cloistered Telepath",
        "D-7 Battle Cruiser",
        "Dahar Master",
        "Kirk, Commanding Officer",
        "Munitions Enthusiast",
        "Quark, Humble Proprietor",
        "The Wrath of Khan",
    ]


def test_a_set_past_the_page_limit_keeps_the_pages_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(previews_module, "MAX_PAGES_PER_SET", 1)
    fake = FakeScryfall()
    first, second = two_pages()
    fake.searches = {SEARCH_URL: first, PAGE_2: second}
    with caplog.at_level(logging.WARNING, logger="commander.scryfall"):
        outcome = probe(fake, tmp_path)
    assert (outcome.state, outcome.cards, outcome.requests) == (ProbeState.UPDATED, 5, 3)
    assert PAGE_2 not in fake.requests
    assert "the search for set trk has over 1 pages; the rest is left out" in caplog.messages


def test_the_set_list_is_read_once_for_the_card_data_and_the_probe() -> None:
    fake = FakeScryfall()
    client = ScryfallClient(Fetcher(fake.client()))
    assert client.sets() == client.sets()
    assert fake.requests == ["https://api.scryfall.com/sets"]


def test_the_probe_counts_no_request_for_a_set_list_the_card_data_already_read(tmp_path: Path) -> None:
    fake = FakeScryfall()
    fake.searches = {SEARCH_URL: search_page()}
    client = ScryfallClient(Fetcher(fake.client()))
    client.sets()
    outcome = probe_previews(client, tmp_path, now=lambda: NOW, pacer=Pacer(0))
    assert fake.requests == ["https://api.scryfall.com/sets", SEARCH_URL, BULK_URL, TAGS_URL]
    # The search and the file list.
    assert outcome.requests == 2


def test_the_probe_runs_once_a_day(tmp_path: Path) -> None:
    fake = FakeScryfall()
    fake.searches = {SEARCH_URL: search_page()}
    probe(fake, tmp_path)
    fake.requests.clear()
    assert probe(fake, tmp_path, now=NOW.replace(hour=23)).state is ProbeState.DONE_TODAY
    assert fake.requests == []
    probe(fake, tmp_path, now=datetime(2026, 10, 23, 1, 0, tzinfo=UTC))
    assert fake.requests == ["https://api.scryfall.com/sets", SEARCH_URL, BULK_URL, TAGS_URL]


def test_a_set_scryfall_has_no_cards_for_yet_is_an_empty_set_not_a_failure(tmp_path: Path) -> None:
    fake = FakeScryfall()
    outcome = probe(fake, tmp_path)
    assert (outcome.state, outcome.cards) == (ProbeState.UPDATED, 0)
    assert read_previews(tmp_path) == PreviewCache(TODAY, ())
    # With no previews there are no tags to fetch.
    assert BULK_URL not in fake.requests


def test_the_probe_keeps_each_previews_tags_and_roles_from_todays_tagger_tags(tmp_path: Path) -> None:
    # Munitions Enthusiast is tagged combat-ramp in today's tags, which the card data does not know yet;
    # combat-ramp sits below ramp in the fixture tags, so it is ramp too. The other previews have no tag.
    fake = FakeScryfall()
    fake.searches = {SEARCH_URL: search_page()}
    lines = [json.loads(line) for line in tag_lines()]
    combat = next(tag for tag in lines if tag["slug"] == "combat-ramp")
    combat["taggings"] = [*combat["taggings"], {"oracle_id": MUNITIONS}]
    fake.tags_gz = gzip_lines([*map(json.dumps, lines), extra_tag_line("anthem", QUARK)])
    probe(fake, tmp_path)
    cache = read_previews(tmp_path)
    assert cache.tags == {MUNITIONS: frozenset({"combat-ramp", "ramp"}), QUARK: frozenset({"anthem"})}
    assert cache.roles == {MUNITIONS: frozenset({Role.RAMP})}
    # And a second read of the file gives them back.
    write_previews(tmp_path, cache)
    assert read_previews(tmp_path) == cache


def test_a_failed_tag_download_keeps_the_cards_and_the_last_tags_of_those_still_previews(
    tmp_path: Path,
) -> None:
    fake = FakeScryfall()
    fake.searches = {SEARCH_URL: search_page()}
    # Yesterday's previews: Quark, and a card no longer among them.
    gone = "00000000-0000-4000-8000-000000000001"
    quark = next(c for c in parse_search_page(search_page()).cards if c.oracle_id == QUARK)
    last = PreviewCache(
        date(2026, 10, 21),
        (quark, replace(quark, oracle_id=gone, name="Gone")),
        {QUARK: frozenset({"anthem"}), gone: frozenset({"ramp"})},
        {gone: frozenset({Role.RAMP})},
    )
    write_previews(tmp_path, last)
    fake.failing = {TAGS_URL: 500}
    outcome = probe(fake, tmp_path)
    assert (outcome.state, outcome.cards) == (ProbeState.UPDATED, 8)
    assert "500" in outcome.tag_problem
    cache = read_previews(tmp_path)
    assert len(cache.cards) == 8
    assert (cache.tags, cache.roles) == ({QUARK: frozenset({"anthem"})}, {})


@pytest.mark.parametrize("failure", ["server error", "malformed"])
def test_a_failed_probe_keeps_the_last_previews_and_tries_again_later_the_same_day(
    tmp_path: Path, failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeScryfall()
    fake.searches = {SEARCH_URL: search_page()}
    probe(fake, tmp_path, now=datetime(2026, 10, 21, 12, 0, tzinfo=UTC))
    kept = read_previews(tmp_path)
    if failure == "server error":
        fake.failing[SEARCH_URL] = 503
    else:
        fake.searches[SEARCH_URL] = {"object": "list", "has_more": False, "data": "no list"}
    with caplog.at_level(logging.WARNING, logger="commander.previews"):
        outcome = probe(fake, tmp_path)
    assert outcome.state is ProbeState.FAILED
    assert read_previews(tmp_path) == kept
    assert [
        m for m in caplog.messages if m.startswith("card previews not refreshed, the last ones are kept: ")
    ]
    # Not marked as probed today, so the next sync asks again.
    fake.searches[SEARCH_URL] = search_page()
    fake.failing.clear()
    assert probe(fake, tmp_path).state is ProbeState.UPDATED


def test_a_later_page_that_is_not_found_fails_the_probe_and_keeps_the_last_previews(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeScryfall()
    fake.searches = {SEARCH_URL: search_page()}
    probe(fake, tmp_path, now=datetime(2026, 10, 21, 12, 0, tzinfo=UTC))
    kept = read_previews(tmp_path)
    # Page 2 is not served, so Scryfall answers 404: only an empty first page may mean "no cards".
    first, _ = two_pages()
    fake.searches = {SEARCH_URL: first}
    with caplog.at_level(logging.WARNING, logger="commander.previews"):
        outcome = probe(fake, tmp_path)
    assert outcome.state is ProbeState.FAILED
    assert fake.requests[-2:] == [SEARCH_URL, PAGE_2]
    assert read_previews(tmp_path) == kept
    assert kept.probed_on == date(2026, 10, 21)
    assert [m for m in caplog.messages if m.startswith("card previews not refreshed")]


def test_a_cache_that_cannot_be_written_keeps_the_last_previews_and_its_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeScryfall()
    fake.searches = {SEARCH_URL: search_page()}
    probe(fake, tmp_path, now=datetime(2026, 10, 21, 12, 0, tzinfo=UTC))
    kept = read_previews(tmp_path)

    def disk_full(*_: object, **__: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(previews_module, "write_text_atomic", disk_full)
    with caplog.at_level(logging.WARNING, logger="commander.previews"):
        outcome = probe(fake, tmp_path)
    assert (outcome.state, outcome.problem) == (ProbeState.FAILED, "disk full")
    assert read_previews(tmp_path) == kept
    assert kept.probed_on == date(2026, 10, 21)
    assert "card previews not refreshed, the last ones are kept: disk full" in caplog.messages


def test_one_unreadable_card_is_skipped_and_the_rest_of_the_set_is_kept(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    answer = search_page()
    del answer["data"][0]["oracle_id"]
    fake = FakeScryfall()
    fake.searches = {SEARCH_URL: answer}
    with caplog.at_level(logging.WARNING, logger="commander.scryfall"):
        outcome = probe(fake, tmp_path)
    assert (outcome.state, outcome.cards) == (ProbeState.UPDATED, 7)
    assert "Captain's Tutelage" not in cached_names(tmp_path)
    assert [m for m in caplog.messages if "Captain's Tutelage" in m and "no oracle_id" in m]


def test_a_cache_of_another_version_or_unreadable_counts_as_none(tmp_path: Path) -> None:
    (tmp_path / PREVIEWS_FILE).write_text(json.dumps({"version": 99, "cards": []}), encoding="utf-8")
    assert read_previews(tmp_path) == PreviewCache(None, ())
    (tmp_path / PREVIEWS_FILE).write_text("{", encoding="utf-8")
    assert read_previews(tmp_path) == PreviewCache(None, ())


# --- which previews fit a deck


def fits(
    deck: Deck,
    db: CardDb,
    *,
    today: date = TODAY,
    profile: Profile | None = None,
    targets: Mapping[Role, int] = DEFAULT_TARGETS,
) -> list[tuple[str, ...]]:
    found = found_fits(deck, db, today=today, profile=profile, targets=targets)
    return [(fit.card.name, *fit.tags) for fit in found]


def found_fits(
    deck: Deck,
    db: CardDb,
    *,
    today: date = TODAY,
    profile: Profile | None = None,
    cards: Sequence[SetCard] | None = None,
    targets: Mapping[Role, int] = DEFAULT_TARGETS,
    on_board: Sequence[str] = (),
) -> list[PreviewFit]:
    return fitting_previews(
        deck,
        db.resolve(deck.main_cards),
        profile or Profile(),
        db,
        parse_search_page(search_page()).cards if cards is None else cards,
        today=today,
        targets=targets,
        on_board=on_board,
    )


# The weights below are ln(legal cards / cards with the feature) over the fixture data's 10,018 legal cards,
# nearly all of them artifact filler: Flash is on 2 (8.52), the synergy-treasure tag on 1, the Golem (9.21), a
# Treasure maker's text on 1 (9.21), the ramp role on 6 (7.42), a creature with power 2 or less on 2 (8.52).
# A feature on no legal card weighs as one on 1.


def test_previews_rank_by_the_weight_of_the_features_they_match_a_rare_one_weighing_more(
    deck: Deck, db: CardDb
) -> None:
    # Flash matches two of the deck's cards, Treasure two (Magda makes one, the Golem has the tag), so the
    # deck's dominant tags are Flash, Treasure. Quark has the treasure tag and a Treasure maker's text
    # (18.42), Captain's Tutelage Flash and the tag (17.73). Munitions Enthusiast has the tag and is ramp,
    # which the deck lacks (16.63), and Dahar Master has the tag alone (9.21). Cloistered Telepath has Flash
    # and Kirk the treasure tag, but neither is in Magda's red; Reckless Impulse is tagged too, but it is a
    # reprint already out; The Wrath of Khan and D-7 Battle Cruiser match nothing.
    assert fits(themed_deck(deck, ("Flash", "Treasure")), db) == [
        ("Quark, Humble Proprietor", "Treasure"),
        ("Captain's Tutelage", "Flash", "Treasure"),
        ("Munitions Enthusiast", "Treasure"),
        ("Dahar Master", "Treasure"),
    ]


def test_a_commanders_wants_rank_first_and_show_a_card_no_deck_tag_matches(
    deck: Deck, commander_db: CardDb
) -> None:
    # Magda is a made-up synergy-low-power commander: Quark, with 1 power, is wanted. With Flash alone no tag
    # of the deck names him, and he still shows, ahead of Tutelage's Flash and Munitions Enthusiast's ramp.
    shown = found_fits(themed_deck(deck, ("Flash", "Treasure")), commander_db)
    assert [(f.card.name, f.fits_commander) for f in shown] == [
        ("Quark, Humble Proprietor", True),
        ("Captain's Tutelage", False),
        ("Munitions Enthusiast", False),
        ("Dahar Master", False),
    ]
    only = found_fits(themed_deck(deck, ("Flash",)), commander_db)
    assert [(f.card.name, f.tags, f.fits_commander) for f in only] == [
        ("Quark, Humble Proprietor", (), True),
        ("Captain's Tutelage", ("Flash",), False),
        ("Munitions Enthusiast", (), False),
    ]


def test_a_tag_weighs_by_the_commander_legal_cards_it_holds(deck: Deck, db: CardDb) -> None:
    # Of the 7 cards with the synergy-treasure tag, only the deck's Battered Golem is a legal card of the
    # card data (9.21, where all 7 would make it 7.27). Dahar Master has the tag alone; Captain's Tutelage,
    # under an id the tags do not know, has Flash alone (8.52). Quark and Munitions Enthusiast left out.
    cards = [
        replace(card, oracle_id="00000000-0000-0000-0000-00000000000b")
        if card.name == "Captain's Tutelage"
        else card
        for card in parse_search_page(search_page()).cards
    ]
    profile = Profile(exclude_cards=("Quark, Humble Proprietor", "Munitions Enthusiast"))
    shown = found_fits(themed_deck(deck, ("Flash", "Treasure")), db, profile=profile, cards=cards)
    assert [f.card.name for f in shown] == ["Dahar Master", "Captain's Tutelage"]


def test_a_commanders_want_weighs_twice_what_a_theme_alone_asks_for(deck: Deck, commander_db: CardDb) -> None:
    # Quark without his Treasure text and tag matches no deck tag; the low-power commander wants him, a
    # feature of 8.52 that counts twice (17.04): behind Captain's Tutelage's Flash and tag (17.73), ahead of
    # Munitions Enthusiast's tag and ramp (16.63), which counted once (8.52) he would not be.
    cards = [
        replace(card, oracle_id="00000000-0000-0000-0000-00000000000a", oracle_text="")
        if card.name == "Quark, Humble Proprietor"
        else card
        for card in parse_search_page(search_page()).cards
    ]
    shown = found_fits(themed_deck(deck, ("Flash", "Treasure")), commander_db, cards=cards)
    assert [(f.card.name, f.fits_commander) for f in shown] == [
        ("Captain's Tutelage", False),
        ("Quark, Humble Proprietor", True),
        ("Munitions Enthusiast", False),
        ("Dahar Master", False),
    ]


def test_a_card_working_against_the_commander_goes_last_and_says_so(deck: Deck, commander_db: CardDb) -> None:
    # Magda is a made-up synergy-low-power commander, and a +1/+1 counter on each creature lifts her small
    # creatures out of her count: Captain's Tutelage, second without it, still shows, after the rest.
    grows = "Flash\nEnchant creature\nPut a +1/+1 counter on each creature you control."
    cards = [
        replace(card, oracle_text=grows) if card.name == "Captain's Tutelage" else card
        for card in parse_search_page(search_page()).cards
    ]
    shown = found_fits(themed_deck(deck, ("Flash", "Treasure")), commander_db, cards=cards)
    assert [(f.card.name, f.against_commander) for f in shown] == [
        ("Quark, Humble Proprietor", False),
        ("Munitions Enthusiast", False),
        ("Dahar Master", False),
        ("Captain's Tutelage", True),
    ]


def test_a_wide_deck_is_shown_protection_for_its_creatures(deck: Deck, tokens_db: CardDb) -> None:
    # D-7 Battle Cruiser given The Needs of the Many's text: no card of the fixture data protects creatures
    # (9.21), behind Quark's repeatable tokens and anthem (9.21 each: the Golem is the legal card with the
    # tag, none has anthem). Dahar Master's tokens alone weigh as much, and D-7, colourless, goes after.
    needs = "Creatures you control gain hexproof and indestructible until end of turn."
    cards = [
        replace(card, oracle_text=needs) if card.name == "D-7 Battle Cruiser" else card
        for card in parse_search_page(search_page()).cards
    ]
    shown = found_fits(themed_deck(deck, ("Tokens", "Go-wide")), tokens_db, cards=cards)
    assert [(f.card.name, *f.tags) for f in shown] == [
        ("Quark, Humble Proprietor", "Tokens", "Go-wide"),
        ("Dahar Master", "Tokens", "Go-wide"),
        ("D-7 Battle Cruiser", "Tokens", "Go-wide"),
        ("Munitions Enthusiast",),
    ]


def test_tags_that_share_a_feature_count_it_once(deck: Deck, tokens_db: CardDb) -> None:
    # Tokens and Go-wide both ask for repeatable-creature-tokens, which Dahar Master has alone. Quark has it
    # and anthem: two features, so he comes first though Dahar sorts before him by name. Munitions
    # Enthusiast matches neither, and shows for the deck's short ramp.
    assert fits(themed_deck(deck, ("Tokens", "Go-wide")), tokens_db) == [
        ("Quark, Humble Proprietor", "Tokens", "Go-wide"),
        ("Dahar Master", "Tokens", "Go-wide"),
        ("Munitions Enthusiast",),
    ]


@pytest.fixture(scope="module")
def shared_tag_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CardDb]:
    """The same with a made-up tag the deck's Golem and Dahar Master carry."""
    tags = [TREASURE_TAG, ramp_with_munitions(), extra_tag_line("made-up-synergy", BATTERED_GOLEM, DAHAR)]
    with CardDb(build_card_db(tmp_path_factory.mktemp("sharedtagdb"), tags)) as opened:
        yield opened


def test_previews_that_fit_alike_go_first_that_share_more_tags_with_the_decks_cards(
    deck: Deck, db: CardDb, shared_tag_db: CardDb
) -> None:
    themed = themed_deck(deck, ("Treasure",))
    profile = Profile(exclude_cards=("Quark, Humble Proprietor", "Munitions Enthusiast"))
    # Captain's Tutelage and Dahar Master match the Treasure tag alone; Tutelage goes first by name.
    assert [f.card.name for f in found_fits(themed, db, profile=profile)] == [
        "Captain's Tutelage",
        "Dahar Master",
    ]
    # Dahar Master shares a tag with the deck's Golem that Tutelage lacks.
    assert [f.card.name for f in found_fits(themed, shared_tag_db, profile=profile)] == [
        "Dahar Master",
        "Captain's Tutelage",
    ]


def test_previews_with_nothing_shared_go_by_the_mana_value_nearest_the_decks_average(
    deck: Deck, db: CardDb
) -> None:
    themed = themed_deck(deck, ("Treasure",))
    profile = Profile(exclude_cards=("Quark, Humble Proprietor", "Munitions Enthusiast"))

    def order(tutelage: float, dahar: float) -> list[str]:
        cards = [
            replace(card, mana_value=tutelage if card.name == "Captain's Tutelage" else dahar)
            if card.name in {"Captain's Tutelage", "Dahar Master"}
            else card
            for card in parse_search_page(search_page()).cards
        ]
        return [f.card.name for f in found_fits(themed, db, profile=profile, cards=cards)]

    # The deck's spells average well under 9 mana, so the nearer card leads whichever the name.
    assert order(tutelage=2.0, dahar=9.0) == ["Captain's Tutelage", "Dahar Master"]
    assert order(tutelage=9.0, dahar=2.0) == ["Dahar Master", "Captain's Tutelage"]


def test_a_colourless_preview_comes_after_the_coloured_ones_that_fit_as_well(deck: Deck, db: CardDb) -> None:
    colourless = [
        replace(card, color_identity="") if card.name == "Captain's Tutelage" else card
        for card in parse_search_page(search_page()).cards
    ]
    themed = themed_deck(deck, ("Treasure",))
    profile = Profile(exclude_cards=("Quark, Humble Proprietor",))
    found = found_fits(themed, db, profile=profile, cards=colourless)
    # Tutelage sorts first by name; being colourless it goes last. Munitions Enthusiast is ramp.
    assert [f.card.name for f in found] == ["Munitions Enthusiast", "Dahar Master", "Captain's Tutelage"]


def test_at_most_the_shown_number_of_previews_fit(
    deck: Deck, db: CardDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(previews_module, "SHOWN", 2)
    assert [name for name, *_ in fits(themed_deck(deck, ("Flash", "Treasure")), db)] == [
        "Quark, Humble Proprietor",
        "Captain's Tutelage",
    ]


def test_a_card_on_the_board_is_left_out_before_the_cut_so_the_next_fit_takes_its_place(
    deck: Deck, db: CardDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(previews_module, "SHOWN", 2)
    themed = themed_deck(deck, ("Flash", "Treasure"))
    assert [n for n, *_ in fits(themed, db)] == ["Quark, Humble Proprietor", "Captain's Tutelage"]
    # By name in any case, as the deck's own cards are matched.
    found = found_fits(themed, db, on_board=["quark, humble proprietor"])
    assert [f.card.name for f in found] == ["Captain's Tutelage", "Munitions Enthusiast"]


def test_dominant_tags_count_with_the_tag_search_off(deck: Deck, db: CardDb) -> None:
    assert fits(themed_deck(deck, ("Treasure",)), db, profile=Profile(tag_search=False))


def test_a_spacecraft_is_a_vehicle_and_an_artifact_feeds_the_artifacts_tag_of_a_preview(
    deck: Deck, db: CardDb
) -> None:
    # The deck's Hoverbike is a Vehicle and its artifacts are Flute, Hoverbike and Golem. The theme work
    # alone, with no role the deck runs short of.
    assert fits(themed_deck(deck, ("Vehicles",)), db, targets={}) == [("D-7 Battle Cruiser", "Vehicles")]
    assert fits(themed_deck(deck, ("Artifacts",)), db, targets={}) == [("D-7 Battle Cruiser", "Artifacts")]


def test_a_red_preview_with_power_two_or_less_feeds_the_weenies_tag(deck: Deck, db: CardDb) -> None:
    # Magda has 2 power. Quark has 1; Munitions Enthusiast has 3, Dahar Master 4, and the blue Telepath is
    # out of Magda's colours.
    assert fits(themed_deck(deck, ("Weenies",)), db, targets={}) == [("Quark, Humble Proprietor", "Weenies")]


def test_an_ignored_tag_finds_no_previews(deck: Deck, db: CardDb) -> None:
    profile = Profile(ignore_themes=("Treasure",))
    assert fits(themed_deck(deck, ("Flash", "Treasure")), db, profile=profile, targets={}) == [
        ("Captain's Tutelage", "Flash")
    ]


def test_the_profiles_excluded_cards_keywords_and_tags_never_show_as_previews(deck: Deck, db: CardDb) -> None:
    # Flash and the treasure tag stay dominant; the exclusions drop cards, not the tags they match by.
    themed = themed_deck(deck, ("Flash", "Treasure"))
    by_card = Profile(exclude_cards=("Dahar Master",))
    assert [name for name, *_ in fits(themed, db, profile=by_card)] == [
        "Quark, Humble Proprietor",
        "Captain's Tutelage",
        "Munitions Enthusiast",
    ]
    # Munitions Enthusiast has trample; ramp is its role, through the ramp tag.
    by_keyword = Profile(exclude=ThemeSpec(keywords=("Trample",)))
    assert "Munitions Enthusiast" not in [name for name, *_ in fits(themed, db, profile=by_keyword)]
    by_tag = Profile(exclude=ThemeSpec(tags=("ramp",)))
    assert "Munitions Enthusiast" not in [name for name, *_ in fits(themed, db, profile=by_tag)]


def test_a_deck_with_no_theme_tags_or_none_a_preview_matches_has_only_the_previews_of_a_short_role(
    deck: Deck, db: CardDb
) -> None:
    # Munitions Enthusiast is ramp, which the deck runs short of by the default targets.
    assert fits(themed_deck(deck, ()), db) == [("Munitions Enthusiast",)]
    assert fits(themed_deck(deck, ("Golems",)), db) == [("Munitions Enthusiast",)]
    assert fits(themed_deck(deck, ()), db, targets={}) == []
    assert fits(themed_deck(deck, ("Golems",)), db, targets={}) == []


def test_a_preview_the_deck_already_has_is_not_offered_even_when_the_card_data_does_not_know_it(
    deck: Deck, db: CardDb
) -> None:
    # A spoiled card added on Archidekt: the card data has no printing of it yet, only the name matches.
    themed = themed_deck(deck, ("Flash", "Treasure"))
    spoiled = replace(
        themed.cards[0],
        name="Captain's Tutelage",
        categories=("Ramp",),
        in_deck=True,
        scryfall_id="0c340eba-645d-416a-9ed4-f2303aa8c919",
        entry_id=999_999,
    )
    held = replace(themed, cards=(*themed.cards, spoiled))
    assert [name for name, *_ in fits(held, db)] == [
        "Quark, Humble Proprietor",
        "Munitions Enthusiast",
        "Dahar Master",
    ]


def test_a_deck_whose_commander_the_card_data_does_not_know_has_no_previews_and_the_log_says_why(
    deck: Deck, db: CardDb, caplog: pytest.LogCaptureFixture
) -> None:
    # The commander is a preview itself: the card data has no printing of it.
    themed = themed_deck(deck, ("Flash", "Treasure"))
    unknown = tuple(
        replace(c, name="Kirk, Commanding Officer", scryfall_id="0c340eba-645d-416a-9ed4-f2303aa8c919")
        if COMMANDER_CATEGORY in c.categories
        else c
        for c in themed.cards
    )
    with caplog.at_level(logging.DEBUG, logger="commander.previews"):
        assert fits(replace(themed, cards=unknown), db) == []
    assert caplog.messages == [
        f"deck {deck.id}: no previews, its commander is missing or not in the card data"
    ]


def test_a_card_leaves_the_previews_on_its_release_day(deck: Deck, db: CardDb) -> None:
    themed = themed_deck(deck, ("Flash", "Treasure"))
    assert fits(themed, db, today=date(2026, 11, 12))
    assert fits(themed, db, today=date(2026, 11, 13)) == []


# --- the sync


def test_a_failed_probe_is_a_sync_warning_and_the_sync_carries_on(
    tmp_path: Path, raw_deck: Json, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    cache = tmp_path / "cache"
    kept = PreviewCache(date(2026, 10, 21), parse_search_page(search_page()).cards)
    write_previews(cache, kept)
    fake.scryfall.failing[SEARCH_URL] = 503

    def downloaded(*_: object, **__: object) -> CardRefresh:
        return DOWNLOADED

    monkeypatch.setattr(syncrun, "refresh_card_db", downloaded)
    services = syncrun.SyncServices(fake.client, MemoryTokenStore(), cache, lambda: NOW, lambda _: Pacer(0))
    outcome = run_sync(
        services,
        Workspace(tmp_path / "decks"),
        "TESTER",
        MetaSettings(),
        SyncOptions(ideas=False),
        on_progress=lambda _: None,
    )
    assert [r.deck_id for r in outcome.results] == [raw_deck["id"]]
    assert outcome.previews is not None
    assert outcome.previews.state is ProbeState.FAILED
    assert [w for w in outcome.warnings if w.startswith("card previews not refreshed (")]
    assert read_previews(cache) == kept


def test_a_failed_tag_download_is_a_sync_warning_and_the_cards_are_kept(
    tmp_path: Path, raw_deck: Json, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    cache = tmp_path / "cache"
    fake.scryfall.searches = {SEARCH_URL: search_page()}
    fake.scryfall.failing[TAGS_URL] = 503

    def downloaded(*_: object, **__: object) -> CardRefresh:
        return DOWNLOADED

    monkeypatch.setattr(syncrun, "refresh_card_db", downloaded)
    services = syncrun.SyncServices(fake.client, MemoryTokenStore(), cache, lambda: NOW, lambda _: Pacer(0))
    outcome = run_sync(
        services,
        Workspace(tmp_path / "decks"),
        "TESTER",
        MetaSettings(),
        SyncOptions(ideas=False),
        on_progress=lambda _: None,
    )
    assert outcome.previews is not None
    assert outcome.previews.state is ProbeState.UPDATED
    assert [w for w in outcome.warnings if w.startswith("tags of the card previews not refreshed (")]
    assert len(read_previews(cache).cards) == 8


def test_the_card_data_and_the_probe_of_a_sync_share_one_scryfall_client(
    tmp_path: Path, raw_deck: Json, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One client reads the set list once; two would ask Scryfall for /sets twice a day.
    fake = FakeArchidekt()
    fake.put(raw_deck)
    clients: list[ScryfallClient] = []

    def downloaded(client: ScryfallClient, *_: object, **__: object) -> CardRefresh:
        clients.append(client)
        return DOWNLOADED

    def probed(client: ScryfallClient, *_: object, **__: object) -> PreviewProbe:
        clients.append(client)
        return PreviewProbe(ProbeState.NO_SEASON)

    monkeypatch.setattr(syncrun, "refresh_card_db", downloaded)
    monkeypatch.setattr(syncrun, "probe_previews", probed)
    services = syncrun.SyncServices(
        fake.client, MemoryTokenStore(), tmp_path / "cache", lambda: NOW, lambda _: Pacer(0)
    )
    run_sync(
        services,
        Workspace(tmp_path / "decks"),
        "TESTER",
        MetaSettings(),
        SyncOptions(ideas=False),
        on_progress=lambda _: None,
    )
    assert len(clients) == 2
    assert clients[0] is clients[1]


def test_a_rebuild_and_the_probe_on_one_client_read_the_file_list_and_the_tags_once(tmp_path: Path) -> None:
    # On a day the card data is rebuilt, the probe takes the tags the rebuild downloaded, which tag
    # Munitions Enthusiast ramp.
    fake = FakeScryfall()
    fake.searches = {SEARCH_URL: search_page()}
    tags = [line for line in tag_lines() if json.loads(line)["id"] != RAMP_TAG]
    fake.tags_gz = gzip_lines([*tags, ramp_with_munitions()])
    client = ScryfallClient(Fetcher(fake.client()))
    refresh_card_db(client, tmp_path, now=lambda: NOW, force=True)
    outcome = probe_previews(client, tmp_path, now=lambda: NOW, pacer=Pacer(0))
    assert fake.requests.count(BULK_URL) == 1
    assert fake.requests.count(TAGS_URL) == 1
    # The set list (a forced rebuild reads none) and the search; the file list was the rebuild's.
    assert outcome.requests == 2
    assert read_previews(tmp_path).roles == {MUNITIONS: frozenset({Role.RAMP})}


def test_an_unexpected_error_in_the_probe_is_logged_and_a_warning_not_a_failed_sync(
    tmp_path: Path, raw_deck: Json, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeArchidekt()
    fake.put(raw_deck)

    def downloaded(*_: object, **__: object) -> CardRefresh:
        return DOWNLOADED

    def broken_probe(*_: object, **__: object) -> PreviewProbe:
        raise KeyError("released_at")

    monkeypatch.setattr(syncrun, "refresh_card_db", downloaded)
    monkeypatch.setattr(syncrun, "probe_previews", broken_probe)
    services = syncrun.SyncServices(
        fake.client, MemoryTokenStore(), tmp_path / "cache", lambda: NOW, lambda _: Pacer(0)
    )
    with caplog.at_level(logging.ERROR, logger="commander.syncrun"):
        outcome = run_sync(
            services,
            Workspace(tmp_path / "decks"),
            "TESTER",
            MetaSettings(),
            SyncOptions(ideas=False),
            on_progress=lambda _: None,
        )
    assert [r.deck_id for r in outcome.results] == [raw_deck["id"]]
    assert outcome.previews is not None
    assert outcome.previews.state is ProbeState.FAILED
    assert [w for w in outcome.warnings if w.startswith("card previews not refreshed (")]
    [record] = [
        r for r in caplog.records if r.getMessage().startswith("the card previews probe failed unexpectedly")
    ]
    assert record.exc_info is not None


def test_a_sync_without_card_data_does_not_probe(tmp_path: Path, raw_deck: Json) -> None:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    services = syncrun.SyncServices(fake.client, MemoryTokenStore(), tmp_path / "cache", lambda: NOW)
    outcome = run_sync(
        services,
        Workspace(tmp_path / "decks"),
        "TESTER",
        MetaSettings(),
        SyncOptions(cards=False, ideas=False),
        on_progress=lambda _: None,
    )
    assert outcome.previews is None
    assert not [url for url in fake.scryfall.requests if "scryfall" in url]


# --- the app's bridge


@pytest.fixture
def desk_settings(tmp_path: Path, raw_deck: Json, previews_db_file: Path) -> DeskSettings:
    return settings_with(tmp_path, raw_deck, previews_db_file)


def settings_with(tmp_path: Path, raw_deck: Json, db_file: Path) -> DeskSettings:
    # The fixture deck is tagged Dwarves, Vehicles and cEDH; Treasure is added, which Magda's text and the
    # Golem's made-up tag match. No card of it is a Vehicle, so that tag is no theme.
    raw_deck["deckTags"].append(
        {"id": 1, "tag": 99, "deck": raw_deck["id"], "name": "Treasure", "position": "M"}
    )
    fake = FakeArchidekt()
    fake.put(raw_deck)
    cache = tmp_path / "cache"
    cache.mkdir()
    shutil.copy(db_file, cache / CARD_DB_FILE)
    result = run(["sync", "--no-cards", "--no-ideas", "--workspace", str(tmp_path / "decks")], tmp_path, fake)
    assert result.code == 0, result.err
    return DeskSettings(tmp_path / "decks", cache / CARD_DB_FILE, "TESTER")


def make_desk(settings: DeskSettings) -> Desk:
    return Desk(
        settings,
        client_factory=FakeArchidekt().client,
        token_store=MemoryTokenStore(),
        now=lambda: NOW,
        pacer_factory=lambda _: Pacer(0),
    )


def test_the_panel_reads_the_roles_the_probe_kept_and_names_the_short_role_a_card_fills(
    desk_settings: DeskSettings, raw_deck: Json
) -> None:
    # D-7 Battle Cruiser fits none of the deck's tags and the card data knows no role of it; the probe's
    # roles say it is ramp, which the deck runs short of, and that is what its tile says it fits.
    desk = make_desk(desk_settings)
    deck_id = int(raw_deck["id"])
    cards = parse_search_page(search_page()).cards
    d7 = next(c for c in cards if c.name == "D-7 Battle Cruiser")
    roles = {d7.oracle_id: frozenset({Role.RAMP})}
    write_previews(desk_settings.cache, PreviewCache(TODAY, cards, roles=roles))
    shown = {str(c["name"]): c for c in cast("list[Json]", desk.previews(deck_id)["cards"])}
    assert shown["D-7 Battle Cruiser"]["fits"] == ["Ramp"]
    # Munitions Enthusiast matches the Treasure tag and is ramp too.
    assert shown["Munitions Enthusiast"]["fits"] == ["Treasure", "Ramp"]
    assert desk.deck(deck_id)["previews"] == 5


def test_the_panel_reads_the_tags_the_probe_kept_beside_the_card_datas(
    desk_settings: DeskSettings, raw_deck: Json
) -> None:
    # The card data gives D-7 Battle Cruiser no tag; the probe's tags give it synergy-treasure, which the
    # deck's Treasure tag asks for.
    desk = make_desk(desk_settings)
    deck_id = int(raw_deck["id"])
    cards = parse_search_page(search_page()).cards
    d7 = next(c for c in cards if c.name == "D-7 Battle Cruiser")
    tags = {d7.oracle_id: frozenset({"synergy-treasure"})}
    write_previews(desk_settings.cache, PreviewCache(TODAY, cards, tags=tags))
    shown = {str(c["name"]): c for c in cast("list[Json]", desk.previews(deck_id)["cards"])}
    assert shown["D-7 Battle Cruiser"]["fits"] == ["Treasure"]


def test_the_deck_view_counts_its_previews_and_the_panel_gets_them_to_look_at(
    desk_settings: DeskSettings, raw_deck: Json
) -> None:
    desk = make_desk(desk_settings)
    deck_id = int(raw_deck["id"])
    assert desk.deck(deck_id)["previews"] == 0
    assert desk.previews(deck_id) == {"cards": []}
    write_previews(desk_settings.cache, PreviewCache(TODAY, parse_search_page(search_page()).cards))
    assert desk.deck(deck_id)["previews"] == 4
    cards = cast("list[Json]", desk.previews(deck_id)["cards"])
    # Quark has the treasure tag and makes Treasure; Munitions Enthusiast is ramp, which the deck lacks.
    assert [c["name"] for c in cards] == [
        "Quark, Humble Proprietor",
        "Munitions Enthusiast",
        "Captain's Tutelage",
        "Dahar Master",
    ]
    assert cards[2] == {
        "name": "Captain's Tutelage",
        "manaCost": "{1}{R}",
        "typeLine": "Enchantment — Aura",
        "text": cards[2]["text"],
        "image": "https://cards.scryfall.io/normal/front/0/c/0c340eba-645d-416a-9ed4-f2303aa8c919.jpg",
        "set": "Star Trek",
        "out": "2026-11-13",
        "fits": ["Treasure"],
        "against": [],
    }
    assert str(cards[2]["text"]).startswith("Flash\nEnchant creature")


def test_the_panel_names_the_commander_among_the_tags_a_preview_fits(
    tmp_path: Path, raw_deck: Json, commander_db_file: Path
) -> None:
    settings = settings_with(tmp_path, raw_deck, commander_db_file)
    write_previews(settings.cache, PreviewCache(TODAY, parse_search_page(search_page()).cards))
    cards = cast("list[Json]", make_desk(settings).previews(int(raw_deck["id"]))["cards"])
    # Quark is a Treasure maker and has 1 power, which Magda, a made-up synergy-low-power commander, wants;
    # Munitions Enthusiast is ramp, which the deck runs short of.
    assert {c["name"]: c["fits"] for c in cards} == {
        "Quark, Humble Proprietor": ["Treasure", "Magda"],
        "Captain's Tutelage": ["Treasure"],
        "Munitions Enthusiast": ["Treasure", "Ramp"],
        "Dahar Master": ["Treasure"],
    }


def test_the_polled_previews_stamp_changes_with_the_cache_file_and_the_local_day(
    desk_settings: DeskSettings,
) -> None:
    clock = [NOW]
    desk = Desk(
        desk_settings,
        client_factory=FakeArchidekt().client,
        token_store=MemoryTokenStore(),
        now=lambda: clock[0],
        pacer_factory=lambda _: Pacer(0),
    )
    before = desk.stamps().get("previews")
    assert before is not None
    assert desk.stamps().get("previews") == before
    write_previews(desk_settings.cache, PreviewCache(TODAY, parse_search_page(search_page()).cards))
    written = desk.stamps().get("previews")
    assert written != before
    clock[0] = NOW + timedelta(days=1)
    assert desk.stamps().get("previews") not in (before, written)


def test_off_season_opening_a_deck_does_no_theme_work_for_previews(
    desk_settings: DeskSettings, raw_deck: Json, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_theme_work(*_: object, **__: object) -> list[str]:
        raise AssertionError("the themes were read with no previews cached")

    monkeypatch.setattr(previews_module, "read_deck_themes", no_theme_work)
    write_previews(desk_settings.cache, PreviewCache(TODAY, ()))
    assert make_desk(desk_settings).deck(int(raw_deck["id"]))["previews"] == 0


def test_a_card_leaves_on_its_release_day_by_the_local_date_not_utc(
    desk_settings: DeskSettings, raw_deck: Json, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 23:30 UTC the day before release is already release day in Central Europe.
    def central_europe(moment: datetime) -> date:
        return (moment + timedelta(hours=1)).date()

    monkeypatch.setattr(previews_module, "local_day", central_europe)
    monkeypatch.setattr(desk_module, "local_day", central_europe)
    write_previews(desk_settings.cache, PreviewCache(TODAY, parse_search_page(search_page()).cards))
    desk = Desk(
        desk_settings,
        client_factory=FakeArchidekt().client,
        token_store=MemoryTokenStore(),
        now=lambda: datetime(2026, 11, 12, 23, 30, tzinfo=UTC),
        pacer_factory=lambda _: Pacer(0),
    )
    assert desk.previews(int(raw_deck["id"])) == {"cards": []}


def test_a_card_without_a_valid_scryfall_id_is_skipped_in_the_answer_and_the_cache(tmp_path: Path) -> None:
    answer = search_page()
    answer["data"][0]["id"] = ""
    names = [card.name for card in parse_search_page(answer).cards]
    assert len(names) == 8
    assert "Captain's Tutelage" not in names
    write_previews(tmp_path, PreviewCache(TODAY, parse_search_page(search_page()).cards))
    stored = json.loads((tmp_path / PREVIEWS_FILE).read_text(encoding="utf-8"))
    stored["cards"][0]["scryfallId"] = "not-a-uuid"
    (tmp_path / PREVIEWS_FILE).write_text(json.dumps(stored), encoding="utf-8")
    assert len(read_previews(tmp_path).cards) == 8


def test_the_config_files_targets_decide_which_roles_run_short(
    desk_settings: DeskSettings, raw_deck: Json, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_previews(desk_settings.cache, PreviewCache(TODAY, parse_search_page(search_page()).cards))
    wanted: list[Mapping[Role, int]] = []
    ranked = desk_module.fitting_previews

    def spy(*args: Any, **kwargs: Any) -> list[PreviewFit]:  # noqa: ANN401
        wanted.append(kwargs["targets"])
        return ranked(*args, **kwargs)

    monkeypatch.setattr(desk_module, "fitting_previews", spy)
    desk = make_desk(replace(desk_settings, targets={Role.RAMP: 0}))
    # With no ramp wanted, Munitions Enthusiast fills no short role; it ranks by what it shares with the deck.
    assert [c["name"] for c in cast("list[Json]", desk.previews(int(raw_deck["id"]))["cards"])] == [
        "Quark, Humble Proprietor",
        "Captain's Tutelage",
        "Munitions Enthusiast",
        "Dahar Master",
    ]
    assert wanted[0][Role.RAMP] == 0


# --- moving a preview to Considering

QUARK_NAME = "Quark, Humble Proprietor"


def desk_at(settings: DeskSettings, clock: list[datetime]) -> Desk:
    return Desk(
        settings,
        client_factory=FakeArchidekt().client,
        token_store=MemoryTokenStore(),
        now=lambda: clock[0],
        pacer_factory=lambda _: Pacer(0),
    )


def board_of(settings: DeskSettings, deck_id: int) -> board_module.Board:
    folder = Workspace(settings.workspace).folder_for(deck_id)
    assert folder is not None
    return read_board(folder)


def previewed(settings: DeskSettings) -> None:
    write_previews(settings.cache, PreviewCache(TODAY, parse_search_page(search_page()).cards))


def panel_names(desk: Desk, deck_id: int) -> list[object]:
    return [c["name"] for c in cast("list[Json]", desk.previews(deck_id)["cards"])]


def test_moving_a_preview_puts_it_on_the_board_as_yours_and_the_next_fit_takes_its_place(
    desk_settings: DeskSettings, raw_deck: Json, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(previews_module, "SHOWN", 2)
    previewed(desk_settings)
    desk = make_desk(desk_settings)
    deck_id = int(raw_deck["id"])
    assert panel_names(desk, deck_id) == [QUARK_NAME, "Munitions Enthusiast"]
    assert desk.deck(deck_id)["previews"] == 2

    answer = desk.move_preview(deck_id, QUARK_NAME)

    moved = cast("Json", answer["card"])
    assert (moved["name"], moved["source"], moved["status"]) == (QUARK_NAME, "you", "open")
    assert moved["reason"] == "Upcoming: fits #Treasure"
    [card] = board_of(desk_settings, deck_id).cards
    assert (card.name, card.source, card.status, card.replace) == (QUARK_NAME, Actor.YOU, Status.OPEN, ())
    # The board keeps the card as the previews saw it, to add from it.
    assert card.printing is not None
    assert (card.printing.released_at, card.printing.set_code, card.printing.collector_number) == (
        date(2026, 11, 13),
        "trk",
        "166",
    )
    # The panel and the deck view's count both skip the card at once and fill its place: the next in line.
    assert panel_names(desk, deck_id) == ["Munitions Enthusiast", "Captain's Tutelage"]
    assert desk.deck(deck_id)["previews"] == 2


def test_the_fits_in_the_reason_name_the_commander_and_the_short_role_as_the_tile_does(
    tmp_path: Path, raw_deck: Json, commander_db_file: Path
) -> None:
    settings = settings_with(tmp_path, raw_deck, commander_db_file)
    previewed(settings)
    desk = make_desk(settings)
    deck_id = int(raw_deck["id"])

    desk.move_preview(deck_id, QUARK_NAME)
    desk.move_preview(deck_id, "Munitions Enthusiast")

    assert [c.reason for c in board_of(settings, deck_id).cards] == [
        "Upcoming: fits #Treasure #Magda",
        "Upcoming: fits #Treasure #Ramp",
    ]


def test_a_preview_cannot_be_moved_twice_nor_when_the_panel_does_not_offer_it(
    desk_settings: DeskSettings, raw_deck: Json
) -> None:
    previewed(desk_settings)
    desk = make_desk(desk_settings)
    deck_id = int(raw_deck["id"])
    desk.move_preview(deck_id, QUARK_NAME)

    assert desk.move_preview(deck_id, QUARK_NAME) == {
        "error": "'Quark, Humble Proprietor' is not among the previews that fit this deck"
    }
    # Kirk is red and white, outside the deck's colours; the reprint is already legal; the third is no card.
    for name in ("Kirk, Commanding Officer", "Reckless Impulse", "Black Lotus", "  "):
        assert "error" in desk.move_preview(deck_id, name), name
    assert [c.name for c in board_of(desk_settings, deck_id).cards] == [QUARK_NAME]
    folder = Workspace(desk_settings.workspace).folder_for(deck_id)
    assert folder is not None
    assert [e.action for e in read_activity(folder)] == ["proposed (considering)"]


def test_a_moved_preview_stays_off_the_panel_whatever_becomes_of_it_and_a_removed_one_comes_back(
    desk_settings: DeskSettings, raw_deck: Json
) -> None:
    previewed(desk_settings)
    desk = make_desk(desk_settings)
    deck_id = int(raw_deck["id"])
    card = cast("Json", desk.move_preview(deck_id, QUARK_NAME)["card"])

    desk.decide(deck_id, str(card["id"]), "reject", None, "no")
    assert QUARK_NAME not in panel_names(desk, deck_id)
    desk.decide(deck_id, str(card["id"]), "reopen", None, "")
    assert QUARK_NAME not in panel_names(desk, deck_id)
    assert desk.remove(deck_id, str(card["id"])) == {"ok": True}
    assert QUARK_NAME in panel_names(desk, deck_id)


def test_the_deck_view_shows_an_upcoming_card_the_card_data_lacks_from_the_boards_snapshot(
    desk_settings: DeskSettings, raw_deck: Json
) -> None:
    previewed(desk_settings)
    desk = make_desk(desk_settings)
    deck_id = int(raw_deck["id"])
    desk.move_preview(deck_id, QUARK_NAME)

    view = desk.deck(deck_id)

    [card] = cast("list[Json]", view["cards"])
    assert (card["name"], card["status"], card["upcoming"], card["out"]) == (
        QUARK_NAME,
        "open",
        True,
        "2026-11-13",
    )
    assert (card["manaCost"], card["typeLine"]) == ("{2}{R}", "Legendary Creature — Ferengi Rogue")
    assert str(card["text"]).startswith("You can't sacrifice Treasure tokens.")
    assert card["image"] == (
        "https://cards.scryfall.io/normal/front/9/6/96104754-57e2-46e9-948d-628aab2aa0ad.jpg"
    )
    assert (card["targets"], card["share"], card["held"], card["priceCents"]) == ([], None, False, None)
    # Filed by its type, as a card the card data knows is.
    assert card["typeCategory"] == "Creature"
    assert "printing" not in card
    assert "error" not in view


def test_an_upcoming_card_keeps_its_face_in_the_view_when_the_previews_cache_is_emptied(
    desk_settings: DeskSettings, raw_deck: Json
) -> None:
    previewed(desk_settings)
    desk = make_desk(desk_settings)
    deck_id = int(raw_deck["id"])
    desk.move_preview(deck_id, QUARK_NAME)
    write_previews(desk_settings.cache, PreviewCache(TODAY, ()))

    [card] = cast("list[Json]", desk.deck(deck_id)["cards"])

    assert (card["name"], card["upcoming"], card["out"]) == (QUARK_NAME, True, "2026-11-13")
    assert (card["typeLine"], card["manaCost"]) == ("Legendary Creature — Ferengi Rogue", "{2}{R}")


def test_an_upcoming_card_is_upcoming_in_the_view_only_until_its_release_day(
    desk_settings: DeskSettings, raw_deck: Json
) -> None:
    previewed(desk_settings)
    clock = [NOW]
    desk = desk_at(desk_settings, clock)
    deck_id = int(raw_deck["id"])
    desk.move_preview(deck_id, QUARK_NAME)

    [before] = cast("list[Json]", desk.deck(deck_id)["cards"])
    clock[0] = RELEASE_DAY
    [after] = cast("list[Json]", desk.deck(deck_id)["cards"])

    assert (before["upcoming"], after["upcoming"]) == (True, False)
    # Out, the card data still lacks it until the next rebuild, so the snapshot still shows it.
    assert (after["out"], after["typeLine"]) == ("2026-11-13", "Legendary Creature — Ferengi Rogue")


@pytest.mark.parametrize("cut", ["Battered Golem", None])
def test_an_upcoming_card_can_be_queued_through_the_bridge_before_its_release_day(
    desk_settings: DeskSettings, raw_deck: Json, cut: str | None
) -> None:
    previewed(desk_settings)
    desk = desk_at(desk_settings, [NOW])
    deck_id = int(raw_deck["id"])
    card_id = str(cast("Json", desk.move_preview(deck_id, QUARK_NAME)["card"])["id"])

    queued = cast("Json", desk.decide(deck_id, card_id, "add", cut, "")["card"])

    assert (queued["status"], queued["choice"]) == ("add", cut)
    [card] = cast("list[Json]", desk.deck(deck_id)["cards"])
    assert (card["status"], card["upcoming"], card["out"]) == ("add", True, "2026-11-13")
