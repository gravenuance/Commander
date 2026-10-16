import re
import tomllib
from pathlib import Path

import pytest

from commander.archidekt import parse_deck
from commander.carddb import CardDb, Role, Theme
from commander.deckprofile import (
    PROFILE_FILE,
    Profile,
    ProfileError,
    ThemeSpec,
    parse_profile,
    profile_template,
    read_profile,
    resolve_themes,
    write_profile_template,
)
from commander.models import Deck
from conftest import Json

FULL = """\
version = 1
archetype = "  cEDH stax  "
bracket_ceiling = 4
max_card_eur = 12.5

[targets]
draw = 0
board_wipe = 1

[themes]
types = ["Dinosaur"]
keywords = ["Discover"]
tags = ["HATEBEAR"]
ignore = ["Vehicles"]

[exclude]
tags = ["flicker"]
cards = ["Flickering Hound"]

[keep]
cards = ["Sol Ring"]
categories = ["Stax"]
"""


@pytest.fixture
def deck(raw_deck: Json) -> Deck:
    return parse_deck(raw_deck)


def write(folder: Path, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / PROFILE_FILE
    path.write_text(text, encoding="utf-8")
    return path


def test_full_profile_is_read(tmp_path: Path) -> None:
    write(tmp_path, FULL)
    assert read_profile(tmp_path) == Profile(
        archetype="cEDH stax",
        bracket_ceiling=4,
        max_card_cents=1250,
        targets={Role.DRAW: 0, Role.BOARD_WIPE: 1},
        themes=ThemeSpec(types=("Dinosaur",), keywords=("Discover",), tags=("hatebear",)),
        exclude=ThemeSpec(tags=("flicker",)),
        exclude_cards=("Flickering Hound",),
        keep_cards=("Sol Ring",),
        keep_categories=("Stax",),
        ignore_themes=("Vehicles",),
    )


def test_missing_profile_is_empty(tmp_path: Path) -> None:
    assert read_profile(tmp_path) == Profile()


def test_byte_order_mark_is_accepted(tmp_path: Path) -> None:
    (tmp_path / PROFILE_FILE).write_bytes(b"\xef\xbb\xbfarchetype = 'x'\n")
    assert read_profile(tmp_path).archetype == "x"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("colour = 'W'", "unknown key 'colour'"),
        ("version = 2", "profile version 2 is not supported"),
        ("bracket_ceiling = 6", "bracket_ceiling must be a whole number from 1 to 5"),
        ("bracket_ceiling = true", "bracket_ceiling must be a whole number"),
        ("max_card_eur = -1", "max_card_eur must be an amount in EUR"),
        ("max_card_eur = '5'", "max_card_eur must be an amount in EUR"),
        ("archetype = 3", "archetype must be text"),
        ("themes = 'stax'", "'themes' must be a table"),
        ("[themes]\ncolours = []", "unknown key 'colours' in [themes]"),
        ("[themes]\ntypes = 'Elf'", "themes.types must be a list"),
        ("[themes]\ntypes = ['']", "themes.types[0] is empty"),
        ("[exclude]\ncards = [1]", "exclude.cards[0] must be text"),
        ("[keep]\ntags = []", "unknown key 'tags' in [keep]"),
        ("[exclude]\nignore = []", "unknown key 'ignore' in [exclude]"),
        ("[themes]\nignore = 'Vehicles'", "themes.ignore must be a list"),
        ("[targets]\nlands = 36", "unknown role 'lands'"),
        ("not toml", "cannot read"),
    ],
)
def test_invalid_profiles_name_the_problem(tmp_path: Path, text: str, message: str) -> None:
    path = write(tmp_path, text)
    with pytest.raises(ProfileError, match=re.escape(message)) as caught:
        read_profile(tmp_path)
    assert str(path) in str(caught.value)


def test_too_many_entries_are_refused() -> None:
    with pytest.raises(ProfileError, match="over 100 entries"):
        parse_profile({"themes": {"types": ["Elf"] * 101}}, "p")


def test_oversized_file_is_refused(tmp_path: Path) -> None:
    write(tmp_path, "# " + "x" * 70_000)
    with pytest.raises(ProfileError, match="too big"):
        read_profile(tmp_path)


def test_template_is_valid_and_empty(deck: Deck) -> None:
    raw = tomllib.loads(profile_template(deck))
    assert parse_profile(raw, "template") == Profile()


def test_template_is_never_written_over_a_profile(tmp_path: Path, deck: Deck) -> None:
    path = write_profile_template(tmp_path, deck)
    assert read_profile(tmp_path) == Profile()
    path.write_text("archetype = 'mine'\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="already exists"):
        write_profile_template(tmp_path, deck)
    assert read_profile(tmp_path).archetype == "mine"


def test_tags_resolve_to_ids_and_unknown_ones_are_reported(card_db_file: Path) -> None:
    profile = Profile(
        themes=ThemeSpec(types=("Dwarf",), tags=("combat-ramp", "stax")),
        exclude=ThemeSpec(keywords=("Flash",), tags=("ramp",)),
    )
    with CardDb(card_db_file) as db:
        resolved = resolve_themes(profile, db)
    assert resolved.themes == Theme(types=("Dwarf",), tag_ids=("51403157-c373-4f34-934a-fcc70a47fe57",))
    assert resolved.exclude == Theme(keywords=("Flash",), tag_ids=("2f3e4ad7-5e60-41b4-bdbc-653f16869cf6",))
    assert resolved.unknown_tags == ("stax",)
