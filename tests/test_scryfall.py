import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from commander.carddb import CARD_DB_FILE, CardDb, Role
from commander.jsonshape import ShapeError
from commander.net import Fetcher, NetworkError
from commander.scryfall import (
    RefreshStatus,
    ScryfallClient,
    ScryfallError,
    parse_bulk_listing,
    parse_card,
    read_cards,
    read_tags,
    refresh_card_db,
    role_members,
)
from conftest import (
    CARDS_URL,
    FILLER_TAGS,
    MAGDA_ORACLE,
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


def real_card(name: str, set_code: str) -> Json:
    for line in fixture_lines("scryfall_cards.jsonl"):
        card: Json = json.loads(line)
        if card["name"] == name and card["set"] == set_code:
            return card
    raise AssertionError(f"{name} ({set_code}) is not in the fixture")


def client_for(fake: FakeScryfall) -> ScryfallClient:
    return ScryfallClient(Fetcher(fake.client(), sleep=lambda _: None))


def refresh(fake: FakeScryfall, folder: Path, *, force: bool = False) -> RefreshStatus:
    return refresh_card_db(client_for(fake), folder, now=lambda: NOW, force=force).status


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


def test_basic_land_has_no_rank() -> None:
    card = parse_card(real_card("Mountain", "clb"))
    assert card is not None
    assert card.edhrec_rank is None


def test_modal_card_takes_its_cost_from_the_front_face() -> None:
    card = parse_card(real_card("Sundering Eruption // Volcanic Fissure", "mh3"))
    assert card is not None
    assert (card.mana_cost, card.cmc, card.type_line) == ("{2}{R}", 3.0, "Sorcery // Land")


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


def test_refresh_skips_download_when_scryfall_has_nothing_newer(tmp_path: Path) -> None:
    fake = FakeScryfall()
    refresh(fake, tmp_path)
    fake.requests.clear()
    assert refresh(fake, tmp_path) is RefreshStatus.CURRENT
    assert fake.requests == ["https://api.scryfall.com/bulk-data"]


@pytest.mark.parametrize("field", ["cards_updated_at", "tags_updated_at"])
def test_refresh_rebuilds_when_either_file_is_newer(tmp_path: Path, field: str) -> None:
    fake = FakeScryfall()
    refresh(fake, tmp_path)
    setattr(fake, field, "2026-10-16T09:00:00.000+00:00")
    assert refresh(fake, tmp_path) is RefreshStatus.UPDATED


def test_force_rebuilds_even_when_current(tmp_path: Path) -> None:
    fake = FakeScryfall()
    refresh(fake, tmp_path)
    assert refresh(fake, tmp_path, force=True) is RefreshStatus.UPDATED


def test_failed_download_keeps_the_previous_data(tmp_path: Path) -> None:
    fake = FakeScryfall()
    refresh(fake, tmp_path)
    before = (tmp_path / CARD_DB_FILE).read_bytes()
    fake.cards_updated_at = "2026-10-16T09:00:00.000+00:00"
    fake.failing[CARDS_URL] = 404
    with pytest.raises(NetworkError, match="HTTP 404"):
        refresh(fake, tmp_path)
    assert (tmp_path / CARD_DB_FILE).read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [CARD_DB_FILE]


def test_unusable_download_keeps_the_previous_data(tmp_path: Path) -> None:
    fake = FakeScryfall()
    refresh(fake, tmp_path)
    before = (tmp_path / CARD_DB_FILE).read_bytes()
    fake.cards_updated_at = "2026-10-16T09:00:00.000+00:00"
    fake.cards_gz = b"<html>maintenance</html>"
    with pytest.raises(ScryfallError):
        refresh(fake, tmp_path)
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
