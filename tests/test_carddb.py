import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from commander.archidekt import parse_deck
from commander.carddb import (
    FACE_SEPARATOR,
    SCHEMA_VERSION,
    CardDataError,
    CardDb,
    Role,
    TagCount,
    Theme,
    read_meta,
)
from conftest import (
    ARCANE_SIGNET,
    CARDS_UPDATED_AT,
    LIGHTNING_BOLT,
    MAGDA_ORACLE,
    Json,
    build_card_db,
    filler_card,
)

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
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    with pytest.raises(CardDataError, match="unreadable"):
        CardDb(path)


def ranked_names(db: CardDb, role: Role, identity: str, max_cents: int | None = None) -> list[str]:
    return [c.name for c in db.candidates(role, identity, exclude={MAGDA_ORACLE}, max_cents=max_cents)]


def test_candidates_are_the_most_played_legal_nonland_cards_first(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        # Not Ancient Tomb (a land), Mana Crypt (banned), the unranked filler card or Magda (excluded).
        assert ranked_names(db, Role.RAMP, "R") == ["Arcane Signet", "Mind Stone", "Jeska's Will"]
        everything = [c.name for c in db.candidates(Role.RAMP, "R", exclude=())]
    assert everything == ["Arcane Signet", "Mind Stone", "Jeska's Will", "Magda, Brazen Outlaw"]


def test_candidates_stay_within_the_colour_identity(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        assert ranked_names(db, Role.DRAW, "R") == []
        assert ranked_names(db, Role.DRAW, "UR") == ["Rhystic Study"]
        # Disruptor Flute is colourless, so it fits every identity.
        assert ranked_names(db, Role.REMOVAL, "") == ["Disruptor Flute"]
        assert ranked_names(db, Role.REMOVAL, "WUBRG") == ["Chaos Warp", "Lightning Bolt", "Disruptor Flute"]


def test_candidates_over_the_price_limit_or_unpriced_are_left_out(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        # Arcane Signet costs EUR 0.30; Mind Stone has no EUR price; Jeska's Will costs EUR 33.80.
        assert ranked_names(db, Role.RAMP, "R", max_cents=30) == ["Arcane Signet"]
        assert ranked_names(db, Role.RAMP, "R", max_cents=29) == []


def test_candidates_carry_their_rank_and_roles(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        [will] = [c for c in db.candidates(Role.RAMP, "R", exclude=()) if c.name == "Jeska's Will"]
    assert (will.edhrec_rank, will.game_changer, will.cheapest_cents) == (107, True, 3380)
    assert will.roles == {Role.RAMP}


def test_rank_is_read_and_basics_have_none(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        cards = db.cards_named(["Magda, Brazen Outlaw", "Mountain"])
    assert cards["magda, brazen outlaw"].edhrec_rank == 1220
    assert cards["mountain"].edhrec_rank is None


def test_card_named_matches_the_full_name_or_the_front_face(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        full = db.card_named("SUNDERING ERUPTION // VOLCANIC FISSURE")
        front = db.card_named("sundering eruption")
        assert db.card_named("Volcanic Fissure") is None
        assert db.card_named("Sundering") is None
        assert db.card_named("Mountain") is not None

    assert full is not None
    assert front == full


def test_printings_of_lists_every_printing_in_set_order(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        printings = db.printings_of(MAGDA_ORACLE)
        assert db.printings_of("no-such-card") == []

    assert [(p.set_code, p.collector_number) for p in printings] == [
        ("khm", "142"),
        ("khm", "312"),
        ("pkhm", "142p"),
        ("pkhm", "142s"),
        ("plst", "KHM-142"),
        ("sld", "1688"),
    ]


def test_cheapest_price_comes_from_regular_printings(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        tomb = db.card_named("Ancient Tomb")
        assert tomb is not None
        printings = {p.set_code: p for p in db.printings_of(tomb.oracle_id)}

    # The gold-bordered WC99 copy (EUR 41.63) is cheaper, but not a regular printing.
    assert tomb.cheapest_cents == 10264
    assert (printings["wc99"].eur_cents, printings["wc99"].regular) == (4163, False)
    assert printings["uma"].regular


RAMP_TAG = "2f3e4ad7-5e60-41b4-bdbc-653f16869cf6"
TUTOR_TAG = "c768d2ec-3264-4a90-a98f-bea8467857d3"


def test_card_text_carries_rules_text_keywords_stats_and_own_tags(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        text = db.card_text(MAGDA_ORACLE)
    assert text is not None
    assert text.text.startswith("Other Dwarves you control get +1/+0.\nWhenever a Dwarf you control")
    assert (text.keywords, text.power, text.toughness) == (("treasure",), "2", "1")
    # The real tag lines put Magda under combat-ramp and tutor-artifact.
    assert text.tags == ("combat-ramp", "tutor-artifact")


def test_card_text_of_a_two_faced_card_joins_the_faces(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        info = db.card_named("Sundering Eruption")
        assert info is not None
        text = db.card_text(info.oracle_id)
    assert text is not None
    front, back = text.text.split(FACE_SEPARATOR)
    assert front.startswith("Destroy target land.")
    assert "{T}: Add {R}." in back


def test_card_text_of_an_unknown_card_is_none(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        assert db.card_text("00000000-0000-0000-0000-000000000000") is None


def test_theme_matches_whole_type_words_only(card_db_file: Path) -> None:
    deck = {MAGDA_ORACLE, ARCANE_SIGNET}
    with CardDb(card_db_file) as db:
        assert db.matching(Theme(types=("Dwarf",)), deck) == {MAGDA_ORACLE}
        assert db.matching(Theme(types=("dwarf",)), deck) == {MAGDA_ORACLE}
        assert db.matching(Theme(types=("Dwar",)), deck) == set()
        assert db.matching(Theme(), deck) == set()


def test_theme_matches_keywords_whole(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        flute = db.card_named("Disruptor Flute")
        hoverbike = db.card_named("High-Speed Hoverbike")
        assert flute is not None
        assert hoverbike is not None
        cards = {flute.oracle_id, hoverbike.oracle_id, MAGDA_ORACLE}
        assert db.matching(Theme(keywords=("Flash",)), cards) == {flute.oracle_id, hoverbike.oracle_id}
        assert db.matching(Theme(keywords=("fly",)), cards) == set()


def test_theme_tags_include_their_sub_tags(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        combat_ramp = db.tag_ids(["combat-ramp"])["combat-ramp"]
        assert db.matching(Theme(tag_ids=(combat_ramp,)), {MAGDA_ORACLE, ARCANE_SIGNET}) == {MAGDA_ORACLE}
        assert db.matching(Theme(tag_ids=(RAMP_TAG,)), {MAGDA_ORACLE, ARCANE_SIGNET, LIGHTNING_BOLT}) == {
            MAGDA_ORACLE,
            ARCANE_SIGNET,
        }


def test_a_cycle_in_the_tag_tree_still_ends(card_db_file: Path) -> None:
    # tutor and tutor-artifact list each other as children in the real tag file; Magda is tutor-artifact.
    with CardDb(card_db_file) as db:
        assert db.matching(Theme(tag_ids=(TUTOR_TAG,)), {MAGDA_ORACLE, ARCANE_SIGNET}) == {MAGDA_ORACLE}
        assert any(t.slug == "tutor" for t in db.find_tags("tutor"))


def test_tag_ids_leave_out_unknown_slugs(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        assert db.tag_ids(["ramp", "no-such-tag"]) == {"ramp": RAMP_TAG}


def test_find_tags_counts_cards_with_sub_tags_most_first(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        found = db.find_tags(" RAMP ")
        assert db.find_tags("  ") == []
    # ramp: Magda (also via combat-ramp), Arcane Signet, Mind Stone, Jeska's Will, Ancient Tomb, Mana Crypt
    # and the unranked filler card; combat-ramp: Magda alone.
    assert found == [TagCount("ramp", 7), TagCount("combat-ramp", 1)]


def test_suggested_tags_put_slugs_starting_with_the_text_first(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        found = db.suggest_tags(" Ramp ")
        assert db.suggest_tags("") == []
        assert db.suggest_tags("ramp", limit=1) == found[:1]
    assert [t.slug for t in found] == ["ramp", "combat-ramp"]
    assert found[1] == TagCount("combat-ramp", 1)


def test_theme_candidates_are_ranked_nonland_cards_in_identity(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        names = [c.name for c in db.theme_candidates(Theme(tag_ids=(RAMP_TAG,)), "R", exclude={MAGDA_ORACLE})]
        dwarves = [c.name for c in db.theme_candidates(Theme(types=("Dwarf",)), "R", exclude=())]
        assert list(db.theme_candidates(Theme(), "R", exclude=())) == []
        assert [c.name for c in db.theme_candidates(Theme(types=("Dwarf",)), "U", exclude=())] == []
    # Same order and exclusions as the ramp role: no land, no banned card, no unranked card.
    assert names == ["Arcane Signet", "Mind Stone", "Jeska's Will"]
    assert dwarves == ["Magda, Brazen Outlaw"]


def test_card_mana_reads_what_each_card_makes(tmp_path: Path) -> None:
    land = json.loads(filler_card(30_001, "Test Dual Land", 0.0))
    land.update(type_line="Land", mana_cost="", produced_mana=["U", "W"], rarity="rare")
    with CardDb(build_card_db(tmp_path, extra_cards=[json.dumps(land)])) as db:
        found = db.card_mana(["test dual land", "Mind Stone", "No Such Card"])
    assert sorted(found) == ["mind stone", "test dual land"]
    dual = found["test dual land"]
    assert (dual.name, dual.type_line, dual.produced, dual.rarity) == ("Test Dual Land", "Land", "WU", "rare")
    # The fixture lines carry no produced_mana, so Mind Stone reads as making nothing here.
    assert found["mind stone"].produced == ""
