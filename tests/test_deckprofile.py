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

[ideas]
commanders = ["The Gitrog Monster", ["Tevesh Szat, Doom of Fools", "Kodama of the East Tree"]]
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
        adjacent=(("The Gitrog Monster",), ("Tevesh Szat, Doom of Fools", "Kodama of the East Tree")),
    )


def test_a_profile_keeps_cards_by_either_face_of_a_name_or_by_category() -> None:
    profile = Profile(keep_cards=("Sol Ring", "Fire // Ice"), keep_categories=("Stax",))
    assert profile.keeps("sol ring")
    # A two-faced card is kept under its front face or its whole name, whichever side spells it so.
    assert profile.keeps("Fire")
    assert profile.keeps("Fire // Ice")
    assert Profile(keep_cards=("Fire",)).keeps("Fire // Ice")
    assert profile.keeps("Mind Stone", ["Ramp", "STAX"])
    assert not profile.keeps("Mind Stone", ["Ramp"])
    assert not profile.keeps("Ice")
    assert not Profile().keeps("Sol Ring", ["Stax"])


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
        ("[ideas]\ncommanders = 'Ygra'", "ideas.commanders must be a list"),
        ("[ideas]\ncommanders = ['']", "ideas.commanders[0] must be a commander or a list of two partners"),
        (
            "[ideas]\ncommanders = [['A', 'B', 'C']]",
            "ideas.commanders[0] must be a commander or a list of two",
        ),
        ("[ideas]\ncommanders = [[]]", "ideas.commanders[0] must be a commander or a list of two"),
        ("[ideas]\ncommanders = [3]", "ideas.commanders[0] must be text"),
        ("[ideas]\ncommanders = [['A', 3]]", "ideas.commanders[0].pair[1] must be text"),
        ("[ideas]\ntags = []", "unknown key 'tags' in [ideas]"),
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


def test_more_adjacent_commanders_than_a_day_of_searches_allows_are_refused() -> None:
    with pytest.raises(ProfileError, match="over 8 commanders"):
        parse_profile({"ideas": {"commanders": [f"Commander {i}" for i in range(9)]}}, "p")


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
    # The profile's own themes go by the name profile; a profile without themes names none.
    assert resolved.labelled == (("profile", resolved.themes),)
    with CardDb(card_db_file) as db:
        assert resolve_themes(Profile(), db).labelled == ()
