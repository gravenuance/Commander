"""The app's settings written into profile.toml, comments kept."""

from pathlib import Path

import pytest

from commander.deckprofile import ProfileError, read_profile
from commander.models import Deck
from commander.profileedit import save_settings

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
    assert "# [grace]" in text
    path.write_text(
        "\n".join(
            line.removeprefix("# ") if line.startswith(("# [grace]", '# "Basalt Monolith"')) else line
            for line in text.splitlines()
        ),
        encoding="utf-8",
    )
    profile = read_profile(tmp_path)
    assert profile.tag_search is True
    assert profile.grace == {"Basalt Monolith": 3.0}


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
