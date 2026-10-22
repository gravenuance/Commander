import json
import logging
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from commander import carddb
from commander.archidekt import parse_deck
from commander.carddb import (
    FACE_SEPARATOR,
    SCHEMA_VERSION,
    CardDataError,
    CardDataMeta,
    CardDb,
    ParsedCard,
    Role,
    TagCount,
    TextGate,
    Theme,
    back_is_land,
    front_name,
    front_type,
    read_meta,
    write_card_db,
)
from commander.scryfall import read_cards
from conftest import (
    ARCANE_SIGNET,
    CARDS_UPDATED_AT,
    CHAOS_WARP,
    LIGHTNING_BOLT,
    MAGDA_ORACLE,
    Json,
    build_card_db,
    card_lines,
    filler_card,
    write_bulk,
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


def test_a_gated_card_joins_its_role_only_when_its_text_holds_the_phrase(tmp_path: Path) -> None:
    cards_gz, _ = write_bulk(tmp_path / "bulk", cards=card_lines())
    path = tmp_path / "cards.sqlite"
    meta = CardDataMeta(CARDS_UPDATED_AT, "2026-10-15T00:00:00+00:00", "2026-10-15T12:00:00+00:00")
    # Lightning Bolt deals 3 damage to any target; Chaos Warp says nothing of the kind. Case doesn't matter.
    gate = TextGate(Role.BOARD_WIPE, frozenset({LIGHTNING_BOLT, CHAOS_WARP}), "Any Target")
    write_card_db(path, read_cards(cards_gz), {Role.BOARD_WIPE: frozenset()}, meta, gates=(gate,))
    with CardDb(path) as db:
        cards = db.cards_named(["Lightning Bolt", "Chaos Warp"])
    assert cards["lightning bolt"].roles == {Role.BOARD_WIPE}
    assert cards["chaos warp"].roles == frozenset()


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


def test_cards_named_matches_a_front_face_like_card_named_and_keys_it_as_asked(card_db_file: Path) -> None:
    asked = ["SUNDERING ERUPTION", "Mountain", "Volcanic Fissure", "Sundering", "Not A Card"]
    with CardDb(card_db_file) as db:
        found = db.cards_named(asked)
        full = db.card_named("Sundering Eruption // Volcanic Fissure")

    # The back face and a partial front face match nothing, as with card_named.
    assert sorted(found) == ["mountain", "sundering eruption"]
    assert full is not None
    assert found["sundering eruption"] == full


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


def test_card_texts_read_many_cards_at_once_and_leave_unknown_ones_out(card_db_file: Path) -> None:
    unknown = "00000000-0000-0000-0000-000000000000"
    with CardDb(card_db_file) as db:
        info = db.card_named("Sundering Eruption")
        assert info is not None
        texts = db.card_texts([MAGDA_ORACLE, info.oracle_id, unknown, MAGDA_ORACLE])
    assert set(texts) == {MAGDA_ORACLE, info.oracle_id}
    magda = texts[MAGDA_ORACLE]
    assert (magda.keywords, magda.power, magda.toughness) == (("treasure",), "2", "1")
    assert magda.tags == ("combat-ramp", "tutor-artifact")
    assert FACE_SEPARATOR in texts[info.oracle_id].text


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


def test_tag_sizes_count_cards_with_sub_tags_and_leave_out_unknown_slugs(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        # Same counts as find_tags: Magda sits in ramp through combat-ramp and is counted once.
        assert db.tag_sizes(["ramp", "combat-ramp", "no-such-tag"]) == {"ramp": 7, "combat-ramp": 1}


def _plan(db_file: Path, sql: str, params: Sequence[object] | Mapping[str, object]) -> str:
    with sqlite3.connect(db_file) as db:
        return "\n".join(str(row[3]) for row in db.execute(f"EXPLAIN QUERY PLAN {sql}", params))


def test_tag_count_and_theme_queries_walk_the_tag_subtree_into_tag_member(card_db_file: Path) -> None:
    # Left alone SQLite scans all of tag_member and probes the subtree for each row.
    internals = vars(carddb)
    cte, condition, params = internals["_theme_sql"](Theme(tag_ids=(RAMP_TAG,)))
    sql = f"{cte} SELECT oracle_id FROM oracle WHERE {condition}"  # noqa: S608 — constant fragments, bound values
    theme_plan = _plan(card_db_file, sql, params)
    count_plan = _plan(card_db_file, internals["_TAG_COUNT_QUERY"], [RAMP_TAG])
    for plan in (theme_plan, count_plan):
        assert "SCAN tag_member" not in plan
        assert "SEARCH tag_member USING" in plan


def test_a_cards_printings_and_roles_are_found_by_index(card_db_file: Path) -> None:
    printings = _plan(card_db_file, "SELECT * FROM printing WHERE oracle_id = ?", [MAGDA_ORACLE])
    roles = _plan(card_db_file, "SELECT role FROM role_member WHERE oracle_id = ?", [MAGDA_ORACLE])
    assert "SEARCH printing USING INDEX printing_oracle" in printings
    assert "SEARCH role_member USING" in roles
    assert "SCAN role_member" not in roles


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


def test_card_mana_reads_each_cards_layout(tmp_path: Path) -> None:
    giant = json.loads(filler_card(30_002, "Test Giant // Test Stomp", 3.0))
    giant.update(layout="adventure", mana_cost="{2}{R} // {1}{R}")
    fire = json.loads(filler_card(30_003, "Test Fire // Test Ice", 4.0))
    fire.update(layout="split", mana_cost="{1}{R} // {1}{U}")
    with CardDb(build_card_db(tmp_path, extra_cards=[json.dumps(giant), json.dumps(fire)])) as db:
        found = db.card_mana(
            [
                "test giant // test stomp",
                "test fire // test ice",
                "Mountain",
                "Sundering Eruption // Volcanic Fissure",
            ]
        )
    assert {name: info.layout for name, info in found.items()} == {
        "test giant // test stomp": "adventure",
        "test fire // test ice": "split",
        "mountain": "normal",
        "sundering eruption // volcanic fissure": "modal_dfc",
    }


def tiny_db_inputs(folder: Path) -> tuple[Path, CardDataMeta]:
    cards_gz, _ = write_bulk(folder / "bulk", cards=card_lines())
    meta = CardDataMeta(CARDS_UPDATED_AT, "2026-10-15T00:00:00+00:00", "2026-10-15T12:00:00+00:00")
    return cards_gz, meta


def test_each_build_works_in_its_own_temp_file(tmp_path: Path) -> None:
    cards_gz, meta = tiny_db_inputs(tmp_path)
    dest = tmp_path / "cards.sqlite"
    seen: list[list[str]] = []

    def watching() -> Iterator[ParsedCard]:
        seen.append(sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".building")))
        yield from read_cards(cards_gz)

    write_card_db(dest, watching(), {}, meta)
    write_card_db(dest, watching(), {}, meta)

    [first], [second] = seen
    assert first != second
    assert all(name.startswith(".cards.sqlite.") for name in (first, second))
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".building")]


def test_a_build_leaves_another_builds_temp_file_alone(tmp_path: Path) -> None:
    cards_gz, meta = tiny_db_inputs(tmp_path)
    foreign = tmp_path / ".cards.sqlite.building"
    foreign.write_bytes(b"another process is building here")

    write_card_db(tmp_path / "cards.sqlite", read_cards(cards_gz), {}, meta)

    assert foreign.read_bytes() == b"another process is building here"


def test_two_builds_into_one_database_do_not_break_each_other(tmp_path: Path) -> None:
    cards_gz, meta = tiny_db_inputs(tmp_path)
    dest = tmp_path / "cards.sqlite"
    in_flight, resume = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def slow() -> Iterator[ParsedCard]:
        cards = read_cards(cards_gz)
        yield next(cards)
        in_flight.set()
        resume.wait(timeout=30)
        yield from cards

    def build_slowly() -> None:
        try:
            write_card_db(dest, slow(), {}, meta)
        except BaseException as error:  # noqa: BLE001 — handed to the assertion below
            errors.append(error)

    builder = threading.Thread(target=build_slowly)
    builder.start()
    try:
        assert in_flight.wait(timeout=30)
        write_card_db(dest, read_cards(cards_gz), {}, meta)
    finally:
        resume.set()
        builder.join(timeout=30)

    assert errors == []
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["cards.sqlite"]
    with CardDb(dest) as db:
        assert db.meta() == meta


def test_a_swap_that_fails_once_because_the_database_is_in_use_is_tried_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cards_gz, meta = tiny_db_inputs(tmp_path)
    dest = tmp_path / "cards.sqlite"
    real_replace = Path.replace
    attempts: list[str] = []
    sleeps: list[float] = []

    def busy_once(self: Path, target: Path) -> Path:
        attempts.append(target.name)
        if len(attempts) == 1:
            raise PermissionError(13, "The process cannot access the file")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", busy_once)

    write_card_db(dest, read_cards(cards_gz), {}, meta, sleep=sleeps.append)

    assert attempts == ["cards.sqlite", "cards.sqlite"]
    assert sleeps == [0.1]
    with CardDb(dest) as db:
        assert db.meta() == meta
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["cards.sqlite"]


def test_a_swap_that_stays_blocked_keeps_the_old_data_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cards_gz, meta = tiny_db_inputs(tmp_path)
    dest = tmp_path / "cards.sqlite"
    write_card_db(dest, read_cards(cards_gz), {}, meta)
    before = dest.read_bytes()
    attempts: list[Path] = []
    sleeps: list[float] = []

    def always_busy(_: Path, target: Path) -> Path:
        attempts.append(target)
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(Path, "replace", always_busy)
    newer = replace(meta, built_at="2026-10-16T12:00:00+00:00")

    with pytest.raises(CardDataError, match=r"in use.*previous card data is kept"):
        write_card_db(dest, read_cards(cards_gz), {}, newer, sleep=sleeps.append)

    assert len(attempts) == 5
    assert sleeps == [0.1, 0.2, 0.4, 0.8]
    assert dest.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["cards.sqlite"]
    assert any(r.levelno == logging.ERROR and "cards.sqlite" in r.getMessage() for r in caplog.records)


def test_two_faced_text_is_read_by_its_front_face() -> None:
    assert (
        front_name("Fable of the Mirror-Breaker // Reflection of Kiki-Jiki") == "Fable of the Mirror-Breaker"
    )
    assert front_type("Sorcery // Land — Forest") == "Sorcery"
    assert front_name("Sol Ring") == "Sol Ring"


def test_only_a_spell_with_a_land_back_is_a_back_face_land() -> None:
    assert back_is_land("Sorcery // Land — Forest")
    assert not back_is_land("Land — Forest")
    assert not back_is_land("Creature — Human // Creature — Werewolf")
    # A land on the front is a land, not a spell with a land back.
    assert not back_is_land("Land // Land")
