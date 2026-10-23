"""The app's settings written into profile.toml, comments kept."""

import logging
from pathlib import Path

import pytest

from commander.deckprofile import ProfileError, read_profile
from commander.models import Deck
from commander.profileedit import ensure_grace_table, save_settings

DECK = Deck(1, "Test deck", "me", 3, "2026-10-16", ())
PROFILE = """\
# Chatterfang's profile: the owner's own notes.
version = 1

[themes]
types = ["Squirrel"]  # the tribe
ignore = []

[ideas]
# Lands decks that play like it.
commanders = ["The Gitrog Monster"]
"""


def test_the_templates_grace_example_stays_valid_once_uncommented_after_the_app_saves_settings(
    tmp_path: Path,
) -> None:
    save_settings(tmp_path, DECK, adjacent=[["Ygra, Eater of All"]], ignore=[], tag_search=True)
    path = tmp_path / "profile.toml"
    text = path.read_text(encoding="utf-8")
    assert "\n[grace]\n" in text
    path.write_text(text.replace('# "Basalt Monolith"', '"Basalt Monolith"'), encoding="utf-8")
    profile = read_profile(tmp_path)
    assert profile.tag_search is True
    assert profile.grace == {"Basalt Monolith": 3.0}


GRACE_BLOCK = """\
# Lists holding these cards weigh more in this deck's shares: a factor over 1, up to 10.
[grace]
# "Basalt Monolith" = 3
"""
WITHOUT_GRACE = """\
version = 1
archetype = "Squirrels"

# Lands decks that play like it.
[ideas]
commanders = ["The Gitrog Monster"]
"""


def write(folder: Path, text: str) -> Path:
    path = folder / "profile.toml"
    path.write_bytes(text.encode("utf-8"))
    return path


def test_the_grace_table_goes_in_before_the_ideas_table_and_its_comments(tmp_path: Path) -> None:
    path = write(tmp_path, WITHOUT_GRACE)
    before = read_profile(tmp_path)
    assert ensure_grace_table(tmp_path) is True
    assert path.read_text(encoding="utf-8") == (
        'version = 1\narchetype = "Squirrels"\n\n'
        + GRACE_BLOCK
        + '\n# Lands decks that play like it.\n[ideas]\ncommanders = ["The Gitrog Monster"]\n'
    )
    assert read_profile(tmp_path) == before


def test_the_grace_table_goes_at_the_end_when_there_is_no_ideas_table(tmp_path: Path) -> None:
    path = write(tmp_path, 'version = 1\n[themes]\ntypes = ["Squirrel"]')
    assert ensure_grace_table(tmp_path) is True
    assert path.read_text(encoding="utf-8") == (
        'version = 1\n[themes]\ntypes = ["Squirrel"]\n\n' + GRACE_BLOCK
    )
    assert read_profile(tmp_path).themes.types == ("Squirrel",)


def test_a_profile_with_crlf_line_ends_keeps_them(tmp_path: Path) -> None:
    path = write(tmp_path, WITHOUT_GRACE.replace("\n", "\r\n"))
    assert ensure_grace_table(tmp_path) is True
    raw = path.read_bytes().decode("utf-8")
    assert raw.replace("\r\n", "").count("\n") == 0
    assert GRACE_BLOCK.replace("\n", "\r\n") in raw
    assert raw.index("[grace]") < raw.index("[ideas]")


@pytest.mark.parametrize("text", ["[grace]\n", 'version = 1\n[grace]\n"Sol Ring" = 2\n\n[ideas]\n'])
def test_a_profile_that_has_a_grace_table_is_left_byte_identical(tmp_path: Path, text: str) -> None:
    path = write(tmp_path, text)
    assert ensure_grace_table(tmp_path) is False
    assert path.read_bytes() == text.encode("utf-8")


def test_a_missing_profile_is_not_created(tmp_path: Path) -> None:
    assert ensure_grace_table(tmp_path) is False
    assert not (tmp_path / "profile.toml").exists()


@pytest.mark.parametrize("text", ["not toml [", "colour = 'W'\n[ideas]\n", "bracket_ceiling = 9\n"])
def test_a_broken_profile_is_left_alone_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, text: str
) -> None:
    path = write(tmp_path, text)
    with caplog.at_level(logging.WARNING, logger="commander.profileedit"):
        assert ensure_grace_table(tmp_path) is False
    assert path.read_bytes() == text.encode("utf-8")
    assert "profile.toml" in caplog.text


def test_saving_settings_after_the_migration_keeps_tag_search_under_ideas(tmp_path: Path) -> None:
    write(tmp_path, WITHOUT_GRACE)
    assert ensure_grace_table(tmp_path) is True
    save_settings(tmp_path, DECK, adjacent=[["Ygra, Eater of All"]], ignore=[], tag_search=True)
    profile = read_profile(tmp_path)
    assert profile.tag_search is True
    assert profile.adjacent == (("Ygra, Eater of All",),)
    assert profile.grace == {}


def test_settings_replace_their_two_lists_and_keep_everything_else(tmp_path: Path) -> None:
    (tmp_path / "profile.toml").write_text(PROFILE, encoding="utf-8")
    pair = ["Tevesh Szat, Doom of Fools", "Kodama of the East Tree"]
    save_settings(
        tmp_path,
        DECK,
        adjacent=[["Ygra, Eater of All"], pair],
        ignore=["landfall", "Tokens"],
        tag_search=False,
    )
    text = (tmp_path / "profile.toml").read_text(encoding="utf-8")
    assert "# Chatterfang's profile: the owner's own notes." in text
    assert 'types = ["Squirrel"]  # the tribe' in text
    assert "# Lands decks that play like it." in text
    profile = read_profile(tmp_path)
    assert profile.adjacent == (("Ygra, Eater of All",), tuple(pair))
    assert profile.ignore_themes == ("landfall", "Tokens")
    assert profile.themes.types == ("Squirrel",)


def test_the_tag_search_setting_is_written_in_ideas_and_read_back(tmp_path: Path) -> None:
    (tmp_path / "profile.toml").write_text(PROFILE, encoding="utf-8")
    on = save_settings(tmp_path, DECK, adjacent=[["The Gitrog Monster"]], ignore=[], tag_search=True)
    assert on.tag_search is True
    text = (tmp_path / "profile.toml").read_text(encoding="utf-8")
    assert "tag_search = true" in text
    assert "# Lands decks that play like it." in text
    assert read_profile(tmp_path).tag_search is True
    off = save_settings(tmp_path, DECK, adjacent=[["The Gitrog Monster"]], ignore=[], tag_search=False)
    assert off.tag_search is False
    assert read_profile(tmp_path).tag_search is False
    assert "tag_search = true" not in (tmp_path / "profile.toml").read_text(encoding="utf-8")


def test_a_deck_without_a_profile_gets_the_tag_search_setting_in_the_template(tmp_path: Path) -> None:
    save_settings(tmp_path, DECK, adjacent=[], ignore=[], tag_search=True)
    assert read_profile(tmp_path).tag_search is True


def test_repeats_are_dropped_and_blank_names_left_out(tmp_path: Path) -> None:
    (tmp_path / "profile.toml").write_text(PROFILE, encoding="utf-8")
    profile = save_settings(
        tmp_path,
        DECK,
        adjacent=[["Ygra, Eater of All"], ["ygra, eater of all "], ["  "]],
        ignore=["Tokens", "tokens", ""],
        tag_search=False,
    )
    assert profile.adjacent == (("Ygra, Eater of All",),)
    assert profile.ignore_themes == ("Tokens",)


def test_a_deck_without_a_profile_gets_the_template_with_the_settings(tmp_path: Path) -> None:
    save_settings(tmp_path, DECK, adjacent=[["The Gitrog Monster"]], ignore=[], tag_search=False)
    text = (tmp_path / "profile.toml").read_text(encoding="utf-8")
    assert "# What Test deck" in text
    assert read_profile(tmp_path).adjacent == (("The Gitrog Monster",),)


def test_too_many_commanders_leave_the_file_alone(tmp_path: Path) -> None:
    (tmp_path / "profile.toml").write_text(PROFILE, encoding="utf-8")
    with pytest.raises(ProfileError, match="over 8 commanders"):
        save_settings(
            tmp_path, DECK, adjacent=[[f"Commander {i}"] for i in range(9)], ignore=[], tag_search=False
        )
    assert (tmp_path / "profile.toml").read_text(encoding="utf-8") == PROFILE


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param('version = 1\nideas = "Ygra"\n', "'ideas' must be a table", id="not-a-table"),
        pytest.param("version = 2\n", "profile version 2 is not supported", id="newer-version"),
        pytest.param("# " + "x" * 70_000 + "\n", "too big for a profile", id="over-size-limit"),
    ],
)
def test_a_profile_that_cannot_be_read_as_one_raises_profile_error_and_stays_as_it_was(
    tmp_path: Path, text: str, message: str
) -> None:
    (tmp_path / "profile.toml").write_text(text, encoding="utf-8")
    with pytest.raises(ProfileError, match=message):
        save_settings(tmp_path, DECK, adjacent=[["Ygra, Eater of All"]], ignore=[], tag_search=False)
    assert (tmp_path / "profile.toml").read_text(encoding="utf-8") == text


def test_a_profile_in_another_encoding_raises_profile_error(tmp_path: Path) -> None:
    (tmp_path / "profile.toml").write_bytes(b"version = 1\n# caf\xe9\n")
    with pytest.raises(ProfileError, match="cannot read"):
        save_settings(tmp_path, DECK, adjacent=[], ignore=[], tag_search=False)


WITH_GRACE = """\
# Squirrels, by the owner.
version = 1

# Lists holding these cards weigh more in this deck's shares: a factor over 1, up to 10.
[grace]
# "Basalt Monolith" = 3
"Rings of Brighthearth" = 2.5  # the engine
"Urza's Saga" = 4

# Lands decks that play like it.
[ideas]
commanders = ["The Gitrog Monster"]
"""


def settings_with(folder: Path, grace: dict[str, float], *, tag_search: bool = False) -> None:
    save_settings(
        folder, DECK, adjacent=[["The Gitrog Monster"]], ignore=[], tag_search=tag_search, grace=grace
    )


def test_grace_cards_are_added_changed_and_removed_in_place(tmp_path: Path) -> None:
    path = write(tmp_path, WITH_GRACE)
    both = {"Rings of Brighthearth": 2.5, "Urza's Saga": 4, "Sol Ring": 1.5}
    profile = save_settings(
        tmp_path, DECK, adjacent=[["The Gitrog Monster"]], ignore=[], tag_search=False, grace=both
    )
    assert profile.grace == {"Rings of Brighthearth": 2.5, "Urza's Saga": 4.0, "Sol Ring": 1.5}
    added = path.read_text(encoding="utf-8")
    assert '"Urza\'s Saga" = 4\n"Sol Ring" = 1.5\n\n# Lands decks' in added
    settings_with(tmp_path, {"Rings of Brighthearth": 6, "Sol Ring": 1.5})
    assert read_profile(tmp_path).grace == {"Rings of Brighthearth": 6.0, "Sol Ring": 1.5}
    after = path.read_text(encoding="utf-8")
    # A changed factor keeps its line and trailing comment; a removed card takes only its own line.
    assert '"Rings of Brighthearth" = 6  # the engine\n"Sol Ring" = 1.5\n\n' in after
    assert "Urza" not in after
    settings_with(tmp_path, {})
    assert read_profile(tmp_path).grace == {}


def test_the_grace_edit_keeps_every_comment_and_the_example(tmp_path: Path) -> None:
    path = write(tmp_path, WITH_GRACE)
    settings_with(tmp_path, {"Sol Ring": 2})
    # Everything above [ideas], where the grace lives, is the file as it was but for the cards.
    old = """\
"Rings of Brighthearth" = 2.5  # the engine
"Urza's Saga" = 4
"""
    assert (
        path.read_text(encoding="utf-8").split("[ideas]")[0]
        == (WITH_GRACE.replace(old, '"Sol Ring" = 2\n').split("[ideas]")[0])
    )


def test_a_save_that_leaves_the_grace_as_it_is_leaves_its_text_alone(tmp_path: Path) -> None:
    path = write(tmp_path, WITH_GRACE.replace("= 2.5  # the engine", "=   2.5"))
    settings_with(tmp_path, {"Rings of Brighthearth": 2.5, "Urza's Saga": 4})
    assert "=   2.5" in path.read_text(encoding="utf-8")


def test_a_profile_without_a_grace_table_gets_it_above_ideas_and_the_cards_in_it(tmp_path: Path) -> None:
    path = write(tmp_path, WITHOUT_GRACE)
    settings_with(tmp_path, {"Basalt Monolith": 3})
    assert path.read_text(encoding="utf-8").split("commanders")[0] == (
        'version = 1\narchetype = "Squirrels"\n\n'
        + GRACE_BLOCK
        + '"Basalt Monolith" = 3\n\n# Lands decks that play like it.\n[ideas]\n'
    )


def test_no_grace_cards_add_no_table_to_a_profile_without_one(tmp_path: Path) -> None:
    path = write(tmp_path, WITHOUT_GRACE)
    settings_with(tmp_path, {})
    assert "[grace]" not in path.read_text(encoding="utf-8")


def test_grace_stays_above_ideas_and_tag_search_stays_under_ideas(tmp_path: Path) -> None:
    path = write(tmp_path, WITH_GRACE)
    settings_with(tmp_path, {"Sol Ring": 2}, tag_search=True)
    text = path.read_text(encoding="utf-8")
    assert (
        text.index("[grace]")
        < text.index('"Sol Ring" = 2')
        < text.index("[ideas]")
        < text.index("tag_search = true")
    )
    profile = read_profile(tmp_path)
    assert (profile.tag_search, profile.grace) == (True, {"Sol Ring": 2.0})
    settings_with(tmp_path, {"Sol Ring": 2, "Mind Stone": 3}, tag_search=False)
    again = read_profile(tmp_path)
    assert (again.tag_search, again.grace) == (False, {"Sol Ring": 2.0, "Mind Stone": 3.0})


def test_a_deck_without_a_profile_gets_the_template_with_its_grace_cards(tmp_path: Path) -> None:
    settings_with(tmp_path, {"Basalt Monolith": 3})
    text = (tmp_path / "profile.toml").read_text(encoding="utf-8")
    assert '# "Basalt Monolith" = 3\n"Basalt Monolith" = 3\n' in text
    assert text.index("[grace]") < text.index("[ideas]")
    assert read_profile(tmp_path).grace == {"Basalt Monolith": 3.0}


def test_grace_edits_keep_crlf_line_ends(tmp_path: Path) -> None:
    path = write(tmp_path, WITH_GRACE.replace("\n", "\r\n"))
    settings_with(tmp_path, {"Sol Ring": 2, "Mind Stone": 3})
    raw = path.read_bytes().decode("utf-8")
    assert '"Sol Ring" = 2\r\n"Mind Stone" = 3\r\n\r\n# Lands decks' in raw
    assert raw.split("[ideas]")[0].replace("\r\n", "").count("\n") == 0


@pytest.mark.parametrize(
    ("grace", "message"),
    [
        pytest.param({"Sol Ring": 1}, "grace.Sol Ring must be a number over 1 and at most 10", id="one"),
        pytest.param({"Sol Ring": 0.5}, "grace.Sol Ring must be a number", id="below-one"),
        pytest.param({"Sol Ring": 10.5}, "at most 10", id="over-the-cap"),
        pytest.param({"Sol Ring": float("nan")}, "grace.Sol Ring must be a number", id="not-a-number"),
        pytest.param({"Sol Ring": 2, "sol ring": 3}, "listed twice", id="twice"),
        pytest.param(
            {"Rings of Brighthearth": 2, "RINGS OF BRIGHTHEARTH": 3}, "listed twice", id="twice-cased"
        ),
    ],
)
def test_a_bad_grace_factor_or_repeat_raises_and_leaves_the_file_byte_identical(
    tmp_path: Path, grace: dict[str, float], message: str
) -> None:
    path = write(tmp_path, WITH_GRACE)
    with pytest.raises(ProfileError, match=message):
        settings_with(tmp_path, grace, tag_search=True)
    assert path.read_bytes() == WITH_GRACE.encode("utf-8")


def test_a_grace_written_as_an_inline_table_refuses_a_change_and_is_left_alone(tmp_path: Path) -> None:
    text = 'version = 1\ngrace = { "Sol Ring" = 2 }\n'
    path = write(tmp_path, text)
    with pytest.raises(ProfileError, match=r"write grace as a \[grace\] table"):
        settings_with(tmp_path, {"Mind Stone": 3})
    assert path.read_bytes() == text.encode("utf-8")
    save_settings(tmp_path, DECK, adjacent=[], ignore=[], tag_search=True)
    assert read_profile(tmp_path).grace == {"Sol Ring": 2.0}


def test_settings_saved_without_grace_leave_the_grace_cards_as_they_are(tmp_path: Path) -> None:
    write(tmp_path, WITH_GRACE)
    save_settings(tmp_path, DECK, adjacent=[], ignore=[], tag_search=True)
    assert read_profile(tmp_path).grace == {"Rings of Brighthearth": 2.5, "Urza's Saga": 4.0}


BOM = b"\xef\xbb\xbf"


def test_a_profile_with_a_byte_order_mark_gets_the_grace_table_and_keeps_the_mark(tmp_path: Path) -> None:
    path = tmp_path / "profile.toml"
    path.write_bytes(BOM + WITHOUT_GRACE.encode("utf-8"))
    before = read_profile(tmp_path)
    assert ensure_grace_table(tmp_path) is True
    assert path.read_bytes() == BOM + (
        'version = 1\narchetype = "Squirrels"\n\n'
        + GRACE_BLOCK
        + '\n# Lands decks that play like it.\n[ideas]\ncommanders = ["The Gitrog Monster"]\n'
    ).encode("utf-8")
    assert read_profile(tmp_path) == before


def test_saving_settings_to_a_profile_with_a_byte_order_mark_edits_the_grace_and_keeps_the_mark(
    tmp_path: Path,
) -> None:
    path = tmp_path / "profile.toml"
    path.write_bytes(BOM + WITHOUT_GRACE.encode("utf-8"))
    settings_with(tmp_path, {"Sol Ring": 2.0})
    data = path.read_bytes()
    assert data.startswith(BOM)
    assert not data.startswith(BOM + BOM)
    assert read_profile(tmp_path).grace == {"Sol Ring": 2.0}


def test_a_new_card_lands_above_the_comment_that_touches_the_next_table(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        'version = 1\n[grace]\n"A" = 2\n# Lands decks that play like it.\n[ideas]\ncommanders = []\n',
    )
    settings_with(tmp_path, {"A": 2, "B": 3})
    assert path.read_text(encoding="utf-8").split("commanders")[0] == (
        'version = 1\n[grace]\n"A" = 2\n"B" = 3\n# Lands decks that play like it.\n[ideas]\n'
    )


def test_comments_under_the_last_entry_stay_with_the_grace_table_when_a_blank_line_follows(
    tmp_path: Path,
) -> None:
    path = write(tmp_path, 'version = 1\n[grace]\n"A" = 2\n# about A\n\n[ideas]\ncommanders = []\n')
    settings_with(tmp_path, {"A": 2, "B": 3})
    assert path.read_text(encoding="utf-8").startswith(
        'version = 1\n[grace]\n"A" = 2\n# about A\n"B" = 3\n\n[ideas]\n'
    )


def test_new_cards_are_written_with_quoted_keys_like_the_existing_ones(tmp_path: Path) -> None:
    path = write(tmp_path, 'version = 1\n[grace]\n"Sol Ring" = 3\n')
    settings_with(tmp_path, {"Sol Ring": 3, "Necropotence": 2, "Mind Stone": 1.5, "Urza's Saga": 4})
    text = path.read_text(encoding="utf-8")
    assert '"Sol Ring" = 3\n"Necropotence" = 2\n"Mind Stone" = 1.5\n"Urza\'s Saga" = 4\n' in text
    assert read_profile(tmp_path).grace == {
        "Sol Ring": 3.0,
        "Necropotence": 2.0,
        "Mind Stone": 1.5,
        "Urza's Saga": 4.0,
    }


@pytest.mark.parametrize("header", ['["grace"]', "[ 'grace' ]", "[ grace ]  # mine"])
def test_a_quoted_or_spaced_grace_header_is_edited_in_place(tmp_path: Path, header: str) -> None:
    path = write(tmp_path, f'version = 1\n{header}\n"A" = 2\n\n[ideas]\ncommanders = []\n')
    settings_with(tmp_path, {"A": 3, "B": 2})
    expected = f'version = 1\n{header}\n"A" = 3\n"B" = 2\n\n[ideas]\n'
    assert path.read_text(encoding="utf-8").startswith(expected)
    assert read_profile(tmp_path).grace == {"A": 3.0, "B": 2.0}
