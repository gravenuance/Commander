"""Tokens, emblems and helper cards: reading them from Scryfall's bulk file, keeping them in the card database
and listing what a deck's cards make (ADR 0061). The fixtures are real bulk lines: scryfall_cards.jsonl has
the fixture deck's tokens, scryfall_tokens.jsonl the cards and tokens of the cases below."""

import copy
import json
import logging
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from commander.carddb import (
    BulkItem,
    CardDataMeta,
    CardDb,
    CardToken,
    ParsedCard,
    ParsedToken,
    TokenKind,
    write_card_db,
)
from commander.jsonshape import ShapeError
from commander.models import Deck, DeckCard
from commander.scryfall import parse_bulk_item, parse_card, parse_token
from commander.tokens import (
    COPIES_A_TOKEN,
    CREATES_A_TOKEN,
    DeckTokens,
    creates_a_token,
    deck_tokens,
    describe,
    missing_helper,
    short_text,
    tokens_of_deck,
)
from conftest import MAGDA_ORACLE, Json, fixture_lines

META = CardDataMeta("2026-10-15", "2026-10-15", "2026-10-15T12:00:00+00:00")
TREASURE = "3c549374-6c37-42e0-8d88-a8555d46732d"
# The newest of the fixture's four Treasure printings (set ttrk, released 2026-11-13).
TREASURE_IMAGE = "b8e8c3b4-3b8d-48a4-b427-a2b8ac3e2f8b"
VAMPIRE = "97af46cb-6209-4ae7-81cc-792894999937"
COPY = "88c78601-87f0-45e7-b2e0-e7ffcfb1cb70"
CALLER_OF_THE_CLAW = "ec79255f-4b4d-4a0a-89d8-c59ba640275b"


def real(fixture: str, name: str, set_code: str) -> Json:
    for line in fixture_lines(fixture):
        found: Json = json.loads(line)
        if found["name"] == name and found["set"] == set_code:
            return copy.deepcopy(found)
    raise AssertionError(f"{name} ({set_code}) is not in {fixture}")


def real_token(name: str, set_code: str) -> Json:
    return real("scryfall_tokens.jsonl", name, set_code)


def token_of(name: str, set_code: str, **changes: object) -> ParsedToken:
    parsed = parse_token(real_token(name, set_code) | changes)
    assert parsed is not None
    return parsed


def treasure(set_code: str, **changes: object) -> ParsedToken:
    parsed = parse_token(real("scryfall_cards.jsonl", "Treasure", set_code) | changes)
    assert parsed is not None
    return parsed


def build(tmp_path: Path, *items: BulkItem) -> CardDb:
    path = tmp_path / "cards.sqlite"
    write_card_db(path, items, {}, META)
    return CardDb(path)


def deck_of(*names: str) -> Deck:
    """A deck whose first card leads it; its printings are made up, so each card is found by its name."""
    cards = tuple(
        DeckCard(
            name=name,
            quantity=1,
            categories=("Commander",) if index == 0 else ("Creature",),
            in_deck=True,
            scryfall_id=str(uuid.UUID(int=index + 1)),
            archidekt_card_id=0,
            entry_id=index,
            set_code="",
            collector_number="",
            finish="",
        )
        for index, name in enumerate(names)
    )
    return Deck(1, "Test deck", "TESTER", 3, "2026-10-15T00:00:00Z", cards)


def rows(made: DeckTokens) -> list[tuple[str, TokenKind, tuple[str, ...]]]:
    return [(r.token.name, r.token.kind, r.makers) for r in made.tokens]


# --- reading the bulk file -------------------------------------------------------------------------------


def test_a_token_printing_is_read_with_its_facts() -> None:
    parsed = token_of("Vampire", "tlci")
    assert parsed == ParsedToken(
        CardToken(
            oracle_id=VAMPIRE,
            name="Vampire",
            kind=TokenKind.TOKEN,
            type_line="Token Creature — Vampire",
            power="1",
            toughness="1",
            colors="W",
            oracle_text="Lifelink (Damage dealt by this creature also causes you to gain that much life.)",
            scryfall_id="0484390e-1167-4407-84de-7ddc726e8926",
        ),
        released_at="2023-11-17",
        regular=True,
    )


def test_an_emblem_is_read_as_an_emblem() -> None:
    token = token_of("Elspeth, Sun's Champion Emblem", "tmoc").token
    assert (token.name, token.kind, token.type_line) == (
        "Elspeth, Sun's Champion Emblem",
        TokenKind.EMBLEM,
        "Emblem — Elspeth",
    )
    assert token.oracle_text == "Creatures you control get +2/+2 and have flying."
    assert (token.power, token.colors) == (None, "")


def test_a_double_faced_token_has_both_faces_in_its_name_and_type() -> None:
    token = token_of("Undercity // The Initiative", "tclb").token
    assert token.name == "Undercity // The Initiative"
    assert token.type_line == "Dungeon — Undercity // Card"
    assert token.kind is TokenKind.HELPER
    assert token.oracle_text.count("\n//\n") == 1
    assert token.oracle_text.startswith("You can't enter this dungeon")


def test_a_token_without_an_oracle_id_takes_its_front_faces() -> None:
    raw = real_token("Day // Night", "tmid")
    del raw["oracle_id"]
    raw["card_faces"][0]["oracle_id"] = "dddddddd-0000-4000-8000-000000000001"
    raw["card_faces"][1]["oracle_id"] = "dddddddd-0000-4000-8000-000000000002"
    parsed = parse_token(raw)
    assert parsed is not None
    assert parsed.token.oracle_id == "dddddddd-0000-4000-8000-000000000001"
    raw["card_faces"][0].pop("oracle_id")
    with pytest.raises(ShapeError, match="no oracle_id"):
        parse_token(raw)


@pytest.mark.parametrize(
    ("name", "set_code", "kind"),
    [
        ("Vampire", "tlci", TokenKind.TOKEN),
        ("Elspeth, Sun's Champion Emblem", "tmoc", TokenKind.EMBLEM),
        # Typed Card: the Monarch and Day // Night (a double-faced Card // Card).
        ("The Monarch", "tcn2", TokenKind.HELPER),
        ("Day // Night", "tmid", TokenKind.HELPER),
        # Typed Dungeon // Card.
        ("Undercity // The Initiative", "tclb", TokenKind.HELPER),
        # Typed plain Token, named Copy: the marker for a token that copies another.
        ("Copy", "ttrc", TokenKind.HELPER),
        # Typed Creature: a reminder card for a face-down creature, not a token.
        ("Morph", "tmkc", TokenKind.HELPER),
        # Filed under the flip layout, but typed Token Enchantment: still a token.
        ("Royal // Young Hero", "twoe", TokenKind.TOKEN),
    ],
)
def test_each_token_is_classified(name: str, set_code: str, kind: TokenKind) -> None:
    assert token_of(name, set_code).token.kind is kind


def test_a_checklist_card_is_no_token() -> None:
    # Ixalan Checklist marks a double-faced card in a library; the cards it covers name it as a part.
    assert parse_token(real_token("Ixalan Checklist", "txln")) is None
    assert parse_bulk_item(real_token("Ixalan Checklist", "txln")) is None
    # Core Set 2019's checklist has no "Checklist" in the text, and a numbered one ends in its number.
    nicol = real_token("Ixalan Checklist", "txln") | {
        "name": "Core Set 2019 Checklist",
        "oracle_text": "(Use this card to represent a double-faced Nicol Bolas card in your library.)",
    }
    assert parse_token(nicol) is None
    numbered = real_token("Ixalan Checklist", "txln") | {"name": "Shadows Over Innistrad Checklist 2"}
    assert parse_token(numbered) is None


def test_a_card_linking_a_checklist_makes_no_token(tmp_path: Path) -> None:
    bello = parse_card(real_token("Growing Rites of Itlimoc // Itlimoc, Cradle of the Sun", "lci"))
    assert bello is not None
    assert len(bello.related_ids) == 1
    # The bulk reader hands the checklist line over, if it reads it at all.
    checklist = parse_bulk_item(real_token("Ixalan Checklist", "txln"))
    items = [item for item in (bello, checklist) if item is not None]
    with build(tmp_path, *items) as db:
        assert db.tokens_of([bello.printing.oracle_id]) == {}


def test_the_face_down_reminder_card_a_card_links_is_a_helper(token_db_file: Path) -> None:
    with CardDb(token_db_file) as db:
        made = tokens_of_deck(db, deck_of("Grim Haruspex"))
    assert rows(made) == [("Morph", TokenKind.HELPER, ("Grim Haruspex",))]
    assert describe(made.tokens[0].token) == "Morph"


def test_a_role_token_filed_under_another_layout_stays_a_token(token_db_file: Path) -> None:
    with CardDb(token_db_file) as db:
        made = tokens_of_deck(db, deck_of("Royal Treatment"))
    assert rows(made) == [("Royal // Young Hero", TokenKind.TOKEN, ("Royal Treatment",))]


def test_a_digital_only_token_is_skipped() -> None:
    assert parse_token(real_token("Vampire", "tlci") | {"digital": True}) is None


def test_a_token_in_an_irregular_border_is_not_regular() -> None:
    assert not token_of("Vampire", "tlci", border_color="silver").regular
    assert not token_of("Vampire", "tlci", oversized=True).regular


def test_the_bulk_reader_tells_tokens_from_cards_by_layout_and_by_type() -> None:
    assert isinstance(parse_bulk_item(real_token("Vampire", "tlci")), ParsedToken)
    assert isinstance(parse_bulk_item(real("scryfall_cards.jsonl", "Battered Golem", "5dn")), ParsedCard)
    # Scryfall files some tokens under another layout, and the dungeons as ordinary cards.
    assert isinstance(parse_bulk_item(real_token("Vampire", "tlci") | {"layout": "flip"}), ParsedToken)
    dungeon = real_token("Undercity // The Initiative", "tclb") | {"layout": "normal"}
    assert isinstance(parse_bulk_item(dungeon), ParsedToken)


def test_a_card_keeps_the_printings_its_all_parts_name() -> None:
    chariot = parse_card(real("scryfall_tokens.jsonl", "Esika's Chariot", "khm"))
    assert chariot is not None
    assert chariot.token_ids == (
        "2e07758f-0d1c-47d9-ba5a-43bc2a7423cd",  # Cat
        "2115c141-f2fb-4bfd-a8b5-2943bad9f418",  # Copy
    )
    assert chariot.related_ids == ()


def test_the_monarch_and_emblems_are_related_parts_not_token_parts() -> None:
    jailer = parse_card(real("scryfall_tokens.jsonl", "Palace Jailer", "cn2"))
    assert jailer is not None
    assert (jailer.token_ids, jailer.related_ids) == ((), ("40b79918-22a7-4fff-82a6-8ebfe6e87185",))
    elspeth = parse_card(real("scryfall_tokens.jsonl", "Elspeth, Sun's Champion", "moc"))
    assert elspeth is not None
    assert elspeth.token_ids == ("d0cc09a9-a21b-40ee-8b68-bc084f71737d",)
    assert elspeth.related_ids == ("610ed7d6-362b-434a-8cdc-b16bfe20eaa0",)


def test_a_card_is_not_among_its_own_parts() -> None:
    # Scryfall's all_parts list the card itself too (the fixture's trimmed lines leave it out).
    raw = real("scryfall_cards.jsonl", "Magda, Brazen Outlaw", "khm")
    for component in ("combo_piece", "token"):
        raw["all_parts"].append({"id": raw["id"], "component": component})
    magda = parse_card(raw)
    assert magda is not None
    assert magda.token_ids == ("4ae9f454-4f8c-4123-9886-674bc439dfe7",)
    # The other Kaldheim printing of Magda, named as a combo piece.
    assert magda.related_ids == ("ab551f0e-7a9c-4c99-b199-042ab018dd9a",)


def test_a_card_with_no_all_parts_links_nothing() -> None:
    golem = parse_card(real("scryfall_cards.jsonl", "Battered Golem", "5dn"))
    assert golem is not None
    assert (golem.token_ids, golem.related_ids) == ((), ())


def test_an_all_parts_entry_without_an_id_is_a_shape_error() -> None:
    raw = real("scryfall_tokens.jsonl", "Esika's Chariot", "khm")
    del raw["all_parts"][0]["id"]
    with pytest.raises(ShapeError, match=r"all_parts\.id"):
        parse_card(raw)


# --- the card database ---------------------------------------------------------------------------------


def test_magda_links_the_treasure_once_though_her_printings_name_three_of_its_printings(
    card_db_file: Path,
) -> None:
    with CardDb(card_db_file) as db:
        linked = db.tokens_of([MAGDA_ORACLE])
    assert linked == {
        MAGDA_ORACLE: (
            CardToken(
                oracle_id=TREASURE,
                name="Treasure",
                kind=TokenKind.TOKEN,
                type_line="Token Artifact — Treasure",
                power=None,
                toughness=None,
                colors="",
                oracle_text="{T}, Sacrifice this token: Add one mana of any color.",
                scryfall_id=TREASURE_IMAGE,
            ),
        )
    }


def test_a_card_has_the_tokens_of_all_its_printings(token_db_file: Path) -> None:
    # Caller of the Claw's Cube-set printing names a Bear; its Spanish promo printing names nothing.
    with CardDb(token_db_file) as db:
        [bear] = db.tokens_of([CALLER_OF_THE_CLAW])[CALLER_OF_THE_CLAW]
    assert (bear.name, bear.power, bear.toughness, bear.colors) == ("Bear", "2", "2", "G")


def test_cards_without_tokens_are_left_out(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        assert db.tokens_of(["d6c46906-1339-4440-8cd0-2bb9890d4975", "not-an-id"]) == {}


def test_the_image_is_the_latest_regular_printing(tmp_path: Path) -> None:
    kaldheim, list_, newest = (treasure("tkhm"), treasure("plst"), treasure("ttrk"))
    silver = treasure("ttrk", border_color="silver")
    magda = parse_card(real("scryfall_cards.jsonl", "Magda, Brazen Outlaw", "khm"))
    assert magda is not None

    def image(*printings: ParsedToken) -> str:
        with build(tmp_path, magda, *printings) as db:
            [found] = db.tokens_of([MAGDA_ORACLE])[MAGDA_ORACLE]
        return found.scryfall_id

    # A regular printing beats a later silver-bordered one (The List, released 2026-09-28, over ttrk's
    # silver copy), whatever the order, and a later regular one beats an earlier.
    assert image(kaldheim, list_, silver) == "a23b05a9-5476-493d-8de7-7c14e726e9be"
    assert image(silver, list_, kaldheim) == "a23b05a9-5476-493d-8de7-7c14e726e9be"
    assert image(kaldheim, newest, list_) == "b8e8c3b4-3b8d-48a4-b427-a2b8ac3e2f8b"


def test_a_token_printing_missing_from_the_file_is_dropped_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    raw = real("scryfall_tokens.jsonl", "Esika's Chariot", "khm")
    raw["all_parts"][1]["id"] = str(uuid.UUID(int=1))  # The Copy marker's printing, which the file lacks.
    chariot = parse_card(raw)
    cat = token_of("Cat", "tkhm")
    assert chariot is not None
    with caplog.at_level(logging.INFO, logger="commander.carddb"), build(tmp_path, chariot, cat) as db:
        linked = db.tokens_of([chariot.printing.oracle_id])
    assert [t.name for t in linked[chariot.printing.oracle_id]] == ["Cat"]
    # Nothing the user can act on, so it is information and not a warning.
    [record] = [r for r in caplog.records if "not in the card data" in r.getMessage()]
    assert (record.levelno, record.getMessage()) == (
        logging.INFO,
        "1 token printings named by cards are not in the card data; skipped",
    )


def test_a_related_part_that_is_no_token_is_not_listed(tmp_path: Path) -> None:
    jailer = parse_card(real("scryfall_tokens.jsonl", "Palace Jailer", "cn2"))
    golem = parse_card(real("scryfall_cards.jsonl", "Battered Golem", "5dn"))
    assert jailer is not None
    assert golem is not None
    # The jailer's combo piece here is a plain card, not the Monarch token.
    other = replace(jailer, related_ids=(golem.printing.scryfall_id,))
    with build(tmp_path, other, golem) as db:
        assert db.tokens_of([other.printing.oracle_id]) == {}


def test_token_named_finds_the_copy_marker(token_db_file: Path) -> None:
    with CardDb(token_db_file) as db:
        copy_marker = db.token_named("Copy")
        assert copy_marker is not None
        assert (copy_marker.oracle_id, copy_marker.kind) == (COPY, TokenKind.HELPER)
        assert db.token_named("No such token") is None


# --- what a deck makes --------------------------------------------------------------------------------


ALL_CASES = (
    "Magda, Brazen Outlaw",
    "Charismatic Conqueror",
    "Preacher of the Schism",
    "Esika's Chariot",
    "Palace Jailer",
    "Ravenloft Adventurer",
    "Caller of the Claw",
    "Elspeth, Sun's Champion",
    "Stormsplitter",
    "Lazav, the Multifarious",
    "Monkey Cage",
)


def test_a_deck_lists_its_tokens_by_makers_then_name(token_db_file: Path) -> None:
    with CardDb(token_db_file) as db:
        made = tokens_of_deck(db, deck_of(*ALL_CASES))
    assert rows(made) == [
        # Two makers each: Esika's Chariot links the Copy marker and Stormsplitter's text copies itself.
        ("Copy", TokenKind.HELPER, ("Esika's Chariot", "Stormsplitter")),
        ("Vampire", TokenKind.TOKEN, ("Charismatic Conqueror", "Preacher of the Schism")),
        ("Bear", TokenKind.TOKEN, ("Caller of the Claw",)),
        ("Cat", TokenKind.TOKEN, ("Esika's Chariot",)),
        ("Elspeth, Sun's Champion Emblem", TokenKind.EMBLEM, ("Elspeth, Sun's Champion",)),
        ("Soldier", TokenKind.TOKEN, ("Elspeth, Sun's Champion",)),
        ("The Monarch", TokenKind.HELPER, ("Palace Jailer",)),
        ("Treasure", TokenKind.TOKEN, ("Magda, Brazen Outlaw",)),
        ("Undercity // The Initiative", TokenKind.HELPER, ("Ravenloft Adventurer",)),
    ]


def test_a_card_that_says_it_creates_a_token_but_links_none_is_left_to_check_by_hand(
    token_db_file: Path,
) -> None:
    with CardDb(token_db_file) as db:
        made = tokens_of_deck(db, deck_of(*ALL_CASES))
    # Monkey Cage creates Monkeys, which Scryfall does not link. Stormsplitter creates a copy; Lazav's text
    # only becomes a copy of a card; the rest link their tokens.
    assert made.check_by_hand == ("Monkey Cage",)


def test_a_copy_maker_is_one_copy_row_whether_or_not_scryfall_links_it(token_db_file: Path) -> None:
    with CardDb(token_db_file) as db:
        unlinked = tokens_of_deck(db, deck_of("Stormsplitter"))
        both = tokens_of_deck(db, deck_of("Stormsplitter", "Esika's Chariot", "Splinter Twin"))
    assert rows(unlinked) == [("Copy", TokenKind.HELPER, ("Stormsplitter",))]
    assert unlinked.tokens[0].token.oracle_id == COPY
    assert unlinked.tokens[0].token.scryfall_id == "2115c141-f2fb-4bfd-a8b5-2943bad9f418"
    [copy_row] = [r for r in both.tokens if r.token.name == "Copy"]
    assert copy_row.makers == ("Esika's Chariot", "Splinter Twin", "Stormsplitter")


def test_a_card_that_only_becomes_a_copy_makes_no_token(token_db_file: Path) -> None:
    with CardDb(token_db_file) as db:
        made = tokens_of_deck(db, deck_of("Lazav, the Multifarious"))
    assert made == DeckTokens((), ())


def test_a_deck_whose_cards_make_no_token_has_an_empty_list(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        made = tokens_of_deck(db, deck_of("Battered Golem", "Mountain", "Disruptor Flute"))
    assert made == DeckTokens((), ())


def test_a_card_the_card_data_does_not_know_makes_nothing(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        assert tokens_of_deck(db, deck_of("Magda, Brazen Outlaw", "No Such Card")).tokens[0].makers == (
            "Magda, Brazen Outlaw",
        )


def test_deck_tokens_needs_only_the_resolved_cards(card_db_file: Path) -> None:
    with CardDb(card_db_file) as db:
        resolved = db.resolve(deck_of("Magda, Brazen Outlaw").main_cards)
        assert rows(deck_tokens(db, resolved)) == [("Treasure", TokenKind.TOKEN, ("Magda, Brazen Outlaw",))]


def test_the_copy_marker_is_listed_without_an_image_when_the_card_data_lacks_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    storm = parse_card(real("scryfall_tokens.jsonl", "Stormsplitter", "pblb"))
    assert storm is not None
    with caplog.at_level(logging.WARNING, logger="commander.tokens"), build(tmp_path, storm) as db:
        made = tokens_of_deck(db, deck_of("Stormsplitter"))
    [row] = made.tokens
    assert (row.token.name, row.token.kind, row.token.scryfall_id, row.makers) == (
        "Copy",
        TokenKind.HELPER,
        "",
        ("Stormsplitter",),
    )
    assert "has no Copy marker" in caplog.text


# --- the text rules -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Whenever you cast an instant or sorcery spell, create a token that's a copy of this creature.",
        "Create two tokens that are copies of target creature you control.",
        "Create a token that\u2019s a copy of target artifact or creature.",
        "Populate. (To populate, create a token that's a copy of a creature token you control.)",
        "When this enters, create X tokens that are copies of it.",
        "Create a token copy of target creature, except it's a Zombie.",
        "Put a copy of target token you control onto the battlefield.",
        "Storm (When you cast this spell, copy it for each spell cast before it. Copies become tokens.)",
        "Whenever you cast a Faerie or Wizard permanent spell, copy it. (The copy becomes a token.)",
        "{2}, {T}: Choose one —\n• Populate.\n• Proliferate.",
        "Devoid (This card has no color.)\nMyriad\nWhen this creature leaves the battlefield, draw a card.",
        "Trample, myriad",
    ],
)
def test_text_that_copies_a_token_is_recognised(text: str) -> None:
    assert COPIES_A_TOKEN.search(text)


@pytest.mark.parametrize(
    "text",
    [
        "{X}: Lazav becomes a copy of target creature card in your graveyard with mana value X.",
        "Copy target instant or sorcery spell you control. You may choose new targets for the copy.",
        "Create a 1/1 white Soldier creature token.",
        "Creature tokens you control have haste.",
        "Exile target token. Create a Treasure token. You may copy a spell.",
        "Myriad Landscape enters tapped.\n{2}, {T}, Sacrifice Myriad Landscape: Search your library.",
        "Copy target spell. If the copy would be put onto the stack, it becomes a card instead.",
    ],
)
def test_text_that_makes_no_token_copy_is_not(text: str) -> None:
    assert not COPIES_A_TOKEN.search(text)


@pytest.mark.parametrize(
    ("text", "creates"),
    [
        ("When a creature enters, create X 2/2 green Monkey creature tokens.", True),
        ("At the beginning of your upkeep, you create a Food token.", True),
        ("Creature tokens you control have haste.", False),
        ("Destroy target token. It's created again. Draw a card.", False),
        ("Create a Treasure. Tokens you control get +1/+1.", False),
    ],
)
def test_text_that_creates_a_token_is_recognised(text: str, creates: bool) -> None:  # noqa: FBT001
    assert bool(CREATES_A_TOKEN.search(text)) is creates
    assert creates_a_token(text) is creates


DOUBLER = "If an effect would create one or more tokens, it creates twice that many of those tokens instead."
SOLDIER = "When this enters, create a 1/1 white Soldier creature token."


@pytest.mark.parametrize(
    ("text", "creates"),
    [
        (DOUBLER, False),
        ("Whenever you create or sacrifice a token, each opponent loses 1 life.", False),
        (
            "Whenever you create or sacrifice a token, draw a card.\nAt the end step, create a Food token.",
            True,
        ),
        (f"{DOUBLER}\n{SOLDIER}", True),
    ],
)
def test_token_doublers_create_nothing_but_another_sentence_may(text: str, creates: bool) -> None:  # noqa: FBT001
    assert creates_a_token(text) is creates


MONARCH = token_of("The Monarch", "tcn2").token
EMBLEM = token_of("Elspeth, Sun's Champion Emblem", "tmoc").token
UNDERCITY = token_of("Undercity // The Initiative", "tclb").token
A_TREASURE = treasure("ttrk").token


@pytest.mark.parametrize(
    ("text", "made_by"),
    [
        ("When this enters, you become the monarch.", MONARCH),
        ("Whenever a creature attacks, target player becomes the monarch.", MONARCH),
        ('-7: You get an emblem with "Creatures you control get +2/+2."', EMBLEM),
        ("When this enters, you take the initiative.", UNDERCITY),
        ("When this enters, venture into the dungeon.", UNDERCITY),
    ],
)
def test_a_card_that_says_it_makes_a_helper_is_flagged_unless_it_links_one(
    text: str, made_by: CardToken
) -> None:
    assert missing_helper(text, [])
    assert missing_helper(text, [A_TREASURE])
    assert not missing_helper(text, [A_TREASURE, made_by])


def test_the_ring_and_ascend_wordings_are_in_the_table() -> None:
    assert missing_helper("When this enters, the Ring tempts you.", [])
    assert missing_helper("Ascend (If you control ten or more permanents, you get the city's blessing.)", [])
    assert not missing_helper("As long as you're the monarch, creatures can't attack you.", [])
    assert not missing_helper("Ascended Lawmage and the Ascendant Spirit.", [])


# --- showing a token ----------------------------------------------------------------------------------


def test_a_creature_token_is_described_by_colour_stats_name_and_what_it_does() -> None:
    assert describe(token_of("Vampire", "tlci").token) == "W 1/1 Vampire (Lifelink)"
    assert describe(token_of("Soldier", "tmoc").token) == "W 1/1 Soldier"
    assert describe(token_of("Bear", "tc15").token) == "G 2/2 Bear"


def test_other_tokens_are_described_without_stats_or_a_hint() -> None:
    assert describe(treasure("ttrk").token) == "C Treasure"
    assert describe(token_of("Elspeth, Sun's Champion Emblem", "tmoc").token) == (
        "Elspeth, Sun's Champion Emblem"
    )
    assert describe(token_of("The Monarch", "tcn2").token) == "The Monarch"
    assert describe(token_of("Copy", "ttrc").token) == "Copy"


def test_short_text_drops_reminder_text_and_cuts_long_text() -> None:
    assert short_text(token_of("Vampire", "tlci").token) == "Lifelink"
    monarch = token_of("The Monarch", "tcn2").token
    # Lines join with a semicolon, each without its full stop, so the punctuation reads right.
    assert short_text(monarch, 200) == (
        "At the beginning of your end step, draw a card; "
        "Whenever a creature deals combat damage to you, its controller becomes the monarch"
    )
    assert short_text(monarch, 20) == "At the beginning of…"
    assert short_text(token_of("Copy", "ttrc").token) == ""


def test_a_creature_tokens_hint_reads_as_text(token_db_file: Path) -> None:
    with CardDb(token_db_file) as db:
        [row] = tokens_of_deck(db, deck_of("Fanatic of Rhonas")).tokens
    # Two lines of its text, each without its full stop: the second is cut.
    # The cut falls inside a mana symbol, which is left out whole rather than split.
    assert describe(row.token) == "B 4/4 Fanatic of Rhonas ({T}: Add {G}; Ferocious — {T}: Add {G}…)"


# --- cards the links and the text rules cover ---------------------------------------------------------


def made(token_db_file: Path, *names: str) -> DeckTokens:
    with CardDb(token_db_file) as db:
        return tokens_of_deck(db, deck_of(*names))


def test_cards_that_make_a_token_copy_by_populate_myriad_or_a_copy_becoming_a_token_are_copy_makers(
    token_db_file: Path,
) -> None:
    got = made(
        token_db_file,
        "Cayth, Famed Mechanist",
        "Chittering Dispatcher",
        "Lithoform Engine",
        "Archmage of Echoes",
    )
    [copy_row] = [r for r in got.tokens if r.token.name == "Copy"]
    # Cayth's populate, Chittering Dispatcher's myriad, Lithoform Engine's and Archmage of Echoes' copies.
    assert copy_row.makers == (
        "Archmage of Echoes",
        "Cayth, Famed Mechanist",
        "Chittering Dispatcher",
        "Lithoform Engine",
    )


def test_a_card_that_embalms_or_eternalizes_makes_its_own_token_and_no_copy(token_db_file: Path) -> None:
    got = made(token_db_file, "Fanatic of Rhonas")
    assert rows(got) == [("Fanatic of Rhonas", TokenKind.TOKEN, ("Fanatic of Rhonas",))]


def test_token_doublers_are_not_cards_to_check_by_hand(token_db_file: Path) -> None:
    got = made(token_db_file, "Doubling Season", "Parallel Lives", "Anointed Procession", "Mirkwood Bats")
    assert got == DeckTokens((), ())


def test_a_helper_a_card_says_it_makes_but_does_not_link_is_left_to_check_by_hand(
    token_db_file: Path,
) -> None:
    got = made(
        token_db_file,
        "Queen Mother Ramonda",
        "Karn, Living Legacy",
        "Keen-Eared Sentry",
        # Its name has the word ascend, but the card has no ascend ability.
        "Ascend from Avernus",
        # These link their Monarch, Initiative and emblem, and are not listed.
        "Palace Jailer",
        "Ravenloft Adventurer",
        "Elspeth, Sun's Champion",
    )
    assert got.check_by_hand == (
        "Karn, Living Legacy",
        "Keen-Eared Sentry",
        "Queen Mother Ramonda",
    )
    # Karn links a Powerstone but no emblem; the Powerstone is still his.
    assert ("Powerstone", ("Karn, Living Legacy",)) in [(r.token.name, r.makers) for r in got.tokens]
