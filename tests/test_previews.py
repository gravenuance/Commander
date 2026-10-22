"""Scryfall's card previews (ADR 0055): the sync's daily probe, its cache and the previews fitting a deck."""

import copy
import json
import logging
import shutil
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from commander import desk as desk_module
from commander import previews as previews_module
from commander import syncrun
from commander.carddb import CARD_DB_FILE, CardDb, Role
from commander.config import MetaSettings
from commander.deckprofile import Profile, ThemeSpec
from commander.desk import Desk, DeskSettings
from commander.models import Deck
from commander.net import Fetcher, Pacer
from commander.previews import (
    PREVIEWS_FILE,
    PreviewCache,
    PreviewProbe,
    ProbeState,
    fitting_previews,
    probe_previews,
    read_previews,
    write_previews,
)
from commander.scryfall import CardRefresh, ScryfallClient, ShapeError, parse_search_page
from commander.store import Workspace
from commander.suggest import DEFAULT_TARGETS
from commander.syncrun import SyncOptions, run_sync
from conftest import (
    FakeArchidekt,
    FakeScryfall,
    Json,
    MemoryTokenStore,
    build_card_db,
    load_fixture,
    tag_lines,
)
from test_cli_apply import run
from test_ideas import themed_deck
from test_meta import deck
from test_syncrun import DOWNLOADED

__all__ = ["deck"]  # the fixture shared with test_meta

SEARCH_URL = "https://api.scryfall.com/cards/search?q=e%3Atrk&unique=cards"
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
# Made up for these tests: the tag a Treasure deck tag stands for, on most red previews, the reprint and the
# red-white Kirk; and Munitions Enthusiast as ramp, a role the small test decks run short of.
TREASURE_TAG = json.dumps(
    {
        "object": "tag",
        "id": "aaaaaaaa-0000-4000-8000-0000000000aa",
        "slug": "synergy-treasure",
        "type": "oracle",
        "child_ids": [],
        "taggings": [{"oracle_id": o} for o in (TUTELAGE, DAHAR, KIRK, MUNITIONS, QUARK, IMPULSE)],
    }
)


def ramp_with_munitions() -> str:
    ramp = next(json.loads(line) for line in tag_lines() if json.loads(line)["id"] == RAMP_TAG)
    ramp["taggings"].append({"oracle_id": MUNITIONS})
    return json.dumps(ramp)


@pytest.fixture(scope="module")
def previews_db_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_card_db(tmp_path_factory.mktemp("previewsdb"), [TREASURE_TAG, ramp_with_munitions()])


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
        2,
    )
    assert fake.requests == ["https://api.scryfall.com/sets", SEARCH_URL, PAGE_2]
    assert pacer.waits == 2
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
    assert (outcome.state, outcome.cards, outcome.requests) == (ProbeState.UPDATED, 5, 1)
    assert PAGE_2 not in fake.requests
    assert "the search for set trk has over 1 pages; the rest is left out" in caplog.messages


def test_the_set_list_is_read_once_for_the_card_data_and_the_probe() -> None:
    fake = FakeScryfall()
    client = ScryfallClient(Fetcher(fake.client()))
    assert client.sets() == client.sets()
    assert fake.requests == ["https://api.scryfall.com/sets"]


def test_the_probe_runs_once_a_day(tmp_path: Path) -> None:
    fake = FakeScryfall()
    fake.searches = {SEARCH_URL: search_page()}
    probe(fake, tmp_path)
    fake.requests.clear()
    assert probe(fake, tmp_path, now=NOW.replace(hour=23)).state is ProbeState.DONE_TODAY
    assert fake.requests == []
    probe(fake, tmp_path, now=datetime(2026, 10, 23, 1, 0, tzinfo=UTC))
    assert fake.requests == ["https://api.scryfall.com/sets", SEARCH_URL]


def test_a_set_scryfall_has_no_cards_for_yet_is_an_empty_set_not_a_failure(tmp_path: Path) -> None:
    outcome = probe(FakeScryfall(), tmp_path)
    assert (outcome.state, outcome.cards) == (ProbeState.UPDATED, 0)
    assert read_previews(tmp_path) == PreviewCache(TODAY, ())


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


def test_a_cache_of_another_version_or_unreadable_counts_as_none(tmp_path: Path) -> None:
    (tmp_path / PREVIEWS_FILE).write_text(json.dumps({"version": 99, "cards": []}), encoding="utf-8")
    assert read_previews(tmp_path) == PreviewCache(None, ())
    (tmp_path / PREVIEWS_FILE).write_text("{", encoding="utf-8")
    assert read_previews(tmp_path) == PreviewCache(None, ())


# --- which previews fit a deck


def fits(
    deck: Deck, db: CardDb, *, today: date = TODAY, profile: Profile | None = None
) -> list[tuple[str, ...]]:
    main = db.resolve(deck.main_cards)
    found = fitting_previews(
        deck,
        main,
        profile or Profile(),
        db,
        parse_search_page(search_page()).cards,
        today=today,
        targets=DEFAULT_TARGETS,
    )
    return [(fit.card.name, *fit.tags) for fit in found]


def test_previews_rank_by_the_dominant_tags_they_match_then_a_short_role_then_name(
    deck: Deck, db: CardDb
) -> None:
    # Flash matches two of the deck's cards, Treasure none, so the deck's dominant tags are Flash, Treasure.
    # Captain's Tutelage has Flash and the treasure tag; Munitions Enthusiast is ramp, which the deck lacks.
    # Cloistered Telepath has Flash and Kirk the treasure tag, but neither is in Magda's red; Reckless Impulse
    # is tagged too, but it is a reprint already out; The Wrath of Khan and D-7 Battle Cruiser match nothing.
    assert fits(themed_deck(deck, ("Flash", "Treasure")), db) == [
        ("Captain's Tutelage", "Flash", "Treasure"),
        ("Munitions Enthusiast", "Treasure"),
        ("Dahar Master", "Treasure"),
        ("Quark, Humble Proprietor", "Treasure"),
    ]


def test_at_most_the_shown_number_of_previews_fit(
    deck: Deck, db: CardDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(previews_module, "SHOWN", 2)
    assert [name for name, *_ in fits(themed_deck(deck, ("Flash", "Treasure")), db)] == [
        "Captain's Tutelage",
        "Munitions Enthusiast",
    ]


def test_dominant_tags_count_with_the_tag_search_off(deck: Deck, db: CardDb) -> None:
    assert fits(themed_deck(deck, ("Treasure",)), db, profile=Profile(tag_search=False))


def test_an_ignored_tag_finds_no_previews(deck: Deck, db: CardDb) -> None:
    profile = Profile(ignore_themes=("Treasure",))
    assert fits(themed_deck(deck, ("Flash", "Treasure")), db, profile=profile) == [
        ("Captain's Tutelage", "Flash")
    ]


def test_the_profiles_excluded_cards_keywords_and_tags_never_show_as_previews(deck: Deck, db: CardDb) -> None:
    # Flash and the treasure tag stay dominant; the exclusions drop cards, not the tags they match by.
    themed = themed_deck(deck, ("Flash", "Treasure"))
    by_card = Profile(exclude_cards=("Dahar Master",))
    assert [name for name, *_ in fits(themed, db, profile=by_card)] == [
        "Captain's Tutelage",
        "Munitions Enthusiast",
        "Quark, Humble Proprietor",
    ]
    # Munitions Enthusiast has trample; ramp is its role, through the ramp tag.
    by_keyword = Profile(exclude=ThemeSpec(keywords=("Trample",)))
    assert "Munitions Enthusiast" not in [name for name, *_ in fits(themed, db, profile=by_keyword)]
    by_tag = Profile(exclude=ThemeSpec(tags=("ramp",)))
    assert "Munitions Enthusiast" not in [name for name, *_ in fits(themed, db, profile=by_tag)]


def test_a_deck_with_no_theme_tags_or_none_a_preview_matches_has_no_previews(deck: Deck, db: CardDb) -> None:
    assert fits(themed_deck(deck, ()), db) == []
    assert fits(themed_deck(deck, ("Golems",)), db) == []


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
    # The fixture deck is tagged Dwarves, Vehicles and cEDH; Treasure is added, which no card of it matches.
    raw_deck["deckTags"].append(
        {"id": 1, "tag": 99, "deck": raw_deck["id"], "name": "Treasure", "position": "M"}
    )
    fake = FakeArchidekt()
    fake.put(raw_deck)
    cache = tmp_path / "cache"
    cache.mkdir()
    shutil.copy(previews_db_file, cache / CARD_DB_FILE)
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
    assert [c["name"] for c in cards] == [
        "Munitions Enthusiast",
        "Captain's Tutelage",
        "Dahar Master",
        "Quark, Humble Proprietor",
    ]
    assert cards[1] == {
        "name": "Captain's Tutelage",
        "manaCost": "{1}{R}",
        "typeLine": "Enchantment — Aura",
        "text": cards[1]["text"],
        "image": "https://cards.scryfall.io/normal/front/0/c/0c340eba-645d-416a-9ed4-f2303aa8c919.jpg",
        "set": "Star Trek",
        "out": "2026-11-13",
        "fits": ["Treasure"],
    }
    assert str(cards[1]["text"]).startswith("Flash\nEnchant creature")


def test_off_season_opening_a_deck_does_no_theme_work_for_previews(
    desk_settings: DeskSettings, raw_deck: Json, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_theme_work(*_: object, **__: object) -> list[str]:
        raise AssertionError("dominant_tags ran with no previews cached")

    monkeypatch.setattr(previews_module, "dominant_tags", no_theme_work)
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
    desk_settings: DeskSettings, raw_deck: Json
) -> None:
    write_previews(desk_settings.cache, PreviewCache(TODAY, parse_search_page(search_page()).cards))
    desk = make_desk(replace(desk_settings, targets={Role.RAMP: 0}))
    # With no ramp wanted, Munitions Enthusiast fills no short role and ranks by its name.
    assert [c["name"] for c in cast("list[Json]", desk.previews(int(raw_deck["id"]))["cards"])] == [
        "Captain's Tutelage",
        "Dahar Master",
        "Munitions Enthusiast",
        "Quark, Humble Proprietor",
    ]
