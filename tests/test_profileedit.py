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


def test_settings_replace_their_two_lists_and_keep_everything_else(tmp_path: Path) -> None:
    (tmp_path / "profile.toml").write_text(PROFILE, encoding="utf-8")
    pair = ["Tevesh Szat, Doom of Fools", "Kodama of the East Tree"]
    save_settings(tmp_path, DECK, adjacent=[["Ygra, Eater of All"], pair], ignore=["landfall", "Tokens"])
    text = (tmp_path / "profile.toml").read_text(encoding="utf-8")
    assert "# Chatterfang's profile: the owner's own notes." in text
    assert 'types = ["Squirrel"]  # the tribe' in text
    assert "# Lands decks that play like it." in text
    profile = read_profile(tmp_path)
    assert profile.adjacent == (("Ygra, Eater of All",), tuple(pair))
    assert profile.ignore_themes == ("landfall", "Tokens")
    assert profile.themes.types == ("Squirrel",)


def test_repeats_are_dropped_and_blank_names_left_out(tmp_path: Path) -> None:
    (tmp_path / "profile.toml").write_text(PROFILE, encoding="utf-8")
    profile = save_settings(
        tmp_path,
        DECK,
        adjacent=[["Ygra, Eater of All"], ["ygra, eater of all "], ["  "]],
        ignore=["Tokens", "tokens", ""],
    )
    assert profile.adjacent == (("Ygra, Eater of All",),)
    assert profile.ignore_themes == ("Tokens",)


def test_a_deck_without_a_profile_gets_the_template_with_the_settings(tmp_path: Path) -> None:
    save_settings(tmp_path, DECK, adjacent=[["The Gitrog Monster"]], ignore=[])
    text = (tmp_path / "profile.toml").read_text(encoding="utf-8")
    assert "# What Test deck" in text
    assert read_profile(tmp_path).adjacent == (("The Gitrog Monster",),)


def test_too_many_commanders_leave_the_file_alone(tmp_path: Path) -> None:
    (tmp_path / "profile.toml").write_text(PROFILE, encoding="utf-8")
    with pytest.raises(ProfileError, match="over 8 commanders"):
        save_settings(tmp_path, DECK, adjacent=[[f"Commander {i}"] for i in range(9)], ignore=[])
    assert (tmp_path / "profile.toml").read_text(encoding="utf-8") == PROFILE
