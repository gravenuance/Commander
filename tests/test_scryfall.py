import json
import logging
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from commander.carddb import CARD_DB_FILE, FACE_SEPARATOR, CardDb, Role, TextGate
from commander.jsonshape import ShapeError
from commander.net import Fetcher, NetworkError
from commander.scryfall import (
    RefreshStatus,
    ScryfallClient,
    ScryfallError,
    SetInfo,
    parse_bulk_listing,
    parse_card,
    parse_sets_listing,
    parse_tag,
    read_cards,
    read_tags,
    refresh_card_db,
    role_gates,
    role_members,
)
from conftest import (
    CARDS_URL,
    FILLER_TAGS,
    MAGDA_ORACLE,
    SETS_URL,
    TAGS_URL,
    FakeScryfall,
    Json,
    card_lines,
    filler_card,
    filler_tag,
    fixture_lines,
    tag_lines,
    write_bulk,
)

GOLEM_ORACLE = "066dfefd-eef5-4c81-8e8a-310bac01674f"
FLUTE_ORACLE = "d6c46906-1339-4440-8cd0-2bb9890d4975"
# 28 real lines minus the digital-only Magda promo, plus the filler.
PRINTINGS_IN_FIXTURE = 28 + 10_000
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
BULK_URL = "https://api.scryfall.com/bulk-data"


def real_card(name: str, set_code: str) -> Json:
    for line in fixture_lines("scryfall_cards.jsonl"):
        card: Json = json.loads(line)
        if card["name"] == name and card["set"] == set_code:
            return card
    raise AssertionError(f"{name} ({set_code}) is not in the fixture")


def client_for(fake: FakeScryfall) -> ScryfallClient:
    return ScryfallClient(Fetcher(fake.client(), sleep=lambda _: None))


def refresh(fake: FakeScryfall, folder: Path, *, force: bool = False, when: datetime = NOW) -> RefreshStatus:
    return refresh_card_db(client_for(fake), folder, now=lambda: when, force=force).status


def only_sets(fake: FakeScryfall, *codes: str) -> Json:
    """The fixture's /sets answer cut down to these sets."""
    return {**fake.sets, "data": [s for s in fake.sets["data"] if s["code"] in codes]}


def test_listing_keeps_files_with_a_download_link() -> None:
    files = parse_bulk_listing(FakeScryfall().listing())
    assert sorted(files) == ["default_cards", "oracle_cards", "oracle_tags"]
    assert files["default_cards"].url == CARDS_URL
    assert files["oracle_tags"].updated_at == "2026-10-15T09:00:38.101+00:00"


def test_listing_rejects_files_off_the_scryfall_file_host() -> None:
    listing = FakeScryfall().listing()
    listing["data"][1]["jsonl_download_uri"] = "https://evil.example/default-cards.jsonl.gz"
    with pytest.raises(ShapeError, match="not a Scryfall file URL"):
        parse_bulk_listing(listing)


def test_parses_a_normal_printing() -> None:
    card = parse_card(real_card("Magda, Brazen Outlaw", "plst"))
    assert card is not None
    assert card.printing.scryfall_id == "338380ba-8415-40a2-8b81-2b30218ea16a"
    assert card.printing.oracle_id == MAGDA_ORACLE
    assert (card.printing.set_code, card.printing.collector_number) == ("plst", "KHM-142")
    assert (card.printing.eur_cents, card.printing.eur_foil_cents) == (763, None)
    assert (card.mana_cost, card.cmc, card.color_identity) == ("{1}{R}", 2.0, "R")
    assert card.type_line == "Legendary Creature — Dwarf Berserker"
    assert card.commander_legal
    assert not card.game_changer
    assert card.edhrec_rank == 1220


def test_the_mana_a_card_makes_and_its_rarity_are_kept() -> None:
    # Made up on a real card: the fixture lines are trimmed to what other tests read.
    raw = {**real_card("Mountain", "clb"), "produced_mana": ["R"], "rarity": "common"}
    card = parse_card(raw)
    assert card is not None
    assert (card.produced, card.rarity) == ("R", "common")
    signet = parse_card(
        {**real_card("Arcane Signet", "frc"), "produced_mana": ["G", "C", "W", "U", "R", "B"]}
    )
    assert signet is not None
    # In WUBRG order with colourless last, whatever order Scryfall lists them in; no rarity reads as "".
    assert (signet.produced, signet.rarity) == ("WUBRGC", "")


def test_basic_land_has_no_rank() -> None:
    card = parse_card(real_card("Mountain", "clb"))
    assert card is not None
    assert card.edhrec_rank is None


def test_modal_card_takes_its_cost_from_the_front_face() -> None:
    card = parse_card(real_card("Sundering Eruption // Volcanic Fissure", "mh3"))
    assert card is not None
    assert (card.mana_cost, card.cmc, card.type_line) == ("{2}{R}", 3.0, "Sorcery // Land")


def test_the_layout_is_kept() -> None:
    modal = parse_card(real_card("Sundering Eruption // Volcanic Fissure", "mh3"))
    plain = parse_card(real_card("Mountain", "clb"))
    assert modal is not None
    assert plain is not None
    assert (modal.layout, plain.layout) == ("modal_dfc", "normal")


def test_reversible_card_takes_oracle_id_and_cmc_from_its_faces() -> None:
    raw = real_card("Battered Golem", "5dn")
    del raw["oracle_id"], raw["cmc"], raw["mana_cost"], raw["type_line"]
    raw["layout"] = "reversible_card"
    face = {
        "oracle_id": GOLEM_ORACLE,
        "cmc": 3.0,
        "mana_cost": "{3}",
        "type_line": "Artifact Creature — Golem",
    }
    raw["card_faces"] = [face, dict(face)]
    card = parse_card(raw)
    assert card is not None
    assert (card.printing.oracle_id, card.cmc, card.mana_cost) == (GOLEM_ORACLE, 3.0, "{3}")
    assert card.type_line == "Artifact Creature — Golem // Artifact Creature — Golem"


def test_digital_printings_and_tokens_are_skipped() -> None:
    assert parse_card(real_card("Magda, Brazen Outlaw", "prm")) is None
    token = real_card("Battered Golem", "5dn") | {"layout": "token"}
    assert parse_card(token) is None


def test_game_changer_flag_is_read() -> None:
    card = parse_card(real_card("Battered Golem", "5dn") | {"game_changer": True})
    assert card is not None
    assert card.game_changer


def comma_price(card: Json) -> None:
    card["prices"]["eur"] = "1,50"


def negative_price(card: Json) -> None:
    card["prices"]["eur"] = "-1.00"


def unknown_colour(card: Json) -> None:
    card["color_identity"] = ["R", "P"]


def no_oracle_id(card: Json) -> None:
    del card["oracle_id"]


def text_cmc(card: Json) -> None:
    card["cmc"] = "2"


def text_rank(card: Json) -> None:
    card["edhrec_rank"] = "1220"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (comma_price, "not a price"),
        (negative_price, "not a price"),
        (unknown_colour, "unknown colours"),
        (no_oracle_id, "no oracle_id"),
        (text_cmc, "expected a number"),
        (text_rank, "edhrec_rank"),
    ],
)
def test_malformed_cards_are_rejected(change: Callable[[Json], None], message: str) -> None:
    raw = real_card("Magda, Brazen Outlaw", "khm")
    change(raw)
    with pytest.raises(ShapeError, match=message):
        parse_card(raw)


def test_reads_cards_and_tolerates_a_few_bad_lines(tmp_path: Path) -> None:
    lines = [*card_lines(), "{not json", json.dumps({"object": "card", "layout": "normal"})]
    cards_gz, _ = write_bulk(tmp_path, cards=lines)
    assert sum(1 for _ in read_cards(cards_gz)) == PRINTINGS_IN_FIXTURE


def test_refuses_a_file_with_too_many_bad_lines(tmp_path: Path) -> None:
    lines = [*card_lines(), *("{not json" for _ in range(200))]
    cards_gz, _ = write_bulk(tmp_path, cards=lines)
    with pytest.raises(ScryfallError, match="200 of 10229 entries unreadable"):
        list(read_cards(cards_gz))


def test_refuses_a_suspiciously_small_file(tmp_path: Path) -> None:
    cards_gz, _ = write_bulk(tmp_path, cards=[filler_card(i) for i in range(50)])
    with pytest.raises(ScryfallError, match="only 50 usable entries"):
        list(read_cards(cards_gz))


def test_refuses_an_absurdly_long_line(tmp_path: Path) -> None:
    cards_gz, _ = write_bulk(tmp_path, cards=["[" + " " * 1024 * 1024 + "]"])
    with pytest.raises(ScryfallError, match="line 1 is over 1048576 characters"):
        list(read_cards(cards_gz))


def test_refuses_a_file_that_is_not_gzip(tmp_path: Path) -> None:
    path = tmp_path / "cards.jsonl.gz"
    path.write_text("<html>maintenance</html>", encoding="utf-8")
    with pytest.raises(ScryfallError, match="cannot read downloaded"):
        list(read_cards(path))


def test_roles_include_sub_tags_and_leave_out_land_tutors(tmp_path: Path) -> None:
    real_tags = fixture_lines("scryfall_tags.jsonl") + [filler_tag(i) for i in range(FILLER_TAGS)]
    _, tags_gz = write_bulk(tmp_path, tags=real_tags)
    members = role_members(read_tags(tags_gz))
    assert members[Role.RAMP] == {MAGDA_ORACLE}
    # Battered Golem is tagged tutor-land, which tutor excludes; tutor-artifact also loops back to tutor.
    assert members[Role.TUTOR] == {MAGDA_ORACLE}
    assert members[Role.REMOVAL] == {FLUTE_ORACLE}
    assert members[Role.BOARD_WIPE] == {FLUTE_ORACLE}
    assert members[Role.DRAW] == frozenset()
    assert members[Role.EXTRA_TURN] == frozenset()


def removal_tag(tag_id: str, slug: str, members: list[str], children: tuple[str, ...] = ()) -> str:
    return json.dumps(
        {
            "object": "tag",
            "id": tag_id,
            "slug": slug,
            "type": "oracle",
            "child_ids": list(children),
            "taggings": [{"oracle_id": member, "weight": "median"} for member in members],
        }
    )


# Real Tagger ids; removal-nonland sits under both removal-artifact and removal-creature, as on Tagger.
REMOVAL_ARTIFACT = "ea1ae714-3500-481b-9bdd-f33c57db8678"
REMOVAL_CREATURE = "d91e421d-ff87-4aad-a4bf-5aefc51252b6"
REMOVAL_NONLAND = "89d7c9d3-f820-4b16-8507-75977dba8d39"


def test_a_sweeper_removing_only_noncreatures_is_a_board_wipe_only_by_its_text(
    tmp_path: Path,
) -> None:
    # Disruptor Flute is the fixture's one sweeper.
    artifact_only = removal_tag(REMOVAL_ARTIFACT, "removal-artifact", [FLUTE_ORACLE], (REMOVAL_NONLAND,))
    _, tags_gz = write_bulk(tmp_path, tags=[*tag_lines(), artifact_only])
    tags = read_tags(tags_gz)
    assert role_members(tags)[Role.BOARD_WIPE] == frozenset()
    assert role_gates(tags) == (TextGate(Role.BOARD_WIPE, frozenset({FLUTE_ORACLE}), "each creature"),)


def test_a_sweeper_also_tagged_with_a_tag_reaching_creatures_stays_a_board_wipe(tmp_path: Path) -> None:
    artifact = removal_tag(REMOVAL_ARTIFACT, "removal-artifact", [FLUTE_ORACLE], (REMOVAL_NONLAND,))
    creature = removal_tag(REMOVAL_CREATURE, "removal-creature", [], (REMOVAL_NONLAND,))
    # Nonland is under removal-artifact too, but it reaches creatures, like Culling Ritual.
    nonland = removal_tag(REMOVAL_NONLAND, "removal-nonland", [FLUTE_ORACLE])
    _, tags_gz = write_bulk(tmp_path, tags=[*tag_lines(), artifact, creature, nonland])
    tags = read_tags(tags_gz)
    assert role_members(tags)[Role.BOARD_WIPE] == {FLUTE_ORACLE}
    assert role_gates(tags) == (TextGate(Role.BOARD_WIPE, frozenset(), "each creature"),)


def test_role_whose_tag_is_gone_is_left_out(tmp_path: Path) -> None:
    _, tags_gz = write_bulk(tmp_path)
    assert Role.MASS_LAND_DENIAL not in role_members(read_tags(tags_gz))


def test_art_tags_are_ignored(tmp_path: Path) -> None:
    _, tags_gz = write_bulk(tmp_path, tags=tag_lines())
    assert "9d1b4a43-0000-4000-8000-000000000001" not in read_tags(tags_gz)


def test_first_refresh_downloads_and_builds(tmp_path: Path) -> None:
    fake = FakeScryfall()
    result = refresh_card_db(client_for(fake), tmp_path, now=lambda: NOW)
    assert result.status is RefreshStatus.UPDATED
    assert result.printings == PRINTINGS_IN_FIXTURE
    assert fake.requests == ["https://api.scryfall.com/bulk-data", CARDS_URL, TAGS_URL]
    with CardDb(tmp_path / CARD_DB_FILE) as db:
        assert db.meta().built_at == "2026-10-15T12:00:00+00:00"
        assert db.meta().cards_updated_at == "2026-10-15T09:05:41.217+00:00"
    assert sorted(p.name for p in tmp_path.iterdir()) == [CARD_DB_FILE]


def test_sets_listing_reads_the_fixture() -> None:
    sets = {s.code: s for s in parse_sets_listing(FakeScryfall().sets)}
    assert len(sets) == 19
    assert sets["fra"] == SetInfo("fra", "Reality Fracture", date(2026, 10, 2), "expansion", digital=False)
    assert sets["yfra"].digital


def fixture_set(code: str) -> SetInfo:
    return next(s for s in parse_sets_listing(FakeScryfall().sets) if s.code == code)


# core, expansion, masters, draft_innovation, commander and eternal sets.
@pytest.mark.parametrize("code", ["fdn", "fra", "cmm", "mbc", "frc", "hoc"])
def test_sets_with_new_commander_cards_count(code: str) -> None:
    assert fixture_set(code).brings_new_cards


# promo, token, memorabilia, box (twice), funny and a digital alchemy set.
@pytest.mark.parametrize("code", ["pfra", "tfra", "afra", "slz", "sld", "unf", "yfra"])
def test_promo_token_box_and_other_sets_do_not_count(code: str) -> None:
    assert not fixture_set(code).brings_new_cards


def test_a_digital_set_never_counts() -> None:
    assert not replace(fixture_set("fra"), digital=True).brings_new_cards


def test_sets_listing_rejects_a_bad_release_date() -> None:
    with pytest.raises(ShapeError, match="released_at"):
        parse_sets_listing({"data": [{"released_at": "soon"}]})


def test_a_rebuild_says_why_and_a_current_check_says_when_it_is_next_due(tmp_path: Path) -> None:
    fake = FakeScryfall()
    fake.sets = only_sets(fake, "fdn")
    first = refresh_card_db(client_for(fake), tmp_path, now=lambda: NOW)
    assert (first.reason, first.next_due) == ("no usable card data", None)
    kept = refresh_card_db(client_for(fake), tmp_path, now=lambda: NOW + timedelta(days=2))
    assert (kept.reason, kept.next_due) == ("", date(2026, 10, 22))
    old = refresh_card_db(client_for(fake), tmp_path, now=lambda: NOW + timedelta(days=9))
    assert old.reason == "9 days old"
    forced = refresh_card_db(client_for(fake), tmp_path, now=lambda: NOW, force=True)
    assert forced.reason == "forced"


def test_a_rebuild_for_a_new_set_names_the_set(tmp_path: Path) -> None:
    fake = FakeScryfall()
    built = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    refresh(fake, tmp_path, when=built)
    result = refresh_card_db(client_for(fake), tmp_path, now=lambda: built + timedelta(days=2))
    assert result.reason == "new set: Reality Fracture"


def test_data_under_a_week_old_is_current_without_a_download(tmp_path: Path) -> None:
    fake = FakeScryfall()
    fake.sets = only_sets(fake, "fdn", "hob", "slz", "fra", "frc")
    refresh(fake, tmp_path)
    fake.requests.clear()
    assert refresh(fake, tmp_path, when=NOW + timedelta(days=6)) is RefreshStatus.CURRENT
    assert fake.requests == [SETS_URL]


def test_newer_bulk_files_alone_do_not_rebuild(tmp_path: Path) -> None:
    fake = FakeScryfall()
    fake.sets = only_sets(fake, "fdn")
    refresh(fake, tmp_path)
    fake.cards_updated_at = fake.tags_updated_at = "2026-10-16T09:00:00.000+00:00"
    fake.requests.clear()
    assert refresh(fake, tmp_path, when=NOW + timedelta(days=1)) is RefreshStatus.CURRENT
    assert fake.requests == [SETS_URL]


def test_data_a_week_old_is_rebuilt_without_asking_for_sets(tmp_path: Path) -> None:
    fake = FakeScryfall()
    fake.sets = only_sets(fake, "fdn")
    refresh(fake, tmp_path)
    fake.requests.clear()
    assert refresh(fake, tmp_path, when=NOW + timedelta(days=7)) is RefreshStatus.UPDATED
    assert fake.requests == [BULK_URL, CARDS_URL, TAGS_URL]


def test_a_set_released_since_the_build_rebuilds_the_data(tmp_path: Path) -> None:
    fake = FakeScryfall()
    built = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    refresh(fake, tmp_path, when=built)
    fake.requests.clear()
    # Reality Fracture, an expansion, came out on 2026-10-02.
    assert refresh(fake, tmp_path, when=built + timedelta(days=2)) is RefreshStatus.UPDATED
    assert fake.requests == [SETS_URL, BULK_URL, CARDS_URL, TAGS_URL]
    fake.requests.clear()
    assert refresh(fake, tmp_path, when=built + timedelta(days=3)) is RefreshStatus.CURRENT
    assert fake.requests == [SETS_URL]


@pytest.mark.parametrize("code", ["pfra", "tfra", "afra"])
def test_a_promo_token_or_art_set_does_not_rebuild_the_data(tmp_path: Path, code: str) -> None:
    fake = FakeScryfall()
    built = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    refresh(fake, tmp_path, when=built)
    fake.sets = only_sets(fake, code)
    assert refresh(fake, tmp_path, when=built + timedelta(days=2)) is RefreshStatus.CURRENT


def test_a_secret_lair_style_box_set_does_not_rebuild_the_data(tmp_path: Path) -> None:
    fake = FakeScryfall()
    built = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    refresh(fake, tmp_path, when=built)
    # The Zeta Set, a "box" set, came out on 2026-09-02.
    assert refresh(fake, tmp_path, when=built + timedelta(days=5)) is RefreshStatus.CURRENT


def test_a_digital_set_does_not_rebuild_the_data(tmp_path: Path) -> None:
    fake = FakeScryfall()
    built = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    refresh(fake, tmp_path, when=built)
    fake.sets = only_sets(fake, "yfra")
    assert refresh(fake, tmp_path, when=built + timedelta(days=4)) is RefreshStatus.CURRENT


def test_a_set_not_out_yet_does_not_rebuild_the_data(tmp_path: Path) -> None:
    fake = FakeScryfall()
    refresh(fake, tmp_path)
    # Mystery Booster Commander Edition comes out on 2026-10-19, Star Trek on 2026-11-13.
    fake.sets = only_sets(fake, "mbc", "trk")
    assert refresh(fake, tmp_path, when=NOW + timedelta(days=1)) is RefreshStatus.CURRENT


@pytest.mark.parametrize(("released", "status"), [(0, RefreshStatus.CURRENT), (1, RefreshStatus.UPDATED)])
def test_only_a_set_released_after_the_build_day_counts(
    tmp_path: Path, released: int, status: RefreshStatus
) -> None:
    fake = FakeScryfall()
    refresh(fake, tmp_path)
    expansion = next(s for s in fake.sets["data"] if s["code"] == "fra")
    day = (NOW + timedelta(days=released)).date().isoformat()
    fake.sets = {**fake.sets, "data": [{**expansion, "released_at": day}]}
    assert refresh(fake, tmp_path, when=NOW + timedelta(days=1)) is status


def test_failed_sets_check_falls_back_to_the_age_rule(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeScryfall()
    refresh(fake, tmp_path)
    fake.failing[SETS_URL] = 503
    with caplog.at_level(logging.WARNING, logger="commander.scryfall"):
        assert refresh(fake, tmp_path, when=NOW + timedelta(days=6)) is RefreshStatus.CURRENT
    assert any("set list" in message for message in caplog.messages)
    assert refresh(fake, tmp_path, when=NOW + timedelta(days=7)) is RefreshStatus.UPDATED


def test_malformed_sets_answer_falls_back_to_the_age_rule(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeScryfall()
    refresh(fake, tmp_path)
    fake.sets = {"object": "list", "data": [{"code": "fra", "released_at": "soon"}]}
    with caplog.at_level(logging.WARNING, logger="commander.scryfall"):
        assert refresh(fake, tmp_path, when=NOW + timedelta(days=2)) is RefreshStatus.CURRENT
    assert any("set list" in message for message in caplog.messages)


def test_unreadable_build_time_rebuilds_the_data(tmp_path: Path) -> None:
    fake = FakeScryfall()
    refresh(fake, tmp_path)
    with closing(sqlite3.connect(tmp_path / CARD_DB_FILE)) as db, db:
        db.execute("UPDATE meta SET value = 'garbage' WHERE key = 'built_at'")
    assert refresh(fake, tmp_path, when=NOW + timedelta(days=1)) is RefreshStatus.UPDATED


def test_force_rebuilds_even_when_current(tmp_path: Path) -> None:
    fake = FakeScryfall()
    refresh(fake, tmp_path)
    fake.requests.clear()
    assert refresh(fake, tmp_path, force=True) is RefreshStatus.UPDATED
    assert fake.requests == [BULK_URL, CARDS_URL, TAGS_URL]


def test_failed_download_keeps_the_previous_data(tmp_path: Path) -> None:
    fake = FakeScryfall()
    refresh(fake, tmp_path)
    before = (tmp_path / CARD_DB_FILE).read_bytes()
    fake.failing[CARDS_URL] = 404
    with pytest.raises(NetworkError, match="HTTP 404"):
        refresh(fake, tmp_path, when=NOW + timedelta(days=7))
    assert (tmp_path / CARD_DB_FILE).read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [CARD_DB_FILE]


def test_unusable_download_keeps_the_previous_data(tmp_path: Path) -> None:
    fake = FakeScryfall()
    refresh(fake, tmp_path)
    before = (tmp_path / CARD_DB_FILE).read_bytes()
    fake.cards_gz = b"<html>maintenance</html>"
    with pytest.raises(ScryfallError):
        refresh(fake, tmp_path, when=NOW + timedelta(days=7))
    assert (tmp_path / CARD_DB_FILE).read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [CARD_DB_FILE]


def test_listing_without_the_tags_file_is_an_error(tmp_path: Path) -> None:
    fake = FakeScryfall()
    listing = fake.listing()
    listing["data"] = [f for f in listing["data"] if f["type"] != "oracle_tags"]
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=listing))
    client = ScryfallClient(Fetcher(httpx.Client(transport=transport)))
    with pytest.raises(ScryfallError, match="no longer lists the oracle_tags"):
        refresh_card_db(client, tmp_path, now=lambda: NOW)


def test_oversized_bulk_file_is_refused_before_download(tmp_path: Path) -> None:
    fake = FakeScryfall()
    listing = fake.listing()
    listing["data"][1]["compressed_size"] = 600 * 1024 * 1024
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return (
            httpx.Response(200, json=listing) if request.url.path == "/bulk-data" else fake.handler(request)
        )

    client = ScryfallClient(Fetcher(httpx.Client(transport=httpx.MockTransport(handler))))
    with pytest.raises(ScryfallError, match="600 MB, over the size limit"):
        refresh_card_db(client, tmp_path, now=lambda: NOW)
    assert requests == ["https://api.scryfall.com/bulk-data"]


def test_a_gold_bordered_memorabilia_printing_is_not_regular() -> None:
    wc99 = parse_card(real_card("Ancient Tomb", "wc99"))
    masters = parse_card(real_card("Ancient Tomb", "uma"))

    assert wc99 is not None
    assert masters is not None
    assert not wc99.printing.regular
    # The trimmed fixture lines lack these fields; a printing is regular unless Scryfall says otherwise.
    assert masters.printing.regular


@pytest.mark.parametrize(
    ("field", "value", "regular"),
    [
        ("border_color", "silver", False),
        ("border_color", "gold", False),
        ("border_color", "borderless", True),
        ("oversized", True, False),
        ("set_type", "memorabilia", False),
        ("set_type", "commander", True),
    ],
)
def test_regular_printings_are_the_ones_sanctioned_play_accepts(
    field: str, value: object, *, regular: bool
) -> None:
    raw = real_card("Ancient Tomb", "uma")
    raw[field] = value

    card = parse_card(raw)

    assert card is not None
    assert card.printing.regular is regular


def test_card_text_keywords_and_stats_are_read() -> None:
    card = parse_card(real_card("High-Speed Hoverbike", real_card_set("High-Speed Hoverbike")))
    assert card is not None
    assert card.oracle_text.startswith(
        "Flash\nFlying\nWhen this Vehicle enters, tap up to one target creature."
    )
    assert (card.keywords, card.power, card.toughness) == (("Flying", "Crew", "Flash"), "2", "2")


def test_two_faced_card_joins_the_text_of_its_faces() -> None:
    card = parse_card(real_card("Sundering Eruption // Volcanic Fissure", "mh3"))
    assert card is not None
    front, back = card.oracle_text.split(FACE_SEPARATOR)
    assert front.startswith("Destroy target land.")
    assert back.endswith("{T}: Add {R}.")
    assert (card.power, card.toughness) == (None, None)


def test_tag_slug_is_read_and_required() -> None:
    raw: Json = {"object": "tag", "id": "t1", "slug": "stax", "type": "oracle", "child_ids": None}
    raw["taggings"] = []
    tag = parse_tag(raw)
    assert tag is not None
    assert (tag.id, tag.slug, tag.child_ids, tag.oracle_ids) == ("t1", "stax", (), frozenset())
    del raw["slug"]
    with pytest.raises(ShapeError, match="slug"):
        parse_tag(raw)


def test_refresh_stores_every_oracle_tag(tmp_path: Path) -> None:
    refresh(FakeScryfall(), tmp_path)
    with CardDb(tmp_path / CARD_DB_FILE) as db:
        # Filler tags have no role, yet are kept; the art tag is not.
        assert set(db.tag_ids(["combat-ramp", "filler-0", "dwarf"])) == {"combat-ramp", "filler-0"}


def real_card_set(name: str) -> str:
    for line in fixture_lines("scryfall_cards.jsonl"):
        card: Json = json.loads(line)
        if card["name"] == name:
            return card["set"]
    raise AssertionError(f"{name} is not in the fixture")


def test_colours_come_from_the_card_then_its_front_face_then_its_cost() -> None:
    # Talismans are colourless though their mana symbols give them a colour identity.
    talisman = real_card("Battered Golem", "5dn") | {"colors": [], "color_identity": ["U", "B"]}
    flip = real_card("Sundering Eruption // Volcanic Fissure", "mh3")
    flip["card_faces"][0]["colors"] = ["R"]
    parsed = [parse_card(talisman), parse_card(flip), parse_card(real_card("Magda, Brazen Outlaw", "plst"))]
    assert [card.colors if card else None for card in parsed] == ["", "R", "R"]


def test_a_refresh_leaves_another_downloads_temp_files_alone(tmp_path: Path) -> None:
    mine = tmp_path / ".default_cards.jsonl.gz.part"
    theirs = tmp_path / ".oracle_tags.jsonl.gz.part"
    mine.write_bytes(b"a CLI sync is downloading this")
    theirs.write_bytes(b"and this")

    refresh(FakeScryfall(), tmp_path)

    assert (mine.read_bytes(), theirs.read_bytes()) == (b"a CLI sync is downloading this", b"and this")


def test_a_refresh_clears_temp_files_a_crash_left_over_a_day_ago(tmp_path: Path) -> None:
    day = 24 * 3600.0
    old_download = tmp_path / ".default_cards.abc123.jsonl.gz.part"
    old_build = tmp_path / ".cards.sqlite.abc123.building"
    recent_download = tmp_path / ".oracle_tags.def456.jsonl.gz.part"
    other = tmp_path / "notes.txt"
    for path, age in (
        (old_download, 2 * day),
        (old_build, day + 60),
        (recent_download, day - 60),
        (other, 9 * day),
    ):
        path.write_bytes(b"x")
        os.utime(path, (NOW.timestamp() - age, NOW.timestamp() - age))

    refresh(FakeScryfall(), tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == [recent_download.name, CARD_DB_FILE, "notes.txt"]
