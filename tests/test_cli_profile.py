import json
from pathlib import Path

from commander.deckprofile import PROFILE_FILE
from conftest import Json
from test_cli import run, suggest, synced

PROFILE = """\
archetype = "Dwarf aggro"
bracket_ceiling = 4
max_card_eur = 20
[targets]
draw = 0
[themes]
types = ["Instant"]
tags = ["no-such-tag"]
[exclude]
cards = ["Chaos Warp"]
[keep]
categories = ["Maybeboard"]
[ideas]
commanders = ["The Gitrog Monster", ["Tevesh Szat, Doom of Fools", "Kodama of the East Tree"]]
"""


def deck_folder(tmp_path: Path) -> Path:
    return next((tmp_path / "decks").iterdir())


def profile(args: list[str], tmp_path: Path, cache: Path) -> tuple[int, str, str]:
    result = run(["profile", *args, "--workspace", str(tmp_path / "decks")], tmp_path, cache=cache)
    return result.code, result.out, result.err


def test_init_writes_a_template_once(tmp_path: Path, raw_deck: Json, own_cache: Path) -> None:
    synced(tmp_path, raw_deck)
    code, out, _ = profile(["magda", "--init"], tmp_path, own_cache)
    assert code == 0
    assert (deck_folder(tmp_path) / PROFILE_FILE).is_file()
    assert out.startswith("wrote ")
    code, _, err = profile(["magda", "--init"], tmp_path, own_cache)
    assert code == 2
    assert "already exists" in err


def test_without_a_profile_says_how_to_start_one(tmp_path: Path, raw_deck: Json, own_cache: Path) -> None:
    synced(tmp_path, raw_deck)
    code, out, _ = profile(["magda"], tmp_path, own_cache)
    assert code == 0
    assert out.splitlines()[1:] == [
        "no profile; `cmdr profile 10004639 --init` starts one",
        "from Archidekt:",
        "  Dwarves (deck tag): types Dwarf",
        "  Vehicles (deck tag): types Vehicle",
        "  cEDH (deck tag): how the deck plays, not a theme",
    ]


def test_profile_is_shown_and_unknown_tags_are_warned(
    tmp_path: Path, raw_deck: Json, own_cache: Path
) -> None:
    synced(tmp_path, raw_deck)
    (deck_folder(tmp_path) / PROFILE_FILE).write_text(PROFILE, encoding="utf-8")
    code, out, err = profile(["magda"], tmp_path, own_cache)
    assert code == 0
    assert out.splitlines()[1:] == [
        "archetype: Dwarf aggro",
        "bracket ceiling: 4",
        "price limit: €20.00",
        "targets: draw 0",
        "themes: types Instant; tags no-such-tag",
        "exclude: Chaos Warp",
        "keep: categories Maybeboard",
        "ideas also from: The Gitrog Monster, Tevesh Szat, Doom of Fools + Kodama of the East Tree",
        "from Archidekt:",
        "  Dwarves (deck tag): types Dwarf",
        "  Vehicles (deck tag): types Vehicle",
        "  cEDH (deck tag): how the deck plays, not a theme",
    ]
    assert "tag 'no-such-tag' is not a Tagger tag" in err


def test_profile_shows_the_derived_themes_it_leaves_out(
    tmp_path: Path, raw_deck: Json, own_cache: Path
) -> None:
    synced(tmp_path, raw_deck)
    ignoring = PROFILE.replace("[exclude]", 'ignore = ["Vehicles", "Spaghetti"]\n[exclude]')
    (deck_folder(tmp_path) / PROFILE_FILE).write_text(ignoring, encoding="utf-8")
    code, out, err = profile(["magda"], tmp_path, own_cache)
    assert code == 0
    lines = out.splitlines()
    assert "themes: types Instant; tags no-such-tag; ignore Vehicles, Spaghetti" in lines
    assert "  Vehicles (deck tag): left out by the profile" in lines
    assert "the profile ignores 'Spaghetti', which is no deck tag or category" in err


def test_profile_json(tmp_path: Path, raw_deck: Json, own_cache: Path) -> None:
    synced(tmp_path, raw_deck)
    (deck_folder(tmp_path) / PROFILE_FILE).write_text(PROFILE, encoding="utf-8")
    code, out, _ = profile(["magda", "--json"], tmp_path, own_cache)
    assert code == 0
    data = json.loads(out)
    assert (data["maxCardCents"], data["targets"], data["unknownTags"]) == (
        2000,
        {"draw": 0},
        ["no-such-tag"],
    )
    assert data["derived"][1] == {
        "source": "deck tag",
        "name": "Vehicles",
        "types": ["Vehicle"],
        "keywords": [],
        "tags": [],
        "covered": None,
        "size": None,
        "used": True,
    }
    assert (data["broadTags"], data["unmatchedDeckTags"], data["vagueCategories"]) == (["cEDH"], [], [])
    assert (data["themes"]["ignore"], data["unknownIgnores"]) == ([], [])


def test_invalid_profile_is_a_usage_error(tmp_path: Path, raw_deck: Json, own_cache: Path) -> None:
    synced(tmp_path, raw_deck)
    (deck_folder(tmp_path) / PROFILE_FILE).write_text("bracket_ceiling = 9\n", encoding="utf-8")
    code, _, err = profile(["magda"], tmp_path, own_cache)
    assert code == 2
    assert "bracket_ceiling must be a whole number from 1 to 5" in err


def test_suggest_follows_the_profile(tmp_path: Path, raw_deck: Json, own_cache: Path) -> None:
    fake = synced(tmp_path, raw_deck)
    (deck_folder(tmp_path) / PROFILE_FILE).write_text(PROFILE, encoding="utf-8")
    result = suggest(["magda", "--json"], tmp_path, fake, own_cache)
    assert result.code == 0, result.err
    [deck] = json.loads(result.out)
    roles = {r["role"]: r for r in deck["roles"]}
    # draw 0 is met; Mind Stone has no price and Jeska's Will (EUR 33.80) is over the EUR 20 limit;
    # Chaos Warp is an Instant but excluded by name.
    assert (roles["draw"]["have"], roles["draw"]["target"], roles["draw"]["short"]) == (0, 0, False)
    assert [i["name"] for i in roles["ramp"]["ideas"]] == ["Arcane Signet"]
    # Lightning Bolt is an Instant, the profile's theme; High-Speed Hoverbike is from the Vehicles deck tag.
    assert [(i["name"], i["fits"]) for i in deck["themed"]] == [
        ("Lightning Bolt", ["profile"]),
        ("High-Speed Hoverbike", ["Vehicles"]),
    ]
    assert deck["ceiling"] == 4
    assert "profile tag 'no-such-tag' is not a Tagger tag" in result.err


def test_suggest_leaves_out_the_derived_themes_the_profile_ignores(
    tmp_path: Path, raw_deck: Json, own_cache: Path
) -> None:
    fake = synced(tmp_path, raw_deck)
    ignoring = PROFILE.replace("[exclude]", 'ignore = ["vehicles", "Spaghetti"]\n[exclude]')
    (deck_folder(tmp_path) / PROFILE_FILE).write_text(ignoring, encoding="utf-8")
    result = suggest(["magda", "--json"], tmp_path, fake, own_cache)
    [deck] = json.loads(result.out)
    assert [i["name"] for i in deck["themed"]] == ["Lightning Bolt"]
    assert "the profile ignores 'Spaghetti', which is no deck tag or category" in result.err


def test_suggest_prints_the_themed_section(tmp_path: Path, raw_deck: Json, own_cache: Path) -> None:
    fake = synced(tmp_path, raw_deck)
    (deck_folder(tmp_path) / PROFILE_FILE).write_text(PROFILE, encoding="utf-8")
    out = suggest(["magda"], tmp_path, fake, own_cache).out
    section = out.split("\ncards that fit the deck's themes (most played first)\n")[1]
    assert section.splitlines()[0].endswith("  Lightning Bolt (fits profile)")
