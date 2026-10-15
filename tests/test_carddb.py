import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from commander.archidekt import parse_deck
from commander.carddb import CardDataError, CardDb, Role, read_meta
from conftest import CARDS_UPDATED_AT, Json

MAGDA_PLST = "338380ba-8415-40a2-8b81-2b30218ea16a"


def test_missing_database_says_how_to_get_one(tmp_path: Path) -> None:
    with pytest.raises(CardDataError, match="run `cmdr sync`"):
        CardDb(tmp_path / "cards.sqlite")
    assert read_meta(tmp_path / "cards.sqlite") is None


def test_database_from_another_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "cards.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 99")
    with pytest.raises(CardDataError, match="another version of cmdr"):
        CardDb(path)
    assert read_meta(path) is None


def test_file_that_is_not_a_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "cards.sqlite"
    path.write_text("not a database", encoding="utf-8")
    with pytest.raises(CardDataError, match="cmdr sync --force"):
        CardDb(path)


def test_meta_records_the_source_files(card_db_file: Path) -> None:
    meta = read_meta(card_db_file)
    assert meta is not None
    assert meta.cards_updated_at == CARDS_UPDATED_AT


def test_resolves_deck_cards_by_printing(card_db_file: Path, raw_deck: Json) -> None:
    deck = parse_deck(raw_deck)
    with CardDb(card_db_file) as db:
        resolved = {r.card.name: r for r in db.resolve(deck.cards)}
    magda = resolved["Magda, Brazen Outlaw"]
    assert magda.printing is not None
    assert magda.printing.eur_cents == 763
    assert magda.info is not None
    # Cheapest over every paper printing and finish: khm 142 non-foil at 6.79.
    assert magda.info.cheapest_cents == 679
    assert magda.info.roles == {Role.RAMP, Role.TUTOR}
    assert all(r.info is not None for r in resolved.values())


def test_unknown_printing_falls_back_to_the_card_name(card_db_file: Path, raw_deck: Json) -> None:
    deck = parse_deck(raw_deck)
    golem = next(c for c in deck.cards if c.name == "Battered Golem")
    stranger = replace(golem, scryfall_id="00000000-0000-0000-0000-000000000000", name="battered GOLEM")
    with CardDb(card_db_file) as db:
        [resolved] = db.resolve([stranger])
    assert resolved.printing is None
    assert resolved.info is not None
    assert resolved.info.name == "Battered Golem"


def test_card_missing_entirely_resolves_to_nothing(card_db_file: Path, raw_deck: Json) -> None:
    golem = next(c for c in parse_deck(raw_deck).cards if c.name == "Battered Golem")
    stranger = replace(golem, scryfall_id="00000000-0000-0000-0000-000000000000", name="Not A Card")
    with CardDb(card_db_file) as db:
        [resolved] = db.resolve([stranger])
    assert (resolved.info, resolved.printing) == (None, None)


def test_roles_missing_from_the_tag_file_are_unavailable(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        assert Role.MASS_LAND_DENIAL not in db.available_roles
        assert Role.TUTOR in db.available_roles


def test_modal_land_back_does_not_make_a_land(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        eruption = db.cards_named(["Sundering Eruption // Volcanic Fissure"])
        mountain = db.cards_named(["MOUNTAIN"])
    assert not eruption["sundering eruption // volcanic fissure"].is_land
    assert mountain["mountain"].is_land


def test_database_missing_its_tables_is_refused_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "cards.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 1")
    with pytest.raises(CardDataError, match="unreadable"):
        CardDb(path)
